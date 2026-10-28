"""Canonical timezone-aware UTC timestamps for mission metadata."""

from datetime import UTC, datetime


def normalize_utc_timestamp(value: str | datetime) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Invalid mission timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Mission timestamp must include a timezone")
    try:
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as exc:
        raise ValueError("Mission timestamp is outside supported datetime limits") from exc
