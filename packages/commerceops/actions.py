"""Commerce Ops v0 — Slice 3: operator actions, notes, customer confirmations.

Write paths for the human layer. All records are append-only inserts into the
existing SPEC §C tables; nothing here interprets courier codes and nothing
converts interpretation into courier truth.

Fake-attempt judgment is an OPERATOR NOTE (kind='note') — never a courier
status, never a separate business action type (SPEC §A: interpretation folds
into OperatorAction with kind=note; §B rule 3: human decisions stay human).

"address provided" from the evidence ("ADDRESS PROVIDED, REATTEMPT") is
likewise recorded as a note (optionally alongside query_sent); it is not a
separate enum kind because SPEC's CHECK constraint defines the kinds.
"""
import hashlib
import sqlite3
import uuid
from datetime import timedelta
from commerceops.timestamps import timestamp_key, parse_timestamp, display_order

from commerceops.core import utcnow, refresh_state
from commerceops.transactions import atomic

VALID_ACTION_KINDS = (
    "note", "query_sent", "reattempt_requested", "open_allowed",
    "cancel_decided", "delivered_confirmed", "returned_confirmed",
)


class ShipmentNotFoundError(Exception):
    """Raised when an action/confirmation references an unknown shipment."""


class ValidationError(Exception):
    """Raised when a record would violate its schema constraints or be empty."""


def _next_timestamp(conn, shipment_id: str, requested: str = None) -> str:
    """Preserve a valid supplied timestamp; otherwise allocate a logical successor.

    Defaults use now or one microsecond after the newest known stored instant.
    Source offsets/strings are never rewritten. Unknown legacy instants are not
    guessed here: terminal/task entry points separately block uncertain history.
    """
    if requested is not None:
        if parse_timestamp(requested) is None:
            raise ValidationError("Invalid timestamp; supply an ISO date/time with the source offset")
        return requested
    candidate = utcnow()
    rows = conn.execute(
        "SELECT ts FROM ("
        " SELECT acted_at AS ts FROM operator_action WHERE shipment_id=?"
        " UNION ALL SELECT COALESCE(occurred_at, imported_at) AS ts FROM tracking_event WHERE shipment_id=?"
        " UNION ALL SELECT imported_at AS ts FROM tracking_event WHERE shipment_id=?"
        " UNION ALL SELECT confirmed_at AS ts FROM customer_confirmation WHERE shipment_id=?)",
        (shipment_id, shipment_id, shipment_id, shipment_id),
    ).fetchall()

    latest = max((parsed for row in rows if (parsed := parse_timestamp(row["ts"])) is not None), default=None)
    if latest is not None and timestamp_key(candidate) <= latest:
        candidate = (latest + timedelta(microseconds=1)).isoformat()
    return candidate


def _check_shipment(conn, shipment_id):
    row = conn.execute(
        "SELECT id FROM shipment WHERE id=?", (shipment_id,)
    ).fetchone()
    if row is None:
        raise ShipmentNotFoundError(f"no shipment with id {shipment_id!r}")


def record_operator_action(conn, shipment_id: str, kind: str,
                           note: str = None, cancel_reason: str = None,
                           actor: str = "laiba", acted_at: str = None):
    """Append one OperatorAction record. Append-only; never overwrites.

    Rules (SPEC §C/§H):
    - cancel_decided requires a non-empty cancel_reason.
    - note requires non-empty note text (an empty interpretation is not a record).
    - other kinds may carry optional free-text note.
    Runs inside a transaction; refreshes current_state via Slice 1 derivation.
    """
    with atomic(conn):
        _check_shipment(conn, shipment_id)
        if kind not in VALID_ACTION_KINDS:
            raise ValidationError(
                f"unknown action kind {kind!r}; valid kinds: {VALID_ACTION_KINDS}"
            )
        if kind == "cancel_decided" and not (cancel_reason or "").strip():
            raise ValidationError("cancel_decided requires non-empty cancel_reason")
        if kind == "note" and not (note or "").strip():
            raise ValidationError("operator note requires non-empty note text")

        aid = uuid.uuid4().hex  # uniqueness never depends on clock granularity
        acted_at = _next_timestamp(conn, shipment_id, acted_at)
        conn.execute(
            "INSERT INTO operator_action (id, shipment_id, kind, note, cancel_reason, actor, acted_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (aid, shipment_id, kind, note, cancel_reason, actor, acted_at),
        )
        refresh_state(conn, shipment_id)
        return aid


def record_customer_confirmation(conn, shipment_id: str, content: str,
                                 channel: str = None, confirmed_at: str = None):
    """Append one CustomerConfirmation record — what the customer REPORTED,
    entered verbatim by the operator. Never generated, never auto-verified;
    the timeline labels it CUSTOMER CONFIRMED per SPEC §F."""
    with atomic(conn):
        _check_shipment(conn, shipment_id)
        if not (content or "").strip():
            raise ValidationError("customer confirmation requires non-empty content")
        cid = uuid.uuid4().hex  # uniqueness never depends on clock granularity
        confirmed_at = _next_timestamp(conn, shipment_id, confirmed_at)
        conn.execute(
            "INSERT INTO customer_confirmation (id, shipment_id, channel, content, confirmed_at)"
            " VALUES (?,?,?,?,?)",
            (cid, shipment_id, channel, content, confirmed_at),
        )
        refresh_state(conn, shipment_id)
        return cid


def list_operator_actions(conn, shipment_id: str):
    _check_shipment(conn, shipment_id)
    rows = conn.execute(
        "SELECT rowid AS record_order, * FROM operator_action WHERE shipment_id=?", (shipment_id,)
    ).fetchall()
    return sorted(rows, key=lambda row: (display_order(row["acted_at"]), row["record_order"]))


def list_customer_confirmations(conn, shipment_id: str):
    _check_shipment(conn, shipment_id)
    rows = conn.execute(
        "SELECT rowid AS record_order, * FROM customer_confirmation WHERE shipment_id=?", (shipment_id,)
    ).fetchall()
    return sorted(rows, key=lambda row: (display_order(row["confirmed_at"]), row["record_order"]))
