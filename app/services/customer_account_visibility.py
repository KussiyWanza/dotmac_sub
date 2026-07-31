"""Canonical legacy-import customer deletion visibility policy."""

from sqlalchemy import and_, func, not_, or_
from sqlalchemy.sql.elements import ColumnElement

from app.models.subscriber import Subscriber, SubscriberStatus

_METADATA_TRUE_VALUES = ("1", "true", "yes", "on")
_METADATA_FALSE_VALUES = ("0", "false", "no", "off")


def _metadata_flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _METADATA_TRUE_VALUES:
            return True
        if normalized in _METADATA_FALSE_VALUES:
            return False
        return None
    if isinstance(value, int):
        if value == 1:
            return True
        if value == 0:
            return False
    return None


def is_splynx_deleted_import(subscriber: Subscriber) -> bool:
    """Return whether a subscriber represents a legacy soft-deleted import."""
    metadata = subscriber.metadata_ or {}
    deletion_flag = _metadata_flag(metadata.get("splynx_deleted"))
    if deletion_flag is not None:
        return deletion_flag
    if not getattr(subscriber, "splynx_customer_id", None):
        return False
    if subscriber.is_active:
        return False
    if subscriber.status != SubscriberStatus.canceled:
        return False
    raw_status = str(metadata.get("splynx_status") or "").strip().lower()
    return raw_status not in {"", "deleted", "canceled"}


def _metadata_text_clause(key: str) -> ColumnElement[str]:
    return func.lower(
        func.trim(func.coalesce(Subscriber.metadata_[key].as_string(), ""))
    )


def splynx_deleted_import_clause() -> ColumnElement[bool]:
    """Return a SQL clause matching legacy soft-deleted imported subscribers."""
    splynx_deleted = _metadata_text_clause("splynx_deleted")
    splynx_status = _metadata_text_clause("splynx_status")
    return or_(
        splynx_deleted.in_(_METADATA_TRUE_VALUES),
        and_(
            not_(splynx_deleted.in_(_METADATA_FALSE_VALUES)),
            Subscriber.splynx_customer_id.is_not(None),
            Subscriber.is_active.is_(False),
            Subscriber.status == SubscriberStatus.canceled,
            not_(splynx_status.in_(("", "deleted", "canceled"))),
        ),
    )
