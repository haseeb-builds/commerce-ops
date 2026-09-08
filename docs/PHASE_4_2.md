# Phase 4.2 — Evidence-cycle-safe human work

## Basis and scope

The checkout has a v0 specification, workflow analysis, and Phase 1–4 code/tests,
but no separate Phase 4.2 roadmap. This increment follows SPEC_V0 §B, §F, §H and
§K: repeated exception cycles, human attribution, history preservation and new
work on the same shipment. It strengthens the existing operational loop rather
than adding a new workflow architecture.

The untouched checkpoint was verified at **140 passed, 0 failed, 0 skipped**.
The environment initially lacked pytest and the UI dependencies; installing the
complete dependencies restored the stated baseline without repository changes.

Inspection identified three linked operational gaps:

1. An old operator decision short-circuited the second cycle's fresh customer
   response, leaving the Case requesting verification again.
2. Task identity included the entire evidence history, while obsolete pending
   tasks were never retired and could still record decisions.
3. Action/customer/follow-up writers committed the caller's transaction, making
   task completion partially durable if subsequent evaluation failed.

**Selected increment:** keep human work aligned with current evidence through
complete repeated cycles, expose explicit evaluation in the existing UI, and
make evidence recording + completion + reconciliation atomic.

## Implemented behavior

### Policy and work identity

- The latest RFD requires a response after that observation. A fresh response
  is considered even when an earlier cycle already has a reattempt decision.
- Only existing decision kinds (`reattempt_requested`, `cancel_decided`,
  `delivered_confirmed`, `returned_confirmed`) settle decision work. A later
  note/query/open permission does not erase an earlier decision.
- The policy returns all inspected evidence IDs and a separate small
  `work_evidence_ids` basis: the current courier observation, plus the relevant
  latest customer response when present. No event contents are copied into a
  Case snapshot.
- Unchanged evaluation reuses work. Notes and follow-up records do not change
  RFD work identity. A new relevant courier/customer record creates a new basis.
- Equal-time records **within a source** use append order, not random UUID
  order. Cross-source response-after comparisons require a later timestamp;
  independent tables' rowids do not establish cross-source causality.
- Receipt/import time drives Case reaction to courier evidence. Source occurrence
  time remains stored unchanged. Default human evidence timestamps follow both
  received and occurred courier timestamps.
- Timestamp comparison uses actual instants across timezone offsets. Policy,
  default action timestamps, shipment derivation, timeline ordering and terminal
  timestamp validation share this interpretation. Source strings are not rewritten.

### Reconciliation and audit

- Case Engine assesses authoritative evidence and reconciles active
  `VERIFY_CUSTOMER` / `DECIDE_ACTION` tasks within one write transaction.
- Obsolete pending/in-progress work becomes `CANCELLED`, retaining the original
  payload plus a supersession reason, timestamp, evidence references and optional
  replacement task ID. Completed task history remains intact.
- Legacy unbound capability requests can be adopted once when their capability
  matches current policy, preserving their ID and custom payload. Such historical
  tasks have no recoverable evidence-cycle identity until bound. Legacy tasks
  with full-history evidence IDs are reconciled to the new minimal basis on
  evaluation; they may be retired/replaced once during this transition.
- Independently scheduled `FOLLOW_UP_ACTION` work is not cancelled by RFD policy.
- `evaluate_shipment` creates a Case only when none exists, otherwise reuses the
  most recent existing Case. Concurrent calls through this entry point do not
  create duplicate Cases or tasks.

### Safe operational completion

- A bound task's capability and evidence basis must still match current policy
  **before** evidence is written, even if no explicit re-evaluation has run since
  new evidence arrived. Stale submissions fail without changing records.
- Backdated/equal-time responses or decisions cannot complete work whose evidence
  they do not follow. Invalid completions retain their pending task.
- Customer responses and operator notes are retained verbatim. Cancellation
  reasons and operator attribution are forwarded to the existing action store.
- Evidence insert, shipment refresh, task completion and next evaluation all
  succeed or fail together. A repeated completion remains rejected, not duplicated.
- `transactions.atomic` uses `BEGIN IMMEDIATE` for owned transactions and
  SAVEPOINTs inside caller transactions. Helpers never commit unrelated caller
  work; nested failures roll back only their operation. SQLite lock errors remain
  explicit failures; callers can retry the whole operation.
