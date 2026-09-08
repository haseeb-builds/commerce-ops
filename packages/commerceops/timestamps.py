"""Chronological comparison without rewriting source timestamp evidence."""
from datetime import datetime, timezone


def timestamp_key(value: str) -> datetime:
    """Compare ISO timestamps by instant, not lexicographic timezone offsets.

    Legacy timestamps without an offset retain the system's UTC convention.
    Callers store/display the original value; this key is for ordering only.
    """
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def parse_timestamp(value):
    """Tolerant read-side parser: unknown chronology is None, never a fake date."""
    try:
        return timestamp_key(value)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def display_order(value):
    """Group unknown-time records first for review; order known instants normally."""
    parsed = parse_timestamp(value)
    return (parsed is not None, parsed or datetime.min.replace(tzinfo=timezone.utc))


EVENT_TIMESTAMP_FIELDS = {
    "tracking_event": ("occurred_at", "imported_at"),
    "operator_action": ("acted_at",),
    "customer_confirmation": ("confirmed_at",),
}


def invalid_timestamp_fields(source, data):
    """Pure diagnostics over source values. An absent occurrence time is allowed."""
    return [field for field in EVENT_TIMESTAMP_FIELDS.get(source, ())
            if not (field == "occurred_at" and data.get(field) is None)
            and parse_timestamp(data.get(field)) is None]


def shipment_timestamp_issues(conn, shipment_id):
    """Read-only compatibility diagnostics; never rewrite historical evidence."""
    issues = []
    for source, fields in EVENT_TIMESTAMP_FIELDS.items():
        for row in conn.execute(f"SELECT id, {', '.join(fields)} FROM {source} WHERE shipment_id=?", (shipment_id,)):
            data = dict(row)
            issues.extend({"source": source, "record_id": row["id"], "field": field, "value": data[field]}
                          for field in invalid_timestamp_fields(source, data))
    return issues
