# Commerce Ops v0 — Implementation Specification

**Status**: Implementation-ready (post-audit; four audit items resolved — see §B state derivation, §D format classes + verbatim retention, §I case 3).
**Supersedes**: `docs/WORKFLOW.md` (kept as analysis reference only).
**Inputs reconciled**: `docs/evidence/sample_cases.csv`, `docs/evidence/Excel.pdf/Laiba Excel.pdf.pdf`, WhatsApp screenshot, WORKFLOW.md, Ox Alpha adversarial review.

---

## 0. EVIDENCE LABELS

Every design decision below is tagged:

- **[E]** directly evidenced (visible in CSV/PDF/screenshot/user context)
- **[I]** strongly inferred (follows from evidence without invention)
- **[U]** currently unknown (system must tolerate, never assume)

Standing epistemic rules (inherited from WORKFLOW.md, validated by review):
1. Courier remarks are observations/claims, never ground truth. [E]
2. Operator interpretation must remain distinguishable from courier claims. [E]
3. Human decisions stay human: fake-attempt judgment, cancel vs reattempt, customer-contact outcomes. [E/I]
4. No invented courier codes, APIs, SLAs, or deadline rules. [rule]

Evidence facts the design must honor:
- One dated review batch ("UNDER REVIEW", 15/8/26) — operator works in batches. [E]
- Duplicate tracking number exists in source data (29221080025518 ×2). [E]
- Operator groups by concern (FAKE ATTEMPTS / CANCEL labels). [E]
- Observed remark codes (PostEx-shaped only): INA, RFD, CNA, OPN, HOLD, PAYMENT NOT AVAILABLE, RESTRICTED AREA. [E]
- Dispositions observed: REATTEMPT, CANCEL, with free-text notes in Urdu/English mix. [E]
- DELIVERED and RETURNED remarks: zero samples. [U]

---

## A. REVISED DOMAIN MODEL

Six entities. Nothing more.

| Entity | Purpose | Tag |
|---|---|---|
| Shipment | Identity + current derived position | [I] |
| TrackingEvent | One courier observation per row (append-only) | [E] |
| OperatorAction | What the operator did/note/concluded (append-only) | [E] |
| CustomerConfirmation | What the customer actually confirmed (append-only) | [I] — evidenced implicitly by "ADDRESS PROVIDED", "NH CHAIYE" |
| FollowUp | Human-set scheduled attention | [I] — "UNDER REVIEW" date implies time tracking |
| ImportBatch | A dated paste/import session | [E] — PDF is literally one |

Deliberately absent (per review): Query entity (a query is an OperatorAction), Deadline/ReturnRisk entities ([U], no rule known), OperatorInterpretation table (folds into OperatorAction with kind=note).

Relationships:
- Shipment 1—N TrackingEvent
- Shipment 1—N OperatorAction
- Shipment 1—N CustomerConfirmation
- Shipment 1—N FollowUp
- ImportBatch 1—N TrackingEvent

## B. REVISED STATE/EVENT MODEL

Shipment carries exactly one mutable field: `current_state`. Everything else is events.

States:
```
NEW              imported or manually added; no problematic observation yet
NEEDS_ACTION     latest relevant event is an unresolved courier problem
ACTION_TAKEN     operator acted; waiting on next courier information
CLOSED_DELIVERED terminal
CLOSED_CANCELLED terminal (human-only)
RETURNED         terminal (parcel back with shipper) [outcome type U; state kept because return risk is real context]
```

Events (all append-only, timestamped, attributed):
```
courier_observation   {raw_code, raw_text, source, batch_id}
operator_action       {kind: note | query_sent | reattempt_requested |
                       open_allowed | cancel_decided | delivered_confirmed |
                       returned_confirmed, note}
customer_confirmation {channel, content}
state_change          {from_state, to_state, reason}   (derived/logged)
follow_up             {created | done | cancelled}
```

