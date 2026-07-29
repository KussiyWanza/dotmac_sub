import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.sequence import DocumentSequence
from app.services import settings_spec

logger = logging.getLogger(__name__)


def _format_number(prefix: str | None, padding: int | None, value: int) -> str:
    prefix_value = prefix or ""
    pad = max(int(padding or 0), 0)
    if pad > 0:
        return f"{prefix_value}{value:0{pad}d}"
    return f"{prefix_value}{value}"


def lock_sequence(db: Session, key: str, start_value: int) -> DocumentSequence:
    """Return a sequence row while holding its transaction-level row lock.

    The row must exist before it can be locked.  A query-then-insert race let
    two first-time callers both conclude that a sequence was absent.  Use the
    database's conflict arbiter to establish the row before taking the lock.
    """
    bind = db.get_bind()
    values = {
        "id": uuid.uuid4(),
        "key": key,
        "next_value": start_value,
    }
    if bind.dialect.name == "postgresql":
        db.execute(
            postgresql_insert(DocumentSequence)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[DocumentSequence.key])
        )
    elif bind.dialect.name == "sqlite":
        db.execute(
            sqlite_insert(DocumentSequence)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[DocumentSequence.key])
        )
    else:  # pragma: no cover - production and tests use PostgreSQL/SQLite
        sequence = db.scalar(
            select(DocumentSequence).where(DocumentSequence.key == key)
        )
        if sequence is None:
            db.add(DocumentSequence(**values))
            db.flush()

    sequence = db.scalar(
        select(DocumentSequence).where(DocumentSequence.key == key).with_for_update()
    )
    if sequence is None:  # pragma: no cover - the insert/select invariant failed
        raise RuntimeError(f"document sequence {key!r} could not be established")
    return sequence


def reconcile_next_value(db: Session, key: str, minimum_next_value: int) -> int:
    """Advance, but never rewind, a sequence while holding its row lock."""
    sequence = lock_sequence(db, key, minimum_next_value)
    if sequence.next_value < minimum_next_value:
        sequence.next_value = minimum_next_value
        db.flush()
    return sequence.next_value


def _next_sequence_value(db: Session, key: str, start_value: int) -> int:
    """Atomically reserve the next value for one document sequence."""
    sequence = lock_sequence(db, key, start_value)
    value = sequence.next_value
    sequence.next_value = value + 1
    db.flush()
    return value


def _resolve_setting(db: Session, domain: SettingDomain, key: str):
    return settings_spec.resolve_value(db, domain, key)


def generate_number_with_config(
    db: Session,
    sequence_key: str,
    *,
    prefix: str | None,
    padding: int | None,
    start_value: int | None,
) -> str:
    start_value_int = max(int(start_value or 1), 1)
    value = _next_sequence_value(db, sequence_key, start_value_int)
    return _format_number(prefix, padding, value)


def generate_number(
    db: Session,
    domain: SettingDomain,
    sequence_key: str,
    enabled_key: str,
    prefix_key: str,
    padding_key: str,
    start_key: str,
) -> str | None:
    enabled = _resolve_setting(db, domain, enabled_key)
    if enabled is False:
        return None
    prefix = _resolve_setting(db, domain, prefix_key)
    padding = _resolve_setting(db, domain, padding_key)
    start_value = _resolve_setting(db, domain, start_key)
    try:
        start_value_int = int(start_value) if start_value is not None else 1
    except (TypeError, ValueError):
        start_value_int = 1
    return generate_number_with_config(
        db,
        sequence_key,
        prefix=prefix if isinstance(prefix, str) else None,
        padding=padding if isinstance(padding, int) else None,
        start_value=start_value_int,
    )


def generate_required_number(
    db: Session,
    domain: SettingDomain,
    sequence_key: str,
    prefix_key: str,
    padding_key: str,
    start_key: str,
) -> str:
    """Generate a document number from formatting policy with no runtime toggle."""
    prefix = _resolve_setting(db, domain, prefix_key)
    padding = _resolve_setting(db, domain, padding_key)
    start_value = _resolve_setting(db, domain, start_key)
    try:
        start_value_int = int(start_value) if start_value is not None else 1
    except (TypeError, ValueError):
        start_value_int = 1
    return generate_number_with_config(
        db,
        sequence_key,
        prefix=prefix if isinstance(prefix, str) else None,
        padding=padding if isinstance(padding, int) else None,
        start_value=start_value_int,
    )
