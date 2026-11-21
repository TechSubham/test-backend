from datetime import UTC, datetime


def utc_now_iso() -> str:
    """Server time as ISO-8601 UTC with milliseconds, e.g. 2026-11-21T10:00:00.123Z."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
