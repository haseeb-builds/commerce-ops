# Commerce Ops — Workflow Analysis

**Source**: `docs/evidence/sample_cases.csv` (25 rows) + operational context provided by user

---

## 1. DATA MODEL

### Columns in Evidence

| Column | Role | Description |
|--------|------|-------------|
| `tracking_no` | Identifier | Unique shipment identifier (SAMPLE_001–SAMPLE_025) |
| `postex_remark` | **Courier remark** | Raw status code/remark from PostEx/courier portal (TCS, Leopards, M&P, PostEx) |
| `our_remark` | **Operator interpretation + action** | Human operator's reading of the courier remark, decision, and instruction combined |
| `status` | **Resulting operational status** | Final disposition for this cycle: `REATTEMPT` or `CANCEL` |

### Semantic Distinctions

| Layer | Example | Who produces it | Truth value |
|-------|---------|-----------------|-------------|
| **Courier remark** | `RFD`, `INA`, `OPN`, `RESTRICTED AREA` | Courier portal / rider | May be inaccurate ("fake attempt") |
| **Operator interpretation** | "FAKE ATTEMPT", "ADDRESS PROVIDED" | Human operator | Based on experience, WhatsApp groups, customer calls |
| **Operator action** | "REATTEMPT", "ALLOW TO OPEN", "CANCEL" | Human operator | Decision made by operator |
| **Resulting status** | `REATTEMPT`, `CANCEL` | System/Excel | What actually happens next |

**Critical rule**: Courier remarks are NOT treated as ground truth. Operator explicitly flags "FAKE ATTEMPT" for two RFD cases (SAMPLE_022, 023).

---

## 2. OBSERVABLE PATTERNS

### From CSV Evidence Only

| Courier Remark | Operator Remark Pattern | Resulting Status | Count |
|----------------|------------------------|------------------|-------|
| `INA` | "ADDRESS PROVIDED, REATTEMPT" | REATTEMPT | 3 |
| `RFD` | "REATTEMPT" | REATTEMPT | 9 |
| `RFD` | "REATTEMPT, ALLOW TO OPEN" | REATTEMPT | 1 |
| `RFD` | "FAKE ATTEMPT THA REATTEMPT KARWAYN" | REATTEMPT | 2 |
| `RFD` | "CANCEL (NH CHAIYE)" | CANCEL | 1 |
| `OPN` | "REATTEMPT, ALLOW TO OPEN" | REATTEMPT | 3 |
| `CNA` | "REATTEMPT" | REATTEMPT | 2 |
| `HOLD` | "REATTEMPT" | REATTEMPT | 1 |
| `PAYMENT NOT AVAILABLE` | "REATTEMPT" | REATTEMPT | 1 |
| `RESTRICTED AREA` | "CANCEL (SELF COLLECT NH KRENGY)" | CANCEL | 1 |

### Pattern Summary (Evidence-Backed)

