"""Commerce Ops v0 — Slice 5: deterministic WhatsApp drafts + terminal outcomes.

Part A: copy-ready draft TEMPLATES only. The operator has already made the
decision; the generator translates (intent, tracking_no, explicit text) into
plain text. Deterministic, never sent, no DB mutation. Only the three intents
justified by evidence are supported:
  - reattempt request        [E: "REATTEMPT KARWAYN" pattern, WORKFLOW.md §5]
  - fake-attempt reattempt   [E: "FAKE ATTEMPT THA REATTEMPT KARWAYN" — CSV
                              SAMPLE_022/023 verbatim]
  - delivery-status query    [E: "YE KB DEL HOGA ?" pattern]

Part B: thin domain APIs over the EXISTING operator_action kinds
(delivered_confirmed / cancel_decided / returned_confirmed) and the EXISTING
derivation. No new states, no new rules.
"""
import sqlite3

from commerceops.core import utcnow, refresh_state, derive_state
from commerceops.actions import (
    record_operator_action, ShipmentNotFoundError, ValidationError,
    _check_shipment,
)

# ---------------------------------------------------------------------------
# Part A — drafts (pure functions; zero DB access)
# ---------------------------------------------------------------------------

DRAFT_INTENTS = ("reattempt", "fake_attempt_reattempt", "delivery_status_query")


def generate_draft(intent: str, tracking_no: str) -> str:
    """Return copy-ready plain text for an explicitly chosen intent.

    - tracking_no is preserved exactly as given (never normalized).
    - Pure string formatting: same inputs always produce identical output.
    - Raises ValidationError on unsupported intents or blank tracking_no.
    - Never touches the database; nothing is ever sent automatically.
    """
    if not (tracking_no or "").strip():
        raise ValidationError("draft requires a non-empty tracking number")
    if intent == "reattempt":
        # evidence pattern: "<tracking> REATTEMPT KARWAYN"
        return f"{tracking_no}\nREATTEMPT KARWAYN"
    if intent == "fake_attempt_reattempt":
        # evidence: CSV SAMPLE_022/023 verbatim "FAKE ATTEMPT THA REATTEMPT KARWAYN"
        return f"{tracking_no}\nFAKE ATTEMPT THA REATTEMPT KARWAYN"
    if intent == "delivery_status_query":
        # evidence pattern: "<tracking> YE KB DEL HOGA ?"
        return f"{tracking_no}\nYE KB DEL HOGA ?"
    raise ValidationError(
        f"unsupported draft intent {intent!r}; supported: {DRAFT_INTENTS}"
    )


# ---------------------------------------------------------------------------
# Part B — terminal outcomes (thin wrappers over existing action model)
# ---------------------------------------------------------------------------

def _validate_terminal_timestamp(conn, shipment_id: str, acted_at: str, label: str):
    """C2 fix: a terminal action whose supplied acted_at would NOT become the
    derived state (because a newer event outranks it) is REJECTED with an
    explicit error instead of silently no-oping. The user's timestamp is never
    rewritten."""
    if acted_at is None:
        return  # API default = now; _next_timestamp guarantees monotonicity
    from commerceops.core import derive_state
    latest = conn.execute(
        "SELECT MAX(ts) AS m FROM ("
        " SELECT acted_at AS ts FROM operator_action WHERE shipment_id=?"
        " UNION ALL SELECT COALESCE(occurred_at, imported_at) FROM tracking_event WHERE shipment_id=?"
        " UNION ALL SELECT confirmed_at FROM customer_confirmation WHERE shipment_id=?)",
        (shipment_id, shipment_id, shipment_id),
    ).fetchone()["m"]
    if latest is not None and acted_at < latest:
        raise ValidationError(
            f"{label} rejected: supplied acted_at {acted_at!r} predates a newer "
            f"event ({latest!r}); the terminal outcome would not take effect. "
            "Supply the current time or correct the history explicitly first."
        )


def mark_delivered(conn, shipment_id: str, actor: str = "laiba",
                   note: str = None, acted_at: str = None):
    """Explicit human confirmation of delivery -> CLOSED_DELIVERED via the
    existing derivation. No courier observation can produce this state.
    Back-dated acted_at values that would silently no-op are rejected (C2)."""
    _check_shipment(conn, shipment_id)
    _validate_terminal_timestamp(conn, shipment_id, acted_at, "mark_delivered")
    return record_operator_action(
        conn, shipment_id, kind="delivered_confirmed",
        note=note, actor=actor, acted_at=acted_at,
    )


def mark_cancelled(conn, shipment_id: str, cancel_reason: str,
                   actor: str = "laiba", note: str = None,
                   acted_at: str = None):
    """Explicit human cancellation. Reason REQUIRED, preserved verbatim.
    Never inferred from courier codes. Back-dated acted_at that would
    silently no-op is rejected (C2)."""
    _check_shipment(conn, shipment_id)
    _validate_terminal_timestamp(conn, shipment_id, acted_at, "mark_cancelled")
    return record_operator_action(
        conn, shipment_id, kind="cancel_decided",
        cancel_reason=cancel_reason, note=note, actor=actor, acted_at=acted_at,
    )


def mark_returned(conn, shipment_id: str, actor: str = "laiba",
                  note: str = None, acted_at: str = None):
    """Explicit human/operator confirmation that the parcel returned to shipper
    -> RETURNED via the existing derivation. Back-dated acted_at that would
    silently no-op is rejected (C2)."""
    _check_shipment(conn, shipment_id)
    _validate_terminal_timestamp(conn, shipment_id, acted_at, "mark_returned")
    return record_operator_action(
        conn, shipment_id, kind="returned_confirmed",
        note=note, actor=actor, acted_at=acted_at,
    )
