"""Commerce Ops v0 — Slice 1 core: schema, import, state derivation, work queue.

Implements docs/SPEC_V0.md exactly. No APIs, no AI, no SLA logic, no
cross-courier normalization. All courier remarks are observations, never
ground truth.
"""
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from commerceops.timestamps import timestamp_key, shipment_timestamp_issues

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batch (
  id            TEXT PRIMARY KEY,
  imported_at   TEXT NOT NULL,
  source_label  TEXT,
  row_count     INTEGER,
  duplicate_row_count INTEGER,
  raw_source_text TEXT
);

CREATE TABLE IF NOT EXISTS shipment (
  id             TEXT PRIMARY KEY,
  tracking_no    TEXT NOT NULL UNIQUE,
  courier        TEXT,
  customer_phone TEXT,
  current_state  TEXT NOT NULL DEFAULT 'NEW',
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracking_event (
  id           TEXT PRIMARY KEY,
  shipment_id  TEXT NOT NULL REFERENCES shipment(id),
  batch_id     TEXT REFERENCES import_batch(id),
  raw_code     TEXT NOT NULL,
  raw_text     TEXT,
  occurred_at  TEXT,
  imported_at  TEXT NOT NULL,
  event_key    TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS operator_action (
  id            TEXT PRIMARY KEY,
  shipment_id   TEXT NOT NULL REFERENCES shipment(id),
  kind          TEXT NOT NULL CHECK (kind IN ('note','query_sent','reattempt_requested',
                'open_allowed','cancel_decided','delivered_confirmed','returned_confirmed')),
  note          TEXT,
  cancel_reason TEXT,
  actor         TEXT NOT NULL DEFAULT 'laiba',
  acted_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_confirmation (
  id           TEXT PRIMARY KEY,
  shipment_id  TEXT NOT NULL REFERENCES shipment(id),
  channel      TEXT,
  content      TEXT NOT NULL,
  confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS follow_up (
  id          TEXT PRIMARY KEY,
  shipment_id TEXT NOT NULL REFERENCES shipment(id),
  reason      TEXT NOT NULL,
  due_at      TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
  created_at  TEXT NOT NULL,
  closed_at   TEXT,
  closed_by_action TEXT
);

-- Slice-1 additions (smallest reversible, reported):
-- quarantine_row holds unparseable import lines so nothing is silently dropped.
CREATE TABLE IF NOT EXISTS quarantine_row (
  id          TEXT PRIMARY KEY,
  batch_id    TEXT NOT NULL REFERENCES import_batch(id),
  line_no     INTEGER,
  line_text   TEXT NOT NULL,
  reason      TEXT
);

-- import_conflict holds conflicting incoming shipment attributes verbatim;
-- no keep-first authority: existing values are kept as-is, incoming values
-- are recorded here and surfaced, never discarded.
CREATE TABLE IF NOT EXISTS import_conflict (
  id          TEXT PRIMARY KEY,
  batch_id    TEXT NOT NULL REFERENCES import_batch(id),
  shipment_id TEXT NOT NULL REFERENCES shipment(id),
  line_no     INTEGER,
  field       TEXT NOT NULL,
  incoming_value TEXT NOT NULL,
  detected_at TEXT NOT NULL
);

-- Case Engine persistence layer (Phase 1)
-- case_entity represents an exception lifecycle associated with a shipment
CREATE TABLE IF NOT EXISTS case_entity (
  id             TEXT PRIMARY KEY,
  shipment_id    TEXT NOT NULL REFERENCES shipment(id),
  status         TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'RESOLVED', 'ABANDONED')),
  opened_at      TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  latest_evidence_at TEXT NOT NULL,
  resolution     TEXT,  -- JSON text
  policy_version INTEGER NOT NULL DEFAULT 1
);

-- Append-only lifecycle audit. Three scalar cursors identify evidence already
-- present at closure; this is not a duplicate evidence/context snapshot.
CREATE TABLE IF NOT EXISTS case_status_event (
  id TEXT PRIMARY KEY,
  case_entity_id TEXT NOT NULL REFERENCES case_entity(id),
  from_status TEXT NOT NULL,
  to_status TEXT NOT NULL,
  changed_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  tracking_cursor INTEGER,
  customer_cursor INTEGER,
  operator_cursor INTEGER,
  resolution TEXT
);
CREATE INDEX IF NOT EXISTS idx_case_status_event_case ON case_status_event(case_entity_id);

-- human_task represents a request for human capability/evidence/action
CREATE TABLE IF NOT EXISTS human_task (
  id             TEXT PRIMARY KEY,
  case_entity_id TEXT NOT NULL REFERENCES case_entity(id),
  type           TEXT NOT NULL CHECK (type IN ('VERIFY_CUSTOMER', 'DECIDE_ACTION', 'FOLLOW_UP_ACTION')),
  capability     TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED')),
  payload        TEXT,  -- JSON
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  completed_at   TEXT
);

-- autonomous_action represents a proposed or executed system action
CREATE TABLE IF NOT EXISTS autonomous_action (
  id             TEXT PRIMARY KEY,
  case_entity_id TEXT NOT NULL REFERENCES case_entity(id),
  action_type    TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'PLANNED' CHECK (status IN ('PLANNED', 'EXECUTED', 'FAILED')),
  payload        TEXT,  -- JSON
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  executed_at    TEXT,
  failure_info   TEXT  -- JSON or text for error details
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_case_entity_shipment ON case_entity(shipment_id);
CREATE INDEX IF NOT EXISTS idx_case_entity_status ON case_entity(status);
CREATE INDEX IF NOT EXISTS idx_human_task_case ON human_task(case_entity_id);
CREATE INDEX IF NOT EXISTS idx_human_task_status ON human_task(status);
CREATE INDEX IF NOT EXISTS idx_autonomous_action_case ON autonomous_action(case_entity_id);
CREATE INDEX IF NOT EXISTS idx_autonomous_action_status ON autonomous_action(status);
"""

TERMINAL_KINDS = {
    "cancel_decided": "CLOSED_CANCELLED",
    "delivered_confirmed": "CLOSED_DELIVERED",
    "returned_confirmed": "RETURNED",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# State derivation — SPEC_V0.md §B, deterministic, no business rules.
# ---------------------------------------------------------------------------

def derive_state(conn: sqlite3.Connection, shipment_id: str) -> str:
    """Pure function over the append-only event tables (audit §B).

    1. Newest event overall is a terminal human action -> terminal state.
    2. Else newest event overall is a tracking_event -> NEEDS_ACTION,
       else ACTION_TAKEN.
    3. Else -> NEW.
    """
    if shipment_timestamp_issues(conn, shipment_id):
        # Unknown chronology cannot establish terminal truth. Keep this shipment
        # visible for explicit source-data review instead of crashing the queue.
        return "NEEDS_ACTION"
    events = []
    for r in conn.execute(
        "SELECT rowid AS ord, kind AS code, acted_at AS at FROM operator_action WHERE shipment_id=?",
        (shipment_id,),
    ):
        events.append(("operator_action", r["code"], r["at"], r["ord"]))
    for r in conn.execute(
        "SELECT rowid AS ord, id, COALESCE(occurred_at, imported_at) AS at FROM tracking_event WHERE shipment_id=?",
        (shipment_id,),
    ):
        events.append(("tracking_event", r["id"], r["at"], r["ord"]))
    # customer_confirmation would count as an operator-side event; Slice 1
    # records none, but included for completeness of the specified derivation.
    for r in conn.execute(
        "SELECT rowid AS ord, confirmed_at AS at FROM customer_confirmation WHERE shipment_id=?",
        (shipment_id,),
    ):
        events.append(("customer_confirmation", None, r["at"], r["ord"]))
    if not events:
        return "NEW"
    # At the newest instant, rowids establish append order only within a
    # source. Cross-source ties cannot prove a terminal decision came later.
    newest_at = max(timestamp_key(e[2]) for e in events)
    newest = {}
    for event in events:
        if timestamp_key(event[2]) == newest_at:
            prior = newest.get(event[0])
            if prior is None or event[3] > prior[3]:
                newest[event[0]] = event
    operator = newest.get("operator_action")
    if operator and operator[1] in TERMINAL_KINDS:
        if "tracking_event" in newest:
            return "NEEDS_ACTION"
        if "customer_confirmation" not in newest:
            return TERMINAL_KINDS[operator[1]]
    # A nonterminal human action is still evidence of action taken, including
    # legacy CSV status/notes received alongside a claim. It is NOT resolution
    # of the Case requirement; policy separately requires a later decision.
    if operator or "customer_confirmation" in newest:
        return "ACTION_TAKEN"
    return "NEEDS_ACTION"


def refresh_state(conn: sqlite3.Connection, shipment_id: str) -> str:
    new_state = derive_state(conn, shipment_id)
    conn.execute(
        "UPDATE shipment SET current_state=?, updated_at=? WHERE id=?",
        (new_state, utcnow(), shipment_id),
    )
    return new_state


# ---------------------------------------------------------------------------
# Import — legacy operator format [E]: tracking_no, postex_remark
# [, our_remark] [, status]. Optional customer_phone column supported for
# conflict-handling tests; unknown extra columns are ignored, not guessed.
# ---------------------------------------------------------------------------

LEGACY_COLUMNS = ["tracking_no", "postex_remark", "our_remark", "status"]


def parse_csv(text: str):
    """Parse legacy operator format. Returns (rows, errors).

    rows: list of dicts with line_no + known columns.
    errors: list of (line_no, line_text, reason) for quarantine.
    Header row is optional; recognized case-insensitively.
    """
    lines = text.splitlines()
    rows, errors = [], []
    header_checked = False
    for i, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        cells = next(iter(__import__("csv").reader([line])))
        cells = [c.strip() for c in cells]
        # Only the FIRST non-empty line may be a header. A later line whose
        # first cell looks like a column label is DATA, never silently dropped.
        if not header_checked:
            header_checked = True
            lowered_first = [c.lower().replace(" ", "_") for c in cells]
            if lowered_first[:1] == ["tracking_no"]:
                continue
        if len(cells) < 2 or not cells[0] or not cells[1].strip().strip("'\"").strip():
            # validation sees through wrapping quotes so a whitespace-only
            # remark is quarantined; raw_code itself is still stored verbatim.
            errors.append((i, raw_line, "missing tracking_no or postex_remark"))
            continue
        row = {"line_no": i}
        for idx, col in enumerate(LEGACY_COLUMNS):
            row[col] = cells[idx] if idx < len(cells) else None
        # tolerate an optional trailing customer_phone in position 5
        row["customer_phone"] = cells[4] if len(cells) > 4 else None
        rows.append(row)
    return rows, errors


def _event_key(tracking_no, raw_code, raw_text, occurred_at):
    """SPEC §C: hash(tracking_no, raw_code, occurred_at, raw_text).

    When the source provides no timestamp, occurred_at participates as None so
    the key is stable across re-imports of identical source data (idempotency).
    """
    material = "\x1f".join([tracking_no, raw_code, raw_text or "", occurred_at or ""])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def import_source(conn: sqlite3.Connection, text: str, source_label: str = None):
    """Import pasted/uploaded legacy-format text. Idempotent and atomic.

    Atomicity (audit fix): the entire import runs in one transaction — a
    failure mid-import leaves NO partial shipments/events/batches behind.
    """
    batch_id = uuid.uuid4().hex  # uniqueness never depends on clock granularity
    imported_at = utcnow()
    rows, errors = parse_csv(text)
    # raw line text per line_no for quarantine records (verbatim preservation)
    line_text_by_no = {}
    _ln = 0
    for _raw in text.splitlines():
        _ln += 1
        if _raw.strip():
            line_text_by_no[_ln] = _raw
    rows_raw = {r["line_no"]: line_text_by_no.get(r["line_no"], "") for r in rows}

    quarantined = 0
    duplicates = 0
    conflicts = []
    shipments_created = 0
    events_created = 0
    actions_created = 0

    try:
        conn.execute("SAVEPOINT import_source")
        conn.execute(
            "INSERT INTO import_batch (id, imported_at, source_label, row_count, duplicate_row_count, raw_source_text)"
            " VALUES (?,?,?,?,?,?)",
            (batch_id, imported_at, source_label, 0, 0, text),
        )

        for line_no, line_text, reason in errors:
            conn.execute(
                "INSERT INTO quarantine_row (id, batch_id, line_no, line_text, reason) VALUES (?,?,?,?,?)",
                (hashlib.sha256(f"{batch_id}:{line_no}".encode()).hexdigest()[:32], batch_id, line_no, line_text, reason),
            )
            quarantined += 1

        for row in rows:
            tno = row["tracking_no"]
            cur = conn.execute("SELECT * FROM shipment WHERE tracking_no=?", (tno,)).fetchone()
            if cur is None:
                sid = hashlib.sha256(f"ship:{tno}".encode()).hexdigest()[:32]
                conn.execute(
                    "INSERT INTO shipment (id, tracking_no, courier, customer_phone, current_state, created_at, updated_at)"
                    " VALUES (?,?,?,?, 'NEW', ?, ?)",
                    (sid, tno, "postex", row.get("customer_phone"), imported_at, imported_at),
                )
                shipments_created += 1
                sid_new = sid
            else:
                sid_new = cur["id"]
                incoming_phone = row.get("customer_phone")
                if incoming_phone and cur["customer_phone"] and incoming_phone != cur["customer_phone"]:
                    # never overwrite, never discard — record verbatim (§I case 3)
                    conn.execute(
                        "INSERT INTO import_conflict (id, batch_id, shipment_id, line_no, field, incoming_value, detected_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (
                            hashlib.sha256(f"{batch_id}:{row['line_no']}:phone".encode()).hexdigest()[:32],
                            batch_id, sid_new, row["line_no"], "customer_phone", incoming_phone, imported_at,
                        ),
                    )
                    conflicts.append({"tracking_no": tno, "field": "customer_phone", "incoming_value": incoming_phone})

            ek = _event_key(tno, row["postex_remark"], None, None)
            try:
                conn.execute(
                    "INSERT INTO tracking_event (id, shipment_id, batch_id, raw_code, raw_text, occurred_at, imported_at, event_key)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        hashlib.sha256(f"evt:{ek}".encode()).hexdigest()[:32],
                        sid_new, batch_id,
                        row["postex_remark"],           # raw_code verbatim, never translated
                        None,                           # courier text only; our_remark is OPERATOR data
                        None,                           # source provides no timestamp [U]
                        imported_at, ek,
                    ),
                )
                events_created += 1
            except sqlite3.IntegrityError:
                duplicates += 1  # identical observation already present

            # C1 fix — legacy operator attribution (SPEC §D):
            #   our_remark -> operator_action(kind='note')  [operator interpretation]
            #   status REATTEMPT -> reattempt_requested; CANCEL -> cancel_decided
            # The courier tracking_event carries ONLY the courier's remark.
            our_remark = (row.get("our_remark") or "").strip()
            legacy_status = (row.get("status") or "").strip()
            if our_remark:
                note_ek = f"note:{_event_key(tno, 'our_remark', our_remark, None)}"
                try:
                    conn.execute(
                        "INSERT INTO operator_action (id, shipment_id, kind, note, actor, acted_at)"
                        " VALUES (?,?,?,?,?,?)",
                        (
                            hashlib.sha256(f"act:{note_ek}".encode()).hexdigest()[:32],
                            sid_new, "note", our_remark, "laiba", imported_at,
                        ),
                    )
                    actions_created += 1
                except sqlite3.IntegrityError:
                    pass  # identical legacy note already imported (idempotent)
            if legacy_status:
                mapped_kind = {"REATTEMPT": "reattempt_requested", "CANCEL": "cancel_decided"}.get(legacy_status.upper())
                if mapped_kind is None:
                    # unknown legacy value: quarantine the line rather than guess
                    conn.execute(
                        "INSERT INTO quarantine_row (id, batch_id, line_no, line_text, reason) VALUES (?,?,?,?,?)",
                        (
                            hashlib.sha256(f"{batch_id}:{row['line_no']}:status".encode()).hexdigest()[:32],
                            batch_id, row["line_no"], rows_raw.get(row["line_no"], ""), f"unmapped legacy status {legacy_status!r}",
                        ),
                    )
                    quarantined += 1
                else:
                    cancel_reason = our_remark if mapped_kind == "cancel_decided" else None
                    st_ek = f"status:{_event_key(tno, mapped_kind, legacy_status, None)}"
                    try:
                        conn.execute(
                            "INSERT INTO operator_action (id, shipment_id, kind, note, cancel_reason, actor, acted_at)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (
                                hashlib.sha256(f"act:{st_ek}".encode()).hexdigest()[:32],
                                sid_new, mapped_kind, None, cancel_reason, "laiba", imported_at,
                            ),
                        )
                        actions_created += 1
                    except sqlite3.IntegrityError:
                        pass  # idempotent

            refresh_state(conn, sid_new)

        conn.execute(
            "UPDATE import_batch SET row_count=?, duplicate_row_count=? WHERE id=?",
            (len(rows), duplicates, batch_id),
        )
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT import_source")
        conn.execute("RELEASE SAVEPOINT import_source")
        raise
    conn.execute("RELEASE SAVEPOINT import_source")
    return {
        "batch_id": batch_id,
        "rows_parsed": len(rows),
        "quarantined": quarantined,
        "duplicates": duplicates,
        "conflicts": conflicts,
        "shipments_created": shipments_created,
        "events_created": events_created,
        "actions_created": actions_created,
    }


# ---------------------------------------------------------------------------
# Work queue — C3 fix: ONE authoritative implementation lives in
# commerceops.followups.work_queue (fresh derivation + follow-up integration).
# This entry point delegates to it for backward compatibility; the two can
# no longer disagree.
# ---------------------------------------------------------------------------

def work_queue(conn: sqlite3.Connection, now: str = None):
    """Authoritative queue = followups.work_queue (fresh derivation, overdue
    human-set follow-ups, one entry per shipment with reasons). Kept as a
    delegating alias so existing callers/tests keep working."""
    from commerceops import followups as _followups
    return _followups.work_queue(conn, now=now)
