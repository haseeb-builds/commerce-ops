# Commerce Ops

Evidence-driven courier/COD exception management. Courier observations, customer
statements and operator decisions remain distinct, with persistent Cases and
idempotent human work.

## Setup and verification

Python 3.11 was used for the verified checkpoint.

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

`pytest.ini` supplies the `packages` import path, including for isolated phase
runs. `requirements.txt` contains the UI runtime dependencies;
`requirements-dev.txt` also installs the complete test dependencies (no skipped
UI suite).

## Local operator UI

```sh
PYTHONPATH=packages COMMERCEOPS_DB=./commerceops.db \
  .venv/bin/python -m commerceops_ui.run
```

Open `http://127.0.0.1:8000`. This is the existing local, unauthenticated operator
harness—not a public multi-user deployment. No courier requests are sent.

Import the documented legacy CSV format, open a shipment, then select **Evaluate
current evidence** to create/reuse its Case and reconcile pending work. Open a
task to record the actual customer response or an explicit operator decision.
Completion records evidence and re-evaluates atomically. After evidence is added
outside task completion, use the same evaluation command to refresh Case work.
Reading the queue never mutates it; stale task submissions are rejected even if
explicit evaluation has not yet run.

## Phase 4.2 promotion safety

Verified at **243 passing tests**, including 55 new promotion regressions.
Current PENDING and IN_PROGRESS work is actionable; resolved Cases reopen on new
evidence, while abandoned Cases require an explicit operator reopen.

Before upgrading an existing database, read the [historical-data and lifecycle
migration contract](docs/PHASE_4_2.md#upgrade-and-historical-data-handling).
Malformed timestamps and unprovable legacy closure boundaries require visible,
explicit review—not guessed chronology or silent completion.

## Specifications and implementation notes

- [Implementation specification](docs/SPEC_V0.md) — supersedes the earlier workflow analysis.
- [Workflow analysis](docs/WORKFLOW.md) — evidence and original operational context.
- [Phase 4.2](docs/PHASE_4_2.md) — scope, contracts, architectural decisions, validation and limits.
