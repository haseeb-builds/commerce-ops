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
from commerceops.actions import (
    ShipmentNotFoundError, ValidationError, _check_shipment, _next_timestamp,
)

VALID_STATUSES = ("open", "done", "cancelled")


class FollowUpNotFoundError(Exception):
    """Raised when completing/referencing an unknown follow-up."""


def create_follow_up(conn, shipment_id: str, due_at: str, reason: str):
    """Create one human-set follow-up. Append-only; duplicates preserved."""
    _check_shipment(conn, shipment_id)
    if not (due_at or "").strip():
        raise ValidationError("follow-up requires a due_at (human-set)")
    if not (reason or "").strip():
        raise ValidationError("follow-up requires a non-empty reason")
    fid = uuid.uuid4().hex  # uniqueness never depends on clock granularity
    if conn.in_transaction:
        conn.commit()  # close any pending implicit transaction before ours
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO follow_up (id, shipment_id, reason, due_at, status, created_at)"
            " VALUES (?,?,?,?, 'open', ?)",
            (fid, shipment_id, reason, due_at, utcnow()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return fid


def complete_follow_up(conn, follow_up_id: str):
    """Mark one follow-up done. Rejects already-closed follow-ups rather than
    rewriting history. Original values remain untouched."""
    row = conn.execute(
        "SELECT * FROM follow_up WHERE id=?", (follow_up_id,)
    ).fetchone()
    if row is None:
        raise FollowUpNotFoundError(f"no follow-up with id {follow_up_id!r}")
    if row["status"] != "open":
        raise ValidationError(
            f"follow-up {follow_up_id!r} is already {row['status']}; cannot complete twice"
        )
    if conn.in_transaction:
        conn.commit()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE follow_up SET status='done', closed_at=? WHERE id=?",
            (utcnow(), follow_up_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
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
    out = {"open": [], "overdue": [], "upcoming": [], "closed": []}
    for r in rows:
        if r["status"] != "open":
            out["closed"].append(r)
        elif r["due_at"] <= now:
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
    items = {}
    for s in conn.execute("SELECT * FROM shipment"):
        reasons = []
        latest = None
        # SPEC §C: current_state column is a CACHE; the queue must evaluate
        # the FRESH derivation so out-of-band writers can't hide shipments.
        if derive_state(conn, s["id"]) == "NEEDS_ACTION":
            ev = conn.execute(
                "SELECT raw_code, COALESCE(occurred_at, imported_at) AS at FROM tracking_event"
                " WHERE shipment_id=? ORDER BY COALESCE(occurred_at, imported_at) DESC, rowid DESC LIMIT 1",
                (s["id"],),
            ).fetchone()
            reasons.append("needs_action")
            if ev:
                latest = ev["at"]
        fu = conn.execute(
            "SELECT COUNT(*) c FROM follow_up WHERE shipment_id=? AND status='open' AND due_at<=?",
            (s["id"], now),
        ).fetchone()["c"]
        if fu:
            reasons.append("overdue_follow_up")
            open_fu = conn.execute(
                "SELECT MAX(due_at) m FROM follow_up WHERE shipment_id=? AND status='open'",
                (s["id"],),
            ).fetchone()
        if not reasons:
            continue
        items[s["id"]] = {
            "tracking_no": s["tracking_no"],
            "latest_raw_code": None,
            "last_event_at": latest,
            "reasons": reasons,
            "overdue_follow_up_count": fu,
        }
        if fu and items[s["id"]]["last_event_at"] is None:
            # no courier events; order by oldest open follow-up instead
            items[s["id"]]["last_event_at"] = open_fu["m"]
    result = [v for v in items.values()]
    result.sort(key=lambda x: x["last_event_at"] or "")
    return result
