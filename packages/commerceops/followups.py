"""Commerce Ops v0 — Slice 4: human-set follow-ups + work-queue integration.

All follow-up timing is human-set (SPEC §G: "Created only by human: due_at
(date) + reason"). The system never infers deadlines from courier codes,
shipper advice, or elapsed time — it only REPORTS whether an existing
human-set due_at has passed. That is a clock comparison, not an SLA.

Statuses are exactly SPEC's: open | done | cancelled. Completion sets
status='done' and stamps closed_at; original reason/due_at/created_at are
never rewritten. Follow-ups are never deduplicated — two identical human
creations remain two distinct rows.
"""
import hashlib
import sqlite3
import uuid

from commerceops.core import utcnow, refresh_state
from commerceops.transactions import atomic
from commerceops.timestamps import parse_timestamp, display_order, shipment_timestamp_issues
from commerceops.actions import (
    ShipmentNotFoundError, ValidationError, _check_shipment, _next_timestamp,
)

VALID_STATUSES = ("open", "done", "cancelled")


class FollowUpNotFoundError(Exception):
    """Raised when completing/referencing an unknown follow-up."""


def create_follow_up(conn, shipment_id: str, due_at: str, reason: str):
    """Create one human-set follow-up. Append-only; duplicates preserved."""
    with atomic(conn):
        _check_shipment(conn, shipment_id)
        if not (due_at or "").strip():
            raise ValidationError("follow-up requires a due_at (human-set)")
        if not (reason or "").strip():
            raise ValidationError("follow-up requires a non-empty reason")
        if parse_timestamp(due_at) is None:
            raise ValidationError("Invalid follow-up due_at timestamp")
        fid = uuid.uuid4().hex  # uniqueness never depends on clock granularity
        conn.execute(
            "INSERT INTO follow_up (id, shipment_id, reason, due_at, status, created_at)"
            " VALUES (?,?,?,?, 'open', ?)",
            (fid, shipment_id, reason, due_at, utcnow()),
        )
        return fid


def complete_follow_up(conn, follow_up_id: str, *, completed_at: str = None):
    """Mark one follow-up done. Rejects already-closed follow-ups rather than
    rewriting history. Original values remain untouched."""
    with atomic(conn):
        row = conn.execute(
            "SELECT * FROM follow_up WHERE id=?", (follow_up_id,)
        ).fetchone()
        if row is None:
            raise FollowUpNotFoundError(f"no follow-up with id {follow_up_id!r}")
        if row["status"] != "open":
            raise ValidationError(
                f"follow-up {follow_up_id!r} is already {row['status']}; cannot complete twice"
            )
        if completed_at is not None and parse_timestamp(completed_at) is None:
            raise ValidationError("Invalid follow-up completion timestamp")
        conn.execute(
            "UPDATE follow_up SET status='done', closed_at=? WHERE id=?",
            (completed_at or utcnow(), follow_up_id),
        )
        return row  # pre-update record returned for reference


def list_follow_ups(conn, shipment_id: str = None, now: str = None):
    """List follow-ups partitioned by clock state. `now` defaults to real UTC;
    tests pass deterministic timestamps. Overdue = human-set due_at < now AND
    still open. This is a clock comparison, NOT an SLA evaluation."""
    now = now or utcnow()
    query = (
        "SELECT f.*, s.tracking_no FROM follow_up f JOIN shipment s ON s.id=f.shipment_id"
    )
    params = []
    if shipment_id is not None:
        _check_shipment(conn, shipment_id)
        query += " WHERE f.shipment_id=?"
        params.append(shipment_id)
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    out = {"open": [], "overdue": [], "upcoming": [], "closed": [], "invalid": []}
    for r in sorted(rows, key=lambda row: display_order(row["due_at"])):
        if r["status"] != "open":
            out["closed"].append(r)
        elif parse_timestamp(r["due_at"]) is None:
            r["timestamp_warning"] = True
            out["invalid"].append(r)
            out["open"].append(r)
        elif parse_timestamp(r["due_at"]) <= parse_timestamp(now):
            out["overdue"].append(r)
            out["open"].append(r)
        else:
            out["upcoming"].append(r)
            out["open"].append(r)
    return out


def work_queue(conn, now: str = None):
    """Slice 4 read model. Surfaces exactly:
      A. shipments whose derived state is NEEDS_ACTION
      B. shipments with overdue open follow-ups
    A shipment satisfying both appears once with both reasons.
    NEVER mutates state; an overdue follow-up does not change current_state.
    """
    from commerceops.core import derive_state
    now = now or utcnow()
    items = []
    for shipment in conn.execute("SELECT * FROM shipment"):
        sid = shipment["id"]
        issues = shipment_timestamp_issues(conn, sid)
        reasons = []
        if derive_state(conn, sid) == "NEEDS_ACTION":
            reasons.append("needs_action")
        if issues:
            reasons.append("timestamp_review")
        events = conn.execute(
            "SELECT rowid AS ord, raw_code, COALESCE(occurred_at,imported_at) AS at "
            "FROM tracking_event WHERE shipment_id=?", (sid,)).fetchall()
        latest = max(events, key=lambda row: (display_order(row["at"]), row["ord"]), default=None)
        fus = list_follow_ups(conn, shipment_id=sid, now=now)
        if fus["overdue"]:
            reasons.append("overdue_follow_up")
        if fus["invalid"] and "timestamp_review" not in reasons:
            reasons.append("timestamp_review")
        if not reasons:
            continue
        oldest_due = min((f["due_at"] for f in fus["open"]), key=display_order, default=None)
        items.append({
            "tracking_no": shipment["tracking_no"],
            "latest_raw_code": latest["raw_code"] if latest else None,
            "last_event_at": latest["at"] if latest else oldest_due,
            "reasons": reasons,
            "overdue_follow_up_count": len(fus["overdue"]),
            "timestamp_issues": issues,
        })
    items.sort(key=lambda item: display_order(item["last_event_at"]))
    return items
