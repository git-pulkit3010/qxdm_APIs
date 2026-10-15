"""Operator-facing CLI.

The CLI is the only public entry point for end-users.  It is small on
purpose: every command is a thin wrapper around the orchestrator.  The
CLI never instantiates vendor automation objects — the configuration
loader and the orchestrator's :meth:`plan` are the only preflight paths.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Optional, Sequence

from .adapters import (
    SimulationQcatAdapter,
    SimulationQxdmAdapter,
    make_qcat_adapter,
    make_qxdm_adapter,
)
from .config import load_run_config
from .errors import (
    ConfigurationError,
    LockAcquisitionError,
    PreflightFailed,
    QxdmAutomationError,
    UnsupportedCapabilityError,
)
from .monitoring import CancellationToken
from .orchestration import Clock, Orchestrator, OrchestrationResult
from .types import LifecycleState
from .utils import normalize_path

LOGGER = logging.getLogger("qxdm_automation")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    )
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(level)
    LOGGER.propagate = False


def _print_json(obj, *, indent: int = 2) -> None:
    json.dump(obj, sys.stdout, indent=indent, sort_keys=True, default=str)
    sys.stdout.write("\n")


def _build_orchestrator(args: argparse.Namespace) -> Orchestrator:
    config = load_run_config(Path(args.config).expanduser())
    contract = getattr(args, "publication_contract", None) or "run_folder"
    qxdm_adapter = make_qxdm_adapter(_resolve_qxdm_binding(config))
    qcat_adapter = make_qcat_adapter(_resolve_qcat_binding(config))
    if getattr(args, "sim_rotation_bytes", None) is not None:
        # Allow tests / demos to override the simulation rotation size.
        qxdm_adapter = SimulationQxdmAdapter(
            target_selector=args.sim_target_selector or "COM-SIM-1",
            rotation_bytes=int(args.sim_rotation_bytes),
            refuse_config=args.sim_refuse_config,
            crash_after_start=args.sim_crash_after_start,
            no_data=args.sim_no_data,
            resume_after_pause=args.sim_resume_after_pause,
            stop_fails=args.sim_stop_fails,
            write_delay_seconds=args.sim_write_delay,
        )
        qcat_adapter = SimulationQcatAdapter(
            fail_on=args.sim_qcat_fail.split(",") if args.sim_qcat_fail else None,
            delay_seconds=args.sim_qcat_delay,
            output_corruption=args.sim_qcat_corrupt,
            timeout_simulation=args.sim_qcat_timeout,
        )
    clock = Clock()
    if getattr(args, "sim_advance_seconds", None):
        # Allow tests to pre-advance the simulated clock.
        for chunk in args.sim_advance_seconds:
            clock.advance(chunk)
    cancellation = CancellationToken()
    orchestrator = Orchestrator(
        config,
        qxdm_adapter=qxdm_adapter,
        qcat_adapter=qcat_adapter,
        clock=clock,
        cancellation=cancellation,
        publication_contract=contract,
    )
    return orchestrator


def _resolve_qxdm_binding(config) -> str:
    if config.mode.value in {"offline", "planning"}:
        return "simulation"
    return config.tool.qxdm_binding


def _resolve_qcat_binding(config) -> str:
    if config.mode.value in {"offline", "planning"}:
        return "simulation"
    return config.tool.qcat_binding


def _install_sigint(cancel: CancellationToken) -> None:
    def _handler(signum, frame):  # noqa: ARG001
        LOGGER.warning("signal %s received; requesting cancellation", signum)
        cancel.cancel()

    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):
        # SIGINT cannot be installed from non-main threads; ignore.
        pass


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        orchestrator = _build_orchestrator(args)
    except ConfigurationError as exc:
        LOGGER.error("configuration error: %s", exc)
        return 2
    try:
        plan = orchestrator.plan()
    except UnsupportedCapabilityError as exc:
        _print_json(
            {
                "state": LifecycleState.BLOCKED.value,
                "blocking_reasons": [str(exc)],
            }
        )
        return 3
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.error("plan failed: %s", exc)
        return 4
    _print_json(plan)
    return 0 if plan["state"] == LifecycleState.PREFLIGHT_OK.value else 3


def cmd_run(args: argparse.Namespace) -> int:
    try:
        orchestrator = _build_orchestrator(args)
    except ConfigurationError as exc:
        LOGGER.error("configuration error: %s", exc)
        return 2
    _install_sigint(orchestrator.cancellation)
    try:
        result = orchestrator.run()
    except UnsupportedCapabilityError as exc:
        LOGGER.error("vendor capability missing: %s", exc)
        return 5
    except QxdmAutomationError as exc:
        LOGGER.error("orchestration failed: %s", exc)
        return 6
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.error("unexpected error: %s", exc)
        return 7
    _print_json(result.to_dict())
    if result.success:
        return 0
    if result.state is LifecycleState.BLOCKED:
        return 3
    if result.state is LifecycleState.CANCELLED:
        return 130
    return 1


def cmd_resume(args: argparse.Namespace) -> int:
    try:
        orchestrator = _build_orchestrator(args)
    except ConfigurationError as exc:
        LOGGER.error("configuration error: %s", exc)
        return 2
    try:
        result = orchestrator.resume_conversion()
    except UnsupportedCapabilityError as exc:
        LOGGER.error("vendor capability missing: %s", exc)
        return 5
    except QxdmAutomationError as exc:
        LOGGER.error("resume failed: %s", exc)
        return 6
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.error("unexpected error: %s", exc)
        return 7
    _print_json(result.to_dict())
    return 0 if result.success else 1


def cmd_inventory(args: argparse.Namespace) -> int:
    """Inspect local tool installs and report capabilities.

    Never instantiates vendor automation.  Only inspects file presence
    and registered type libraries when the relevant Python imports are
    available.
    """

    out: dict = {"qxdm": {}, "qcat": {}, "notes": []}
    qxdm_path = Path(args.qxdm_path).expanduser() if args.qxdm_path else None
    qcat_path = Path(args.qcat_path).expanduser() if args.qcat_path else None

    qxdm_adapter = make_qxdm_adapter("inventory")
    try:
        qxdm_report = qxdm_adapter.inventory(qxdm_path=qxdm_path)
    except UnsupportedCapabilityError as exc:
        out["qxdm"] = {"error": str(exc), "binding": qxdm_adapter.binding}
    else:
        out["qxdm"] = _capability_to_dict(qxdm_report)

    qcat_adapter = make_qcat_adapter("inventory")
    try:
        qcat_report = qcat_adapter.inventory(qcat_path=qcat_path)
    except UnsupportedCapabilityError as exc:
        out["qcat"] = {"error": str(exc), "binding": qcat_adapter.binding}
    else:
        out["qcat"] = _capability_to_dict(qcat_report)

    # Lightweight Windows inspection: presence of common install paths.
    if sys.platform == "win32":
        candidates = [
            Path(r"C:\Program Files\Qualcomm\QXDM\QXDM.exe"),
            Path(r"C:\Program Files (x86)\Qualcomm\QXDM\QXDM.exe"),
            Path(r"C:\Qualcomm\Tools\QCAT\QCAT.exe"),
        ]
        found = []
        for c in candidates:
            try:
                if c.exists():
                    found.append(str(c))
            except OSError:
                pass
        out["candidate_paths_present"] = found
    else:
        out["notes"].append("non-Windows host — install-path check skipped")

    _print_json(out)
    return 0


def _capability_to_dict(report) -> dict:
    return {
        "binding": report.qxdm_binding if report.qxdm_path else report.qcat_binding,
        "path": str(report.qxdm_path) if report.qxdm_path else str(report.qcat_path),
        "native_rotation_supported": report.native_rotation_supported,
        "binary_format_evidence": sorted(report.binary_format_evidence),
        "qcat_export_evidence": report.qcat_export_evidence,
        "blocking_reasons": list(report.blocking_reasons),
        "notes": list(report.notes),
    }


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qxdm-automation",
        description="CompNova QXDM capture + QCAT text handoff.",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="validate configuration and report capabilities")
    _add_config_arg(p_plan)
    _add_simulation_args(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_run = sub.add_parser("run", help="execute a full capture + conversion + publication run")
    _add_config_arg(p_run)
    _add_simulation_args(p_run)
    p_run.add_argument(
        "--publication-contract",
        choices=["run_folder", "flat"],
        default="run_folder",
        help="how the TXT handoff is presented to the consumer (default: run_folder)",
    )
    p_run.set_defaults(func=cmd_run)

    p_resume = sub.add_parser(
        "resume",
        help="re-run conversion + publication for an existing finalised capture",
    )
    _add_config_arg(p_resume)
    _add_simulation_args(p_resume)
    p_resume.add_argument(
        "--publication-contract",
        choices=["run_folder", "flat"],
        default="run_folder",
    )
    p_resume.set_defaults(func=cmd_resume)

    p_inv = sub.add_parser(
        "inventory",
        help="inspect local tool installs and report capabilities without launching anything",
    )
    p_inv.add_argument("--qxdm-path", help="optional path to the QXDM executable")
    p_inv.add_argument("--qcat-path", help="optional path to the QCAT executable")
    p_inv.set_defaults(func=cmd_inventory)

    return p


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        help="path to a JSON configuration file",
    )


def _add_simulation_args(parser: argparse.ArgumentParser) -> None:
    """Arguments that influence the simulation adapter.

    The default behaviour is to use the simulation adapter in any mode;
    these flags let tests force specific failure modes.  They have no
    effect when the real adapter is wired in.
    """

    parser.add_argument("--sim-rotation-bytes", type=int, default=None)
    parser.add_argument("--sim-target-selector", default=None)
    parser.add_argument("--sim-refuse-config", action="store_true")
    parser.add_argument("--sim-crash-after-start", action="store_true")
    parser.add_argument("--sim-no-data", action="store_true")
    parser.add_argument("--sim-resume-after-pause", action="store_true")
    parser.add_argument("--sim-stop-fails", action="store_true")
    parser.add_argument("--sim-write-delay", type=float, default=0.0)
    parser.add_argument(
        "--sim-qcat-fail",
        default=None,
        help="comma-separated list of source basenames that the simulation QCAT should fail on",
    )
    parser.add_argument("--sim-qcat-delay", type=float, default=0.0)
    parser.add_argument("--sim-qcat-corrupt", action="store_true")
    parser.add_argument("--sim-qcat-timeout", action="store_true")
    parser.add_argument(
        "--sim-advance-seconds",
        type=float,
        action="append",
        default=None,
        help="pre-advance the simulated clock (test convenience, may repeat)",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