1. **INA → address provided → reattempt** (3/3 cases)
2. **RFD → reattempt** (9/12 RFD cases)
3. **RFD → "allow to open" + reattempt** (1/12 RFD cases)
4. **RFD → "fake attempt" → reattempt** (2/12 RFD cases)
5. **RFD → cancel (customer doesn't want)** (1/12 RFD cases)
6. **OPN → reattempt + allow to open** (3/3 cases)
7. **CNA → reattempt** (2/2 cases)
8. **RESTRICTED AREA → cancel (self-collect not possible)** (1/1 case)
9. **HOLD / PAYMENT NOT AVAILABLE → reattempt** (1 each)

**NOT universal rules** — these are observed frequencies in 25 samples.

---

## 3. WORKFLOW RECONSTRUCTION

### Core Event → Interpretation → Action → Follow-up → Outcome

```
NEW SHIPMENT
    │
    ▼
COURIER PORTAL CHECK (TCS/Leopards/M&P/PostEx)
    │
    ▼
COURIER REMARK RECEIVED
    │
    ├── INA (Incomplete Address)
    │     │
    │     ▼
    │  OPERATOR: Call customer / WhatsApp / check Excel
    │     │
    │     ├── Address obtained → "ADDRESS PROVIDED, REATTEMPT" → REATTEMPT
    │     └── Address not obtained → (UNKNOWN from evidence)
    │
    ├── RFD (Refused/Returned)
    │     │
    │     ▼
    │  OPERATOR: Assess — real refusal vs fake attempt?
    │     │         Check: customer call, WhatsApp group, past history
    │     │
    │     ├── Real refusal → "REATTEMPT" (9 cases) or "CANCEL (NH CHAIYE)" (1 case)
    │     ├── Fake attempt detected → "FAKE ATTEMPT THA REATTEMPT KARWAYN" → REATTEMPT (2 cases)
    │     └── Allow to open → "REATTEMPT, ALLOW TO OPEN" → REATTEMPT (1 case)
    │
    ├── OPN (Open/Allow to Open)
    │     │
    │     ▼
    │  OPERATOR: "REATTEMPT, ALLOW TO OPEN" → REATTEMPT (3 cases)
    │
    ├── CNA (Customer Not Available)
    │     │
    │     ▼
    │  OPERATOR: "REATTEMPT" → REATTEMPT (2 cases)
    │
    ├── HOLD
    │     │
    │     ▼
    │  OPERATOR: "REATTEMPT" → REATTEMPT (1 case)
    │
    ├── PAYMENT NOT AVAILABLE
    │     │
    │     ▼
    │  OPERATOR: "REATTEMPT" → REATTEMPT (1 case)
    │
    └── RESTRICTED AREA
          │
          ▼
       OPERATOR: "CANCEL (SELF COLLECT NH KRENGY)" → CANCEL (1 case)
```

### Follow-Up Loop (Repeated Work)

```
REATTEMPT status assigned
    │
    ▼
SCHEDULE REATTEMPT (via courier portal / WhatsApp group)
    │
    ▼
WAIT for next courier update
    │
    ▼
NEW COURIER REMARK → back to COURIER REMARK RECEIVED
    │
    ▼
... repeat until DELIVERED or CANCEL
```

---

## 4. EXCEPTION TAXONOMY (Evidence-Only)

| Exception Type | Courier Remark | Operator Response | Evidence |
|----------------|----------------|-------------------|----------|
| **Fake delivery attempt** | RFD | Detect via customer call/WhatsApp → force reattempt | SAMPLE_022, 023 |
| **Restricted/non-service area** | RESTRICTED AREA | Cancel — self-collect not feasible | SAMPLE_024 |
| **Customer refusal (genuine)** | RFD | Cancel — "NH CHAIYE" (don't want) | SAMPLE_025 |
| **Address incomplete** | INA | Obtain address → reattempt | SAMPLE_001, 004, 007 |
| **Customer unavailable** | CNA | Reattempt | SAMPLE_009, 021 |
| **Payment issue (COD)** | PAYMENT NOT AVAILABLE | Reattempt | SAMPLE_008 |
| **Hold** | HOLD | Reattempt | SAMPLE_019 |
| **Allow to open** | OPN / RFD | Reattempt + allow open | SAMPLE_002, 010, 013, 014 |

---

## 5. HUMAN WORK REQUIRED (Per Evidence + Context)

| Step | Current Method | Tools Used |
|------|----------------|------------|
| Check courier portal for latest remark | Manual login to TCS/Leopards/M&P/PostEx | Browser, multiple portals |
| Copy tracking numbers & remarks | Copy-paste | Excel, clipboard |
| Maintain shipment list | Excel spreadsheet | Excel |
| Interpret ambiguous remarks | Human judgment + WhatsApp group | WhatsApp, memory |
| Detect fake attempts | Call customer / check WhatsApp group | Phone, WhatsApp |
| Contact customer for address/confirmation | Phone call / WhatsApp | Phone, WhatsApp |
| Request reattempt from courier | WhatsApp operational group / portal | WhatsApp, portal |
| Remember previous actions on same shipment | Memory / Excel history | Excel, memory |
| Decide cancel vs reattempt | Human judgment | — |
| Track return-risk deadlines | Memory / Excel | Excel, memory |
| Consolidate info across couriers | Manual | Excel |

---

## 6. REPEATED WORK (Critical)

**Same shipment → multiple intervention cycles**

Evidence shows:
- 23/25 samples end in `REATTEMPT` → each will re-enter the workflow
- RFD appears 12 times — most lead to reattempt → will generate another remark
- Operator must **remember**: "did I already call this customer?", "was this a fake attempt before?", "how many reattempts?"

**Repeated per-shipment actions**:
1. Portal check (every cycle)
2. Interpretation (every cycle)
3. Customer contact (potentially every cycle)
4. WhatsApp group coordination (every reattempt)
5. Excel update (every cycle)
6. Decision: reattempt vs cancel (every cycle)

---

## 7. PRELIMINARY STATE MACHINE

```
                    ┌─────────────────────┐
                    │   UNPROCESSED       │  (new order imported)
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  DELIVERY_ATTEMPT   │  (courier shows remark)
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
    ┌─────────────────┐ ┌─────────────┐ ┌──────────────┐
    │   EXCEPTION     │ │  REATTEMPT  │ │    CANCEL    │
    │   (INA/CNA/     │ │   QUEUED    │ │  (RESTRICTED │
    │    RFD/HOLD/    │ │             │ │   AREA /     │
    │    PAYMENT/OPN) │ │             │ │   REFUSAL)   │
    └────────┬────────┘ └──────┬──────┘ └──────┬───────┘
             │                 │                │
             ▼                 │                │
    ┌─────────────────┐        │                │
    │  QUERY_SENT     │        │                │
    │ (call/WhatsApp/ │        │                │
    │  address fix)   │        │                │
    └────────┬────────┘        │                │
             │                 │                │
             ▼                 │                │
    ┌─────────────────┐        │                │
    │ AWAITING_       │        │                │
    │ FOLLOWUP        │        │                │
    └────────┬────────┘        │                │
             │                 │                │
     ┌───────┴───────┐         │                │
     ▼               ▼         │                │
┌─────────┐    ┌──────────┐   │                │
│ RESOLVED│    │ UNRESOLVED│  │                │
│ (addr   │    │ (no      │  │                │
│  found) │    │  contact)│  │                │
└────┬────┘    └────┬─────┘  │                │
     │              │        │                │
     ▼              ▼        ▼                ▼
┌──────────┐  ┌──────────┐ ┌──────────┐ ┌──────────┐
│ REATTEMPT│  │ ESCALATE │ │ REATTEMPT│ │  FINAL   │
│ (re-queue│  │ (manual) │ │ (re-queue│ │  CANCEL  │
│  to      │  │          │ │  to      │ │          │
│  courier)│  │          │ │  courier)│ │          │
└────┬─────┘  └──────────┘ └────┬─────┘ └──────────┘
     │                           │
     └───────────────┬───────────┘
                     ▼
            ┌─────────────────┐
            │  DELIVERED      │  (terminal — not in evidence)
            └─────────────────┘
```

**Uncertain states** (not directly evidenced):
- `DELIVERED` — never appears in CSV (all samples are mid-flow)
- `ESCALATE` — inferred for unresolved exceptions
- `QUERY_SENT` / `AWAITING_FOLLOWUP` — inferred from operator actions

---

## 8. AUTOMATION OPPORTUNITIES

| Operation | Classification | Rationale |
|-----------|----------------|-----------|
| Import/normalize shipment data from Shopify/PostEx | **A — deterministic** | Structured API/CSV → standard schema |
| Detect new exception (remark changed) | **A — deterministic** | Diff previous vs current remark |
| Maintain shipment state machine | **A — deterministic** | Rule-based transitions from evidence |
| Track whether query already sent | **A — deterministic** | Boolean flag per shipment per exception type |
| Identify shipments requiring follow-up | **A — deterministic** | Query: state ∈ {AWAITING_FOLLOWUP, REATTEMPT_QUEUED} + overdue |
| Generate WhatsApp query drafts | **B — AI-assisted** | Template + context (remark, history, customer phone) |
| Maintain operational history (audit log) | **A — deterministic** | Append-only log of every event |
| Detect return-risk deadlines | **B — AI-assisted** | Needs business rule: "X days since first attempt" — not in evidence |
| Consolidate courier info across portals | **B — AI-assisted** | Normalize different courier codes → common taxonomy |
| Identify contradictory claims (courier vs customer) | **C — human decision** | Requires judgment: "fake attempt" detection needs customer call |
| Decide reattempt vs cancel | **C — human decision** | Business judgment: "NH CHAIYE", restricted area nuance |
| Detect fake attempts automatically | **D — insufficient evidence** | Only 2 samples; needs customer confirmation pattern |
| Predict which RFD are fake | **D — insufficient evidence** | No features in evidence (time, rider, location) |

**Key principle**: Do NOT classify as AI just because AI *could* do it. Classify as AI only when:
- Deterministic logic is insufficient
- Human provides the ground truth pattern
- Evidence shows variability requiring judgment

---

## 9. PRODUCT CORE — Smallest Useful System

**Problem**: Operator manually checks 4+ courier portals, copies to Excel, interprets remarks, coordinates via WhatsApp, calls customers, tracks reattempts from memory.

**Smallest system that reduces workload**:

```
┌─────────────────────────────────────────────────────────────┐
│  SHIPMENT TRACKER (single-page dashboard)                   │
├─────────────────────────────────────────────────────────────┤
│  1. INGEST                                                  │
│     - Pull orders from Shopify (API)                        │
│     - Pull tracking + remarks from PostEx (API)             │
│     - Normalize courier codes → common taxonomy (INA/RFD/   │
│       CNA/OPN/HOLD/RESTRICTED/PAYMENT)                      │
│                                                              │
│  2. STATE MACHINE                                           │
│     - Auto-transition on new remark                         │
│     - Track: UNPROCESSED → DELIVERY_ATTEMPT → EXCEPTION     │
│       → QUERY_SENT → AWAITING_FOLLOWUP → REATTEMPT_QUEUED   │
│       → (loop) → DELIVERED / CANCEL                         │
│     - Persist every transition (audit log)                  │
│                                                              │
│  3. WORK QUEUE                                              │
│     - "Needs Action" view:                                  │
│       • EXCEPTION awaiting query (INA: call for address)    │
│       • AWAITING_FOLLOWUP overdue (>24h)                    │
│       • REATTEMPT_QUEUED not yet confirmed by courier       │
│       • Return-risk approaching (configurable days)         │
│                                                              │
│  4. ACTION HELPERS (per shipment)                           │
│     - "Call Customer" → opens dialer with phone + context   │
│     - "Draft WhatsApp Query" → pre-filled template          │
│     - "Request Reattempt" → one-click to PostEx API         │
│     - "Mark Fake Attempt" → flags + forces reattempt        │
│     - "Cancel" → reason selector (restricted/refusal/other) │
│                                                              │
│  5. HISTORY PANEL                                           │
│     - Full timeline: courier remarks, operator actions,     │
│       customer responses, reattempts                        │
│     - Shows "previous fake attempt" flag                    │
└─────────────────────────────────────────────────────────────┘
```

**What this eliminates**:
- Manual portal checking (ingest via API)
- Copy-paste to Excel (central state)
- Remembering history (audit log)
- "Did I already query this?" (state machine)
- Drafting WhatsApp from scratch (templates)
- Tracking reattempt confirmations (queue)

**What stays human**:
- Calling customers
- Judging fake attempts
- Deciding cancel vs reattempt on edge cases
- WhatsApp group coordination (for now)

---

## 10. CRITICAL UNKNOWNS

| Unknown | Why It Matters |
|---------|----------------|
| **Full courier code taxonomy** | CSV shows 7 codes; real portals have 20+ |
| **Return-risk deadline rules** | "Return risk" mentioned but no day counts in evidence |
| **Reattempt confirmation flow** | How does operator know courier accepted reattempt? |
| **Fake attempt detection signals** | Only 2 samples; need pattern (rider, time, location?) |
| **Customer contact success rate** | How often does "address provided" actually work? |
| **Multi-courier mapping** | TCS/Leopards/M&P codes → common taxonomy? |
| **Shopify → PostEx sync frequency** | Real-time? Batch? Manual trigger? |
| **WhatsApp group structure** | One group per courier? Per region? Per operator? |
| **COD payment flow** | "PAYMENT NOT AVAILABLE" → what resolution? |
| **Delivered state** | Zero samples — what does courier send? |
| **Bulk operations** | Does operator process 10/50/100 at once? |
| **Role separation** | One operator does all? Or split (caller vs tracker)? |

---

## FACTS
- 25 sample rows, 4 columns: tracking_no, postex_remark, our_remark, status
- 7 distinct courier remarks observed: INA, RFD, CNA, OPN, HOLD, PAYMENT NOT AVAILABLE, RESTRICTED AREA
- 2 final statuses: REATTEMPT (23), CANCEL (2)
- RFD is most frequent remark (12/25)
- Operator explicitly identifies "FAKE ATTEMPT" for 2 RFD cases
- Operator adds "ALLOW TO OPEN" for OPN and 1 RFD
- RESTRICTED AREA → CANCEL with note "SELF COLLECT NH KRENGY"
- One RFD → CANCEL with "NH CHAIYE" (customer doesn't want)
- All INA → "ADDRESS PROVIDED, REATTEMPT"
- All CNA, HOLD, PAYMENT NOT AVAILABLE → REATTEMPT

## INFERENCES
- Courier remarks are not trusted blindly; operator verifies via customer/WhatsApp
- "REATTEMPT" status means "schedule another delivery attempt with courier"
- Workflow is cyclic: REATTEMPT → wait → new remark → repeat
- Operator maintains mental/excel history of each shipment's previous cycles
- WhatsApp groups are used for courier coordination (reattempt requests)
- Return-risk is a time-bound concern not visible in CSV
- "ALLOW TO OPEN" suggests customer can inspect before accepting (COD-related?)

## UNKNOWNS
- Complete courier remark taxonomy across TCS, Leopards, M&P, PostEx
- Exact reattempt confirmation mechanism (portal? WhatsApp? callback?)
- Return-risk deadline (days since first attempt? since last attempt?)
- Fake attempt detection heuristics beyond customer denial
- Delivered state courier remark
- Bulk vs single shipment processing mode
- Whether multiple operators share the Excel/WhatsApp
- Shopify → PostEx integration details (API? manual export?)
- COD payment resolution workflow
- Escalation path for unresolved exceptions

## HIGH-VALUE AUTOMATION OPPORTUNITIES
1. **Unified ingest** — Single dashboard replacing 4+ portal logins (A)
2. **Auto state machine** — Eliminates "what state is this shipment?" memory work (A)
3. **Work queue** — Shows only actionable items, sorted by urgency (A)
4. **History timeline** — Replaces Excel scrolling + memory (A)
5. **WhatsApp draft generator** — Cuts copy-paste for repetitive queries (B)
6. **Reattempt one-click** — Replaces WhatsApp group coordination for standard cases (A)
7. **Fake attempt flag** — Persists "this was fake before" across cycles (A)
8. **Return-risk alert** — Configurable deadline warning (B)

## PRELIMINARY STATE MACHINE
```
UNPROCESSED
  → DELIVERY_ATTEMPT (courier remark received)
    → EXCEPTION (INA/CNA/RFD/HOLD/PAYMENT/OPN/RESTRICTED)
      → QUERY_SENT (operator contacts customer/courier)
        → AWAITING_FOLLOWUP (waiting for response)
          → RESOLVED → REATTEMPT_QUEUED → (courier accepts) → DELIVERY_ATTEMPT (loop)
          → UNRESOLVED → ESCALATE / CANCEL
    → CANCEL (RESTRICTED AREA / genuine refusal)
  → DELIVERED (terminal — not observed)
```

## SMALLEST USEFUL PRODUCT
**Shipment Tracker Dashboard** with:
1. Automated ingest from Shopify + PostEx (API)
2. Normalized courier remark taxonomy
3. Deterministic state machine with audit log
4. Actionable work queue (filter: needs action, overdue, return-risk)
5. Per-shipment history panel
6. Action buttons: Call, Draft WhatsApp, Request Reattempt, Mark Fake, Cancel
7. No AI agents, no marketplace, no generic platform — just the operator's Excel + WhatsApp + portals unified