Transitions:
```
NEW          → NEEDS_ACTION      on courier_observation with unresolved problem code
NEEDS_ACTION → ACTION_TAKEN      on operator_action (query_sent, reattempt_requested, …)
ACTION_TAKEN → NEEDS_ACTION      on new unresolved courier_observation   ← THE LOOP
any non-terminal → CLOSED_DELIVERED   via human action "mark delivered"
any non-terminal → CLOSED_CANCELLED   via human action "cancel" + reason + confirm
any non-terminal → RETURNED           via human action "mark returned"
```

Not states (correcting WORKFLOW.md): DELIVERY_ATTEMPT, EXCEPTION, QUERY_SENT, AWAITING_FOLLOWUP, REATTEMPT_QUEUED are events/actions, not states. ESCALATE, RESOLVED, UNRESOLVED: removed entirely — invented.

**Deterministic state derivation** (audit addition): `current_state` is persisted ONLY as a cache of this pure function over the append-only event tables; it is never hand-editable and never changed by import logic:

```
1. If the latest terminal-producing operator_action for the shipment is
   cancel_decided / delivered_confirmed / returned_confirmed AND no
   courier_observation or operator_action of any kind is newer:
       → the corresponding CLOSED_CANCELLED / CLOSED_DELIVERED / RETURNED.
2. Else if the shipment has ≥1 tracking_event:
       → NEEDS_ACTION if the newest event overall is a tracking_event,
         else ACTION_TAKEN (newest event overall is an operator_action,
         customer_confirmation, or follow_up record).
3. Else → NEW.
```

Only three human actions can produce a terminal state: cancel_decided (with reason + confirmation), delivered_confirmed, returned_confirmed. No import, timer, queue action, or automation ever writes current_state directly; recompute happens solely on insert of a new event. Unknown codes follow rule 2 like any other observation (→ NEEDS_ACTION). No business rule about which codes are "problems" is needed — any fresh courier observation outranks the operator's last action, by design of the loop.

Unknown-code handling: any unrecognized raw_code still creates a courier_observation and forces NEEDS_ACTION. Never auto-mapped, never ignored.

## C. DATABASE SCHEMA

```sql
import_batch (
  id            uuid pk,
  imported_at   timestamptz not null default now(),
  source_label  text,                    -- e.g. "portal dump 15/8"
  row_count     int,
  duplicate_row_count int,                -- rows skipped as duplicates of this import
  raw_source_text text                     -- exact pasted/uploaded text; audit addition
)

shipment (
  id             uuid pk,
  tracking_no    text not null unique,   -- dedup key
  courier        text,                   -- free label, default 'postex'; no taxonomy enforced
  customer_phone text,
  current_state  text not null default 'NEW',
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null
)

tracking_event (
  id           uuid pk,
  shipment_id  uuid fk -> shipment,
  batch_id     uuid fk -> import_batch null,     -- null if manual entry
  raw_code     text not null,                    -- verbatim, never translated
  raw_text     text,                             -- full remark text if any
  occurred_at  timestamptz,                      -- if source provides; else = imported_at
  imported_at  timestamptz not null default now(),
  event_key    text not null,                    -- hash(tracking_no, raw_code, occurred_at, raw_text) for idempotency
  unique(event_key)
)

operator_action (
  id          uuid pk,
  shipment_id uuid fk -> shipment,
  kind        text not null check (kind in ('note','query_sent','reattempt_requested',
                        'open_allowed','cancel_decided','delivered_confirmed','returned_confirmed')),
  note        text,                              -- interpretation / free text, e.g. 'FAKE ATTEMPT THA REATTEMPT KARWAYN'
  cancel_reason text,                            -- required when kind='cancel_decided'
  actor       text not null default 'laiba',     -- single-operator v0
  acted_at    timestamptz not null default now()
)

customer_confirmation (
  id           uuid pk,
  shipment_id  uuid fk -> shipment,
  channel      text,                              -- 'call' | 'whatsapp' (free text allowed)
  content      text not null,
  confirmed_at timestamptz not null default now()
)

follow_up (
  id          uuid pk,
  shipment_id uuid fk -> shipment,
  reason      text not null,
  due_at      date not null,                     -- HUMAN-set; system never computes deadlines
  status      text not null default 'open' check (status in ('open','done','cancelled')),
  created_at  timestamptz not null default now(),
  closed_at   timestamptz,
  closed_by_action uuid fk -> operator_action null
)
```

