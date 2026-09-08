# Phase 4.2 — Evidence-cycle-safe human work

## Scope and promotion result

This increment follows SPEC_V0 §B, §F, §H and §K: repeated exception cycles,
human attribution, history preservation and new work on the **same Case**.
Case Engine orchestrates; deterministic policy evaluates evidence; coordination
records never establish courier/customer truth.

The protected Phase 4.1 checkpoint has 140 tests. The initial Phase 4.2 increment
added 48. Adversarial review produced a **NO-GO** with eight promotion blockers.
The remediation adds **55 regression cases** and corrects all eight contracts
below. Final validation: **243 passed, 0 failed, 0 skipped**.

No existing test was edited, removed, skipped or weakened. The original
`core.import_source` implementation, including its SAVEPOINT fix, is unchanged
from the protected checkpoint.

## Corrected production contracts

### 1. Unclassified evidence cannot silently discharge work

- The latest RFD requires a customer response after that observation. Relevant
  customer responses are reconsidered in every cycle, not only the first one.
- A new unsupported courier observation, including INA, requires explicit human
  review through `DECIDE_ACTION`. It is not interpreted as resolution or as a new
  invented courier taxonomy.
- Policy returns an explicit `retire_work` authorization. `MONITOR` alone cannot
  cancel an outstanding obligation. Reconciliation either retains work, creates
  a valid linked replacement, or records deliberate retirement.
- Notes and follow-up coordination do not ordinarily change the RFD work basis.
  Existing text-based refusal routing remains limited; it is not language
  understanding or adjudication of who is telling the truth.

### 2. Cancellation does not permanently occupy request identity

- Task creation is atomic and idempotent under concurrent requests.
- A cancelled request gets a deterministic successor key referencing the
  cancelled task. Repeated requests converge on that successor rather than
  duplicating it or returning the unusable cancelled row.
- Reopening a Case gives work a `requirement_scope` referencing its reopen audit
  event. Earlier **completed** work cannot suppress a renewed requirement, and
  earlier pending work cannot complete under the new lifecycle scope.
- Supersession retains original payload, reason, evidence references and the
  replacement ID. Completed history is not overwritten.

### 3. Completion requires a proven current basis

- Current VERIFY/DECIDE capability requests bind evidence at **creation**, inside
  the writer transaction. Before completion, capability, nonempty evidence IDs
  and lifecycle scope must still match current policy.
- There is no missing-basis wildcard or submission-time adoption. Truly unbound
  historical tasks cannot complete and are explicitly replaced on evaluation.
- A damaged legacy payload can be recovered only during reconciliation when its
  immutable canonical request hash proves the exact current request basis.
  Capability equality alone is never sufficient proof.
- Non-current low-level requests remain storable for earlier-phase compatibility,
  but are not authority to complete current Case work.

### 4. Terminal completion uses the authoritative outcome contract

- Task delivery/cancellation/return decisions use the same domain outcome APIs
  as standalone operations. They must actually derive `CLOSED_DELIVERED`,
  `CLOSED_CANCELLED` or `RETURNED`, respectively.
- Validation considers all tracking occurrence/receipt, customer and operator
  timestamps, including notes outside the minimal task basis. Explicit older or
  equal-time terminal submissions fail **before** evidence is written.
- Writer reservation precedes validation. Outcome insert, shipment refresh,
  task completion and subsequent evaluation succeed or roll back together.
- `transactions.atomic` uses `BEGIN IMMEDIATE` for owned transactions and
  SAVEPOINTs for caller-owned transactions. Nested failures preserve unrelated
  caller work; helpers never commit that work. Lock failures remain explicit and
  may be retried as whole operations.

### 5. RESOLVED and ABANDONED have deliberate, distinct behavior

- Explicit evaluation reuses an existing Case rather than creating a disconnected
  one. A RESOLVED Case reopens when a genuinely new courier/customer record exists
  beyond its recorded closure boundary, even with equal or backdated timestamps.
- ABANDONED does **not** automatically reopen; an operator must explicitly reopen
  it. Repeating the same lifecycle status is an idempotent no-op.
- New append-only `case_status_event` records retain from/to status, timestamp,
  reason, prior resolution and three scalar evidence cursors. Reopening clears
  the active resolution only after preserving it in the audit.
- Policy excludes preclosure decisions/responses when assessing renewed work.
  Preclosure timestamps cannot keep an otherwise valid new decision unsettled.
- There is no full evidence snapshot, new workflow status or `current_step`.

### 6. Timestamp validation and read compatibility are separate

- Supplied action/customer/follow-up timestamps must parse as ISO date/time.
  Malformed or empty supplied timestamps fail domain validation atomically.
- Source timestamp strings and offsets are preserved verbatim. Comparison uses
  UTC instants; supported legacy naive values are interpreted as UTC. Date-only
  follow-up values use midnight UTC. No SLA or invented deadline is introduced.
- Default human evidence times use now or a logical successor one microsecond
  after the latest known evidence time. This establishes write ordering, not
  proof of when an external source event actually occurred.
- Policy, shipment derivation, timeline, terminal validation, evidence lists,
  follow-up due comparisons and Case metadata compare instants, not raw strings.
