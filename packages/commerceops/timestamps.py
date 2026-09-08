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