Rules:
- All event tables append-only. No UPDATE/DELETE paths in v0 except follow_up.status. [E: history preservation is the point]
- `shipment.current_state` is derivable from events; treat it as a cache, recomputed on each new event. Never hand-editable.
- No generated columns, no triggers beyond updated_at — keep it dumb and inspectable.

## D. IMPORT FORMAT

v0 input = paste or CSV upload, matching the Excel/PDF layout we actually have evidence for. [E: PDF/CSV shape]

**Two distinct format classes** (audit clarification — do not conflate):
1. **Legacy operator format [E]**: `tracking_no, postex_remark [, our_remark] [, status]` — this is what sample_cases.csv and the Laiba PDF contain. v0's import mapping targets THIS format only.
2. **Raw courier portal export format [U]**: whatever TCS/Leopards/M&P/PostEx portals actually emit on copy/export (columns, date formats, code casing) is UNKNOWN. v0 does not assume it. When such a dump is pasted, rows that don't match format 1 go to quarantine for human column-mapping; nothing is guessed.
- `our_remark` / `status` from legacy Excel rows are ingested as: our_remark → operator_action(kind='note'), status REATTEMPT → operator_action(kind='reattempt_requested'); status CANCEL → operator_action(kind='cancel_decided', cancel_reason=text). This preserves existing history without inventing semantics. [I]
- **Verbatim legacy retention** (audit addition): each import_batch stores `raw_source_text` (the exact pasted/uploaded text) and each quarantined-or-mapped row keeps its original line string. Structured interpretation (note/reattempt_requested/cancel_decided) is derived FROM the source text and never replaces it — the literal "REATTEMPT"/"CANCEL" strings and full our_remark text remain queryable forever. [audit]
- Unparseable rows go to a quarantine list displayed after import; nothing silently dropped. [I]
- Import is idempotent via tracking_event.event_key. Re-pasting the same dump adds nothing. [I — duplicate row in PDF proves dedupe is needed]

No Shopify integration. No portal scraping/APIs in v0.

## E. WORK QUEUE BEHAVIOR

The queue answers exactly one question: **which shipments need Laiba right now?**

Items appear when shipment is in NEEDS_ACTION, plus:
- any non-terminal shipment with an open overdue FollowUp
- any non-terminal shipment where days-since-last-event exceeds a *displayed* threshold (aging counter shown; no SLA claimed, no auto-escalation) [I]

Sort: oldest last-event first. [I]

Each queue item shows: tracking_no, raw_code verbatim, age (days since last event), open follow-up flag, prior fake-attempt flag (presence of matching operator note), last operator note snippet. Grouping by concern (fake attempts / cancels) is available as a filter — mirrors her PDF groupings. [E]

Queue NEVER: changes state, sends anything, cancels, or hides an item because a query was sent (it shows "query already sent ✓" instead). [I]

## F. SHIPMENT DETAIL / TIMELINE BEHAVIOR

One page per shipment, strictly chronological merged timeline:
courier observations (labeled "COURIER CLAIM"), operator actions ("OPERATOR"), customer confirmations ("CUSTOMER CONFIRMED"), state changes, follow-ups. Source attribution on every line — this enforces the epistemic layering at UI level. [E→UI]

Header: current_state, age, courier, phone, open follow-ups, fake-attempt-ever flag.

Actions available on the page (each writes an immutable record):
- Add note (interpretation)
- Mark query sent
- Mark reattempt requested → generates copy-ready message text (below)
- Record customer confirmation
- Set follow-up (human picks date + reason)
- Cancel… requires explicit reason + confirmation dialog [E: cancel has reasons like NH CHAIYE / SELF COLLECT]
- Mark delivered / mark returned

Copy-ready messages: static templates filled by lookup (tracking no, remark, note). Not AI-generated. [review-corrected]

## G. FOLLOW-UP BEHAVIOR

