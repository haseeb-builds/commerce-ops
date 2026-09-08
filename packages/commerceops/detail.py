"""Commerce Ops v0 — Slice 2: shipment detail + chronological timeline.

Read model over the existing Slice 1 schema. No new business rules, no code
interpretation, no AI. Every timeline entry carries an explicit source label
so courier claims are never presented as customer truth (SPEC §F).
"""
from datetime import datetime

# Epistemic labels per SPEC §F — display-only, never stored back.
SOURCE_LABELS = {
    "tracking_event": "COURIER CLAIM",
    "operator_action": "OPERATOR",
    "customer_confirmation": "CUSTOMER CONFIRMED",
    "follow_up": "FOLLOW-UP RECORD",
    "state_change": "STATE CHANGE",
}

# operator_action kinds rendered verbatim; no kind is interpreted further.
ACTION_KINDS = {
    "note": "OPERATOR NOTE",
    "query_sent": "OPERATOR: QUERY SENT",
    "reattempt_requested": "OPERATOR: REATTEMPT REQUESTED",
    "open_allowed": "OPERATOR: ALLOW TO OPEN",
    "cancel_decided": "OPERATOR: CANCEL DECIDED",
    "delivered_confirmed": "OPERATOR: DELIVERED CONFIRMED",
    "returned_confirmed": "OPERATOR: RETURNED CONFIRMED",
}


def get_shipment_by_tracking(conn, tracking_no: str):
    """Lookup by tracking number. Returns shipment row or None."""
    return conn.execute(
        "SELECT * FROM shipment WHERE tracking_no=?", (tracking_no,)
    ).fetchone()


def _sort_key(entry):
    """Same deterministic semantics as Slice 1 derive_state:
    timestamp first, insertion order (rowid) as tie-break."""
    return (entry["at"], entry["ord"])


def build_timeline(conn, shipment_id: str):
    """Complete chronological event history for one shipment.

    Oldest-first. Equal timestamps ordered by insertion (later insert = newer),
    exactly matching Slice 1's derivation tie-break.
    """
    entries = []
    for r in conn.execute(
        "SELECT rowid AS ord, id, raw_code, raw_text, occurred_at, imported_at,"
        " batch_id FROM tracking_event WHERE shipment_id=?",
        (shipment_id,),
    ):
        entries.append({
            "type": "tracking_event",
            "label": SOURCE_LABELS["tracking_event"],
            "at": r["occurred_at"] or r["imported_at"],
            "occurred_at_source_provided": r["occurred_at"] is not None,
            "ord": r["ord"],
            "raw_code": r["raw_code"],       # verbatim
            "raw_text": r["raw_text"],       # verbatim
            "batch_id": r["batch_id"],
            "event_id": r["id"],
        })
    for r in conn.execute(
        "SELECT rowid AS ord, kind, note, cancel_reason, actor, acted_at"
        " FROM operator_action WHERE shipment_id=?",
        (shipment_id,),
    ):
        entries.append({
            "type": "operator_action",
            "label": ACTION_KINDS.get(r["kind"], f"OPERATOR: {r['kind']}"),
            "kind": r["kind"],
            "at": r["acted_at"],
            "ord": r["ord"],
            "note": r["note"],               # verbatim
            "cancel_reason": r["cancel_reason"],
            "actor": r["actor"],
        })
    for r in conn.execute(
        "SELECT rowid AS ord, channel, content, confirmed_at"
        " FROM customer_confirmation WHERE shipment_id=?",
        (shipment_id,),
    ):
        entries.append({
            "type": "customer_confirmation",
            "label": SOURCE_LABELS["customer_confirmation"],
            "at": r["confirmed_at"],
            "ord": r["ord"],
            "channel": r["channel"],
            "content": r["content"],         # verbatim
        })
    for r in conn.execute(
        "SELECT rowid AS ord, id, reason, due_at, status, created_at, closed_at"
        " FROM follow_up WHERE shipment_id=?",
        (shipment_id,),
    ):
        entries.append({
            "type": "follow_up",
            "label": SOURCE_LABELS["follow_up"],
            # created_at is when the record entered the timeline
            "at": r["created_at"],
            "ord": r["ord"],
            "reason": r["reason"],
            "due_at": r["due_at"],
            "status": r["status"],
            "closed_at": r["closed_at"],
        })
    entries.sort(key=_sort_key)
    return entries


