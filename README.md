# CompNova QXDM automation backend

Offline-tested software that implements the **QXDM capture + QCAT text
handoff** stage of CompNova's automation.  This repository contains:

* A typed, lifecycle-aware orchestrator with a guarded vendor boundary.
* Behavioural simulation adapters that exercise the full orchestration
  without a real QXDM or QCAT.
* An operator CLI with `plan`, `run`, `resume`, and `inventory`
  sub-commands.
* An atomic run manifest, a single-owner lock, a deadline-driven
  monitor, a publication layer with two consumer contracts, and a
  conversion-only resume path.
* 84 pytest tests covering the 15 behaviour categories listed in the
  authoritative prompt, all runnable on a Python 3.10+ host with no
  Windows or vendor software.
* Documentation: the **evidence / binding matrix**, **operator
  guide**, and **live commissioning checklist**.

> This is offline-tested software.  It is **not** a live, production
> system.  The vendor adapters raise
> `UnsupportedCapabilityError` on every method until a documented
> binding is wired in (see `docs/evidence_binding.md`).  The simulation
> adapters are clearly labelled and do not represent a real QXDM or
> QCAT.

## Quick start (offline)

```powershell
PS> python -m pip install -e .
PS> $env:PYTHONPATH = "$PWD\src"
PS> python -m qxdm_automation.cli -v run -c examples\offline_sim.json
```

The above runs the simulation end-to-end: it generates fake QXDM
segments, converts them through the synthetic QCAT, publishes the
result under the configured publication root, and writes a complete
manifest with a `READY` marker.  Total runtime is a few seconds.

## Quick start (planning)

```powershell
PS> python -m qxdm_automation.cli -v plan -c examples\offline_sim.json
```

Validates the configuration, the configured paths, and the simulated
adapter capabilities.  Returns a JSON plan; never touches the
filesystem beyond the run workspace root.

## Quick start (live)

Before `mode=live` is meaningful, an adapter binding must be wired into
`src/qxdm_automation/adapters/real_qxdm.py` (see
`docs/evidence_binding.md §4` for the precise minimum).  The shipped
adapters raise `UnsupportedCapabilityError` on every call, so the run
will be rejected at preflight.

## Documentation

* `docs/evidence_binding.md` — what the supplied PDFs do and do not
  establish, plus the per-operation binding matrix.
* `docs/operator_guide.md` — installation, configuration, CLI, run
  layout, cancellation, retention, and the GUI-fallback procedure.
* `docs/commissioning_checklist.md` — the live commissioning steps
  that must be signed before unattended capture.

## Tests

```powershell
PS> python -m pip install -e .[vendor]   # optional
PS> $env:PYTHONPATH = "$PWD\src"
PS> python -m pytest -q
```

The suite runs in ~30 seconds on a developer host.

## Repository layout

```
src/qxdm_automation/
├── __init__.py
├── cli.py
├── config/         # configuration schema and loader
├── adapters/       # vendor boundary (real + simulation)
├── locking/        # single-owner lock with stale recovery
├── manifest/       # atomic run manifest
├── monitoring/     # capture monitor and flush waiter
├── discovery/      # diagnostic target hints
├── conversion/     # QCAT conversion wrapper
├── publication/    # crash-safe TXT handoff
├── orchestration/  # the lifecycle state machine driver
├── types.py        # shared value types
├── errors.py       # typed exception hierarchy
└── utils/          # hashing, atomic IO, path safety
tests/
├── unit/           # configuration, manifest, lock, utils
├── integration/    # orchestrator lifecycle, adapters, publication, failure paths
└── offline_sim/    # end-to-end via the CLI
examples/
├── offline_sim.json
├── planning_only.json
└── sample.dmc
docs/
├── evidence_binding.md
├── operator_guide.md
└── commissioning_checklist.md
```
