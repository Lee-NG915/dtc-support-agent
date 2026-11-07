"""UTC time helpers."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """All persisted timestamps are UTC. Presentation layer converts timezone."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalize database values, including SQLite's naive datetimes, to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