def _state_changes(entries):
    """Derive state-change entries by replaying the SAME derivation rule over
    the sorted timeline. Purely presentational replay of Slice 1 logic — no
    new rules."""
    from commerceops.core import TERMINAL_KINDS

    changes = []
    current = "NEW"
    prev = None
    for e in entries:
        if e["type"] == "tracking_event":
            new = "NEEDS_ACTION"
        elif e["type"] == "operator_action" and e.get("kind") in TERMINAL_KINDS:
            new = TERMINAL_KINDS[e["kind"]]
        else:
            new = "ACTION_TAKEN"
        if new != current and not (current == "NEW" and new == "ACTION_TAKEN"):
            changes.append({
                "after_entry_ord": e["ord"],
                "from_state": current,
                "to_state": new,
                "at": e["at"],
            })
        current = new
    return changes


def shipment_detail(conn, tracking_no: str):
    """Full detail representation for one shipment, or None if unknown.

    Returns dict: header fields + timeline (entries annotated with any state
    change that occurred immediately after them). Raw values are never altered.
    """
    s = get_shipment_by_tracking(conn, tracking_no)
    if s is None:
        return None
    from commerceops.core import derive_state
    entries = build_timeline(conn, s["id"])
    fresh_state = derive_state(conn, s["id"])
    changes = _state_changes(entries)
    for ch in changes:
        for e in entries:
            if e["ord"] == ch["after_entry_ord"]:
                e["state_change_after"] = {
                    "from": ch["from_state"], "to": ch["to_state"],
                    "label": SOURCE_LABELS["state_change"],
                }
    return {
        "tracking_no": s["tracking_no"],
        "courier": s["courier"],               # free label from import [E]
        "customer_phone": s["customer_phone"],
        # SPEC §C: current_state is a CACHE of the derivation. The displayed
        # state is always the freshly computed derivation; if the stored cache
        # diverges (e.g. an out-of-band writer bypassed refresh_state) we
        # surface it rather than silently show stale state.
        "derived_state": fresh_state,
        "cached_state": s["current_state"],
        "cache_mismatch": fresh_state != s["current_state"],
        "created_at": s["created_at"],
        "timeline": entries,
    }


def _esc(value) -> str:
    """H1 fix: HTML-escape every interpolated value before rendering."""
    import html as _html_mod
    return _html_mod.escape("" if value is None else str(value), quote=True)


def render_detail_html(detail) -> str:
    """Minimal operational HTML view. Optimized for comprehension:
    every row leads with its epistemic label. No JS, no styling tricks.
    All values HTML-escaped (audit H1)."""
    if detail is None:
        return (
            "<html><body><h1>Shipment not found</h1>"
            "<p>No shipment exists for the given tracking number.</p></body></html>"
        )
    rows = []
    for e in detail["timeline"]:
        parts = [f"<strong>{e['label']}</strong>"]
        if e["type"] == "tracking_event":
            parts.append(f"code=<code>{_esc(e['raw_code'])}</code>")
            if e["raw_text"]:
                parts.append(f"text=<code>{_esc(e['raw_text'])}</code>")
            if not e["occurred_at_source_provided"]:
                parts.append("<em>(no source timestamp; shown at import time)</em>")
        elif e["type"] == "operator_action":
            body = e.get("cancel_reason") or e.get("note") or ""
            if body:
                parts.append(f"<code>{_esc(body)}</code>")
            parts.append(f"actor={_esc(e['actor'])}")
        elif e["type"] == "customer_confirmation":
            parts.append(f"channel={_esc(e.get('channel') or 'unspecified')}")
            parts.append(f"<code>{_esc(e['content'])}</code>")
        elif e["type"] == "follow_up":
            parts.append(
                f"reason=<code>{_esc(e['reason'])}</code> due={_esc(e['due_at'])} status={_esc(e['status'])}"
            )
        sc = e.get("state_change_after")
        if sc:
            parts.append(
                f"<em>[{sc['label']}: {_esc(sc['from'])} &rarr; {_esc(sc['to'])}]</em>"
            )
        rows.append(f"<li>{_esc(e['at'])} &mdash; " + " | ".join(parts) + "</li>")
    timeline_html = "<ol>\n" + "\n".join(rows) + "\n</ol>" if rows else "<p>No events recorded.</p>"
    cache_warn = ""
    if detail.get("cache_mismatch"):
        cache_warn = (f" | <em>WARNING: cached state out of sync "
                      f"({_esc(detail['cached_state'])})</em>")
    return f"""<html><head><title>Shipment {_esc(detail['tracking_no'])}</title></head><body>
<h1>Shipment {_esc(detail['tracking_no'])}</h1>
<p>
State: <strong>{_esc(detail['derived_state'])}</strong>{cache_warn} |
Courier: {_esc(detail['courier'] or 'unknown')} |
Phone: {_esc(detail['customer_phone'] or 'not provided')} |
Created: {_esc(detail['created_at'])}
</p>
<h2>Timeline (oldest first)</h2>
{timeline_html}
</body></html>"""