- Within-source equal times use append order. Cross-table rowids do not prove
  causality: equal-time courier/terminal records cannot establish terminal
  closure, and policy requires a strictly later settling decision. Nonterminal
  human actions may still derive ACTION_TAKEN; that is not Case resolution.
- Malformed historical authoritative timestamps remain intact and visible.
  Affected shipments derive NEEDS_ACTION and appear with `timestamp_review`;
  their chronology-dependent terminal/task completion is blocked. Other
  shipments and queue/detail HTTP reads remain usable.
- Unknown-time timeline entries are flagged, not assigned a fictional source
  instant. Ambiguous cross-source ties are labelled. Inferred state transitions
  are suppressed when chronology is uncertain. Valid transitions attach to a
  source plus row identity, never another table's equal rowid.
- Invalid legacy follow-up due dates are visible but not guessed overdue.

### 7. IN_PROGRESS work remains actionable

- The operational queue includes both PENDING and IN_PROGRESS tasks.
- Current tasks in either status expose the same review/completion forms and
  domain completion path. IN_PROGRESS does not acquire a completion timestamp.
- Stale, cancelled and already completed submissions still fail without duplicate
  evidence. Review-blocked tasks visibly explain why completion is unavailable.

### 8. Cancellation confirmation is encoding-independent

- The domain decision handler normalizes the decision kind before checking
  confirmation. Both typed forms and legacy JSON reach this same validation.
- Explicit JSON boolean `true` or typed `"yes"` is required for task cancellation.
  Missing/false values, misleading strings and numeric boolean lookalikes fail
  atomically. A nonempty cancellation reason remains required and verbatim.
- Calling the explicitly named standalone `mark_cancelled` API remains an
  explicit cancellation command with its existing required-reason contract.

## Upgrade and historical-data handling

`core.connect` installs the additive lifecycle audit table/index using
`CREATE ... IF NOT EXISTS`. Existing evidence is not rewritten or snapshotted.
Back up the database before upgrading and preserve evidence-table rowids/append
order during maintenance; closure cursors depend on the append-only contract.
Do not delete, reorder or renumber evidence as an implicit migration.

**Legacy closed Cases without an audit boundary:** timestamps cannot prove which
records were present at closure. The engine therefore does not guess a cutoff
from `updated_at`. On explicit evaluation, a legacy RESOLVED Case with evidence
reopens for history review with unknown (NULL) cursors. Completion remains
blocked until an operator reviews the history and records an explicit lifecycle
decision. ABANDONED remains abandoned. A reviewed resolution through
`case.update_case_status(..., 'RESOLVED', reason=...)` captures a known boundary;
a subsequent deliberate reopen or genuinely new evidence uses that boundary.
Merely reading queue/detail does not perform this migration/evaluation.

**Malformed historical source timestamps:** there is no automatic rewrite to
now, epoch or receipt time. Inventory them through the read-only diagnostic:

```python
from commerceops.timestamps import shipment_timestamp_issues
issues = shipment_timestamp_issues(conn, shipment_id)
# Each issue identifies source table, record ID, field and original value.
```

The UI shows the same uncertainty. Source-data correction requires verified
source information and a separately reviewed/audited data migration retaining
original values and provenance. This phase intentionally does not add a guessed
repair algorithm or an unaudited source-editing endpoint. Re-evaluate after a
verified repair; do not bypass the block by completing an old task.

## Operator surface and boundaries

- Shipment detail offers **Evaluate current evidence**. GETs remain read-only;
  evidence written outside task completion requires explicit evaluation.
- Customer assertions are never prefilled; actual response, contact channel,
  decision, actor and cancellation reason flow to the existing evidence APIs.
- Follow-up completion uses its stored reference, checks shipment ownership and
  preserves the supplied completion time. It does not trust a client replacement
  ID and is not superseded by the RFD policy.
- The legacy importer retains its undated-row dedupe contract: it cannot identify
  a repeated identical undated observation as a new event.
- The UI remains a local unauthenticated harness, not a public multi-tenant
  deployment. No courier API, external sending, autonomous execution, AI/LLM,
  new integration or speculative scheduler was added.

## Validation (Python 3.11)

Command suffix after `.venv/bin/python -m pytest -q`:

| Suite | Passed |
|---|---:|
| `tests/test_phase2.py` independently | 4 |
| `tests/test_phase3.py` independently | 11 |
| `tests/test_promotion_safety.py` | 55 |
| `tests/test_phase4.py tests/test_phase4_2.py tests/test_phase4_2_ui.py tests/test_promotion_safety.py` | 117 |
| Complete suite | **243** |

`compileall`, `pip check` and `git diff --check` pass. Two upstream
Starlette/AnyIO/httpx deprecation warnings remain visible; none are suppressed.
Regression coverage includes complete repeated cycles, unsupported evidence,
legacy binding proof/rejection, cancellation successor races, concurrent same-Case
reopening, closure migration uncertainty, terminal writer reservation and nested
rollback, malformed-history HTTP isolation, time offsets/ties, IN_PROGRESS and
all accepted task-cancellation encodings.
