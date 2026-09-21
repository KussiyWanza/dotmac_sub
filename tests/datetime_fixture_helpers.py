"""Cross-dialect datetime assertions for the test suite."""

from datetime import UTC, datetime


def naive_utc(value: datetime) -> datetime:
    """Return the represented UTC wall time without dialect-specific tzinfo.

    SQLite reads stored ``DateTime`` values back as naive datetimes while
    PostgreSQL preserves timezone awareness. Tests concerned with the instant,
    rather than the ORM dialect's representation, compare normalized values.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