- Follow-up completion trusts the stored task's follow-up reference, not a
  replacement ID supplied by the client. It checks shipment ownership and records
  the supplied/default completion timestamp in the actual follow-up row.

### UI and setup

- Shipment detail has an explicit **Evaluate current evidence** command.
- Queue/detail GETs remain read-only. Pending tasks link to a review/completion
  form and offer re-evaluation.
- Verification and decision forms require actual operator input. Removed the
  hard-coded `"Verified"` quick-complete button and prefilled customer assertions.
- Decision forms require explicit cancellation confirmation and a reason.
- Superseded task detail shows its reason and replacement link. Completion errors
  and next evaluation are visible and escaped; redirect query strings are encoded.
- The existing JSON completion interface remains supported, including correct
  forwarding of the verification channel.
- Added dependency manifests and setup instructions. `pytest.ini` now supplies
  `packages` so isolated regression modules do not rely on another test module
  modifying `sys.path` first.

## Deliberate contract corrections

1. `DECIDE_ACTION` completion no longer accepts `note`, `query_sent` or
   `open_allowed` as a decision. These remain valid standalone operator actions.
   Accepting them as completion previously hid an unresolved obligation behind a
   completed task's idempotency key.
2. Nested domain writes no longer implicitly commit their caller's transaction.
   Top-level calls still commit on success. Explicit transaction callers must
   commit their own work.
3. Stale/backdated task completions now fail rather than recording evidence under
   an obsolete task. Correction/late-history entry is still available through
   the separate evidence APIs, followed by explicit evaluation.

No schema migration, new workflow state, `current_step`, courier API, LLM,
automatic sending, invented deadline or courier taxonomy was added. The
**core.py import SAVEPOINT implementation is unchanged**; its nested transaction
contract is additionally tested. Existing tests were not edited or weakened.

## Validation (Python 3.11)

| Command suffix after `.venv/bin/python -m pytest -q` | Passed |
|---|---:|
| `tests/test_phase4_2.py tests/test_phase4_2_ui.py` | 48 |
| `tests/test_phase4.py tests/test_phase4_2.py tests/test_phase4_2_ui.py` | 62 |
| `tests/test_phase3.py` | 11 |
| `tests/test_phase2.py` | 4 |
| complete suite | **188** |

Final: **188 passed, 0 failed, 0 skipped** (the original 140 plus 48 new tests).
`compileall`, `pip check` and `git diff --check` also pass. UI test collection
reports two upstream deprecation warnings in the pinned Starlette/AnyIO/httpx
stack; no warnings are suppressed.

Tests include three full cycles on one Case, unchanged-evidence idempotency,
contradictory/irrelevant responses, stale submissions, retired-task audit,
legacy adoption, source-time vs receipt-time, timezone/equal-time ordering,
late-failure rollback both with and without caller transactions, concurrent
completion/evaluation using independent SQLite connections, foreign follow-up
references, explicit UI evaluation, real operator forms and escaped errors.

Adversarial tests exposed and drove production fixes for non-decision task
completion, timezone ordering, and UUID-based equal-time ordering. Running Phase
3 in isolation also exposed a pre-existing import-path dependency; pytest
configuration fixes collection without changing its tests.

## Boundaries / next work

- This is not a full follow-up scheduler, courier ingestion integration or
  autonomous execution phase. Existing manually dated follow-ups remain intact.
- Evidence added outside operational task completion requires explicit Case
  evaluation (UI command or domain API). There is no hidden GET mutation or
  background worker. Stale bound-task submissions are still guarded immediately.
- The legacy CSV import cannot distinguish repeated identical undated source
  rows; it retains its documented dedupe contract. A genuinely distinct source
  event must already be present in the event store to start a distinct cycle.
- Normal repeated cycles retain the same **OPEN** Case. Explicitly RESOLVED or
  ABANDONED Cases are not silently replaced or automatically reopened; their
  existing lifecycle contract is retained.
- Existing text-based refusal routing is intentionally not expanded into a new
  taxonomy. It is not general language understanding or adjudication of truth.
  Customer statements remain evidence of what the customer said, and action
  decisions remain human.
- The UI remains the original local, unauthenticated harness, not a public
  multi-tenant deployment. No external request is sent by recording a decision.