- Created only by human: due_at (date) + reason.
- Appears in queue once due (or before due, flagged upcoming).
- Closed by: any subsequent operator_action on the shipment, or explicitly.
- Repeats allowed: new follow-up rows; history retained. No recurrence engine.
- System does NOT compute return-risk deadlines. It shows "days since first problem observation" and "days since last event". Deadline automation waits until the rule is learned. [U]

## H. ACTION/AUDIT MODEL

- Every mutation of operational meaning is an insert into an event table with actor + timestamp.
- Cancellation additionally stores cancel_reason (required, non-empty).
- current_state transitions are logged as state_change records with reason.
- There is no delete. Corrections happen by adding a new note/action, never by editing history. [I]

## I. DUPLICATE HANDLING

Three distinct cases (the evidence contains #1):
1. Same tracking_no twice in ONE import → single shipment; second row becomes a tracking_event only if event_key differs; identical → skipped, counted in batch.duplicate_row_count. [E-driven]
2. Same tracking_no across imports → same shipment; new observation appended if event_key differs. [I]
3. Conflicting data for same tracking_no (different phone etc.) → keep the existing row's values as-is, store every incoming conflicting value verbatim on the import record, and surface a conflict notice in the UI. NO source value is ever silently discarded or overwritten; resolution is an explicit human edit recorded as an operator_action note. [audit: no keep-first authority rule exists in evidence]

## J. UNKNOWN/UNSUPPORTED DATA HANDLING

- Unknown remark code/text: stored verbatim, shipment → NEEDS_ACTION, flagged "UNRECOGNIZED CODE" in queue/detail. Never guessed, never normalized. [rule]
- Missing fields (no phone, no date): stored null; features degrade visibly (no dialer link), never defaulted to invented values. [I]
- Quarantined import rows listed post-import for manual fix. [I]
- Delivered/returned courier formats unknown → v0 records these outcomes via human action only. [U acknowledged]

## K. MVP ACCEPTANCE CRITERIA

1. Paste/import a CSV in format D → shipments + observations created; re-import same file creates zero duplicates.
2. The PDF's 25 rows import cleanly; SAMPLE_022/023 carry their fake-attempt text as operator notes; the duplicated real-world row collapses to one shipment with count reported.
3. New problematic remark on an ACTION_TAKEN shipment moves it back to NEEDS_ACTION (loop works).
4. Queue lists every NEEDS_ACTION shipment + overdue follow-ups; sorted oldest-first; shows courier claim separately from operator notes.
5. Shipment detail shows complete attributed timeline; nothing editable, append-only.
6. Cancel requires reason + confirmation; produces cancel_decided record; cannot be triggered by import or automation.
7. Delivered/Returned recorded via human action only.
8. Follow-up with human-set date surfaces in queue when due.
9. Unknown code appears verbatim with UNRECOGNIZED flag; system does not crash or misclassify.
10. No network calls to Shopify/couriers exist in the codebase.
11. Full-history survival test: run two complete cycles on one shipment → all events from both cycles visible.

## L. IMPLEMENTATION ORDER

1. Schema + migrations (Section C)
2. Import pipeline: parse → dedupe → quarantine report (D, I)
3. State derivation logic (B)
4. Work queue read model (E)
5. Shipment detail/timeline + action recording (F, H)
6. Follow-ups (G)
7. Message templates + copy buttons (F)
8. Acceptance tests (K) as they map to steps 2–7

Steps 1–4 yield a usable "what needs attention" tool even before rich detail pages.

---

## DECISION-EVIDENCE SUMMARY

| Decision | Tag |
|---|---|
| Remarks as events, layered attribution | E |
| Dedupe on tracking_no | E (dup row in PDF) |
| Dated import batches | E ("UNDER REVIEW", 15/8/26) |
| Concern-group filters (fake/cancel) | E (PDF labels) |
| Manual import, no APIs | E (current workflow is manual) |
| Append-only history | I |
| 5+1 state machine | I |
| Follow-up entity w/ human dates | I |
| Quarantine + conflict notices | I |
| RETURNED state shape | I (outcome mechanics U) |
| Delivered/return remark formats | U |
| Return-risk deadline rule | U |
| Cross-courier taxonomies | U (deferred) |
