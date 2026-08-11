"""What a setting was before it changed, and who changed it.

`AuditEvent` records that a change happened; it does not record the value. So
"who turned this off, and what was it before" has been unanswerable — the sort
of question that gets asked during an incident, when the answer matters most.

## Why this mirrors the kernel's model exactly

`dotmac_kernel.settings_models.DomainSettingHistory` defines this table for the
kernel's own writers, and Sub is moving its settings writes onto them. Declaring
the same shape here means that cutover needs no schema change and no data
migration: the kernel's `_record_history` writes the rows this file already
describes.

It is a restatement, and a deliberate one. The kernel's model lives on the
kernel's `Base`, which is not Sub's — and `001_squashed_initial_schema` builds
the test lanes from SUB's metadata, so a table Sub does not declare simply does
not exist there. `DomainSetting` itself is carried the same way for the same
reason. The cost is that these two declarations must not drift; the migration
and `tests/test_domain_setting_history.py` are what hold them together.

## The value is not recorded for a secret

`value_before` and `value_after` stay NULL when the setting is secret, and
`secret_changed` carries the fact that it moved. A history table that kept
credentials would mean rotating a compromised secret leaves the compromised one
readable forever — the table meant to explain a change becoming the place a leak
persists.
"""

from __future__ import annotations

import enum
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    event,
    insert,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.domain_settings import DomainSetting, SettingDomain, SettingDomainType


class SettingChangeAction(str, enum.Enum):
    """Genuinely closed, and deliberately still an enum.

    A row was created, updated or deleted; no module declares a fourth. ADR-0008
    constrains vocabularies whose members belong to somebody else — not every
    enum. Same reasoning as the kernel's own member of this name.
    """

    create = "create"
    update = "update"
    delete = "delete"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


class DomainSettingHistory(Base):
    """One recorded transition of a setting's value."""

    __tablename__ = "domain_setting_history"
    __table_args__ = (
        Index("ix_domain_setting_history_lookup", "tenant_id", "domain", "key"),
        Index("ix_domain_setting_history_changed_at", "changed_at"),
        Index("ix_domain_setting_history_actor", "changed_by_party_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: Denormalised from the parent so history survives the setting's deletion —
    #: the transition that matters most is often the last one.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    domain: Mapped[SettingDomain] = mapped_column(
        SettingDomainType(120), nullable=False
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    setting_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("domain_settings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[SettingChangeAction] = mapped_column(
        Enum(
            SettingChangeAction,
            name="ck_domain_setting_history_action",
            native_enum=False,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    #: Rendered as text for both scalar and JSON settings: history is read by a
    #: human comparing two states, not by code re-parsing them.
    value_before: Mapped[str | None] = mapped_column(Text)
    value_after: Mapped[str | None] = mapped_column(Text)
    #: True when the setting is secret, in which case the two columns above stay
    #: NULL — see the module docstring.
    secret_changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    #: WHO, and the request it arrived on. All nullable: a seed, a migration or
    #: a CLI genuinely has no actor, and recording "unknown" honestly beats
    #: inventing one.
    changed_by_party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parties.id", ondelete="SET NULL"),
        nullable=True,
    )
    change_reason: Mapped[str | None] = mapped_column(Text)
    #: 45 characters holds an IPv6 address with an embedded IPv4 suffix.
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    #: Ties the change to one request across logs, audit and outbox.
    request_id: Mapped[str | None] = mapped_column(String(128))


# ── Recording ───────────────────────────────────────────────────────────────
#
# The listeners live in the MODEL module, like `_reject_undeclared_domain`, so
# they register wherever models are imported — which is everywhere. Putting them
# in a service would mean history depended on some import having happened, and
# the paths that forgot would be silently unrecorded, which is worse than no
# history at all: it reads as complete and is not.
def _stored_text(value_text: str | None, value_json: object) -> str | None:
    """The row's value as history records it — text, or dumped JSON."""

    if value_json is not None:
        return json.dumps(value_json, sort_keys=True)
    return value_text


def _is_secret(target: DomainSetting) -> bool:
    return bool(getattr(target, "is_secret", False))


def _record(
    connection: Connection,
    target: DomainSetting,
    action: SettingChangeAction,
    *,
    before: str | None,
    after: str | None,
) -> None:
    """Insert the transition through the CONNECTION, not the session.

    `Session.add` is unsupported inside a mapper-level flush event — the flush
    plan is already built, so a new object either raises or is silently missed.
    A direct insert on the same connection joins the caller's transaction, which
    is what makes the history and the change atomic: if the write rolls back,
    so does its record.
    """

    from app.services.setting_history import current_change_context

    context = current_change_context()
    secret = _is_secret(target)
    connection.execute(
        insert(DomainSettingHistory).values(
            id=uuid.uuid4(),
            tenant_id=target.tenant_id,
            domain=str(target.domain),
            key=target.key,
            setting_id=target.id,
            action=action.value,
            # A secret's value is never recorded. A history table that kept it
            # would mean rotating a compromised credential leaves the
            # compromised one readable for as long as history is retained.
            value_before=None if secret else before,
            value_after=None if secret else after,
            secret_changed=secret,
            changed_at=datetime.now(UTC),
            changed_by_party_id=context.actor_party_id,
            change_reason=context.reason,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
            request_id=context.request_id,
        )
    )


def _after_insert(
    mapper: object, connection: Connection, target: DomainSetting
) -> None:
    _record(
        connection,
        target,
        SettingChangeAction.create,
        before=None,
        after=_stored_text(target.value_text, target.value_json),
    )


def _after_update(
    mapper: object, connection: Connection, target: DomainSetting
) -> None:
    """Record the transition, using the values SQLAlchemy still remembers.

    `get_history` is read here rather than in `before_update` because the
    attribute's previous value is only available while the flush is in
    progress; once it completes, the session has nothing left to compare.
    """

    from sqlalchemy import inspect

    state = inspect(target)
    text_history = state.attrs.value_text.history
    json_history = state.attrs.value_json.history

    if not text_history.has_changes() and not json_history.has_changes():
        # A change to `is_active`, `label` or ordering is not a value
        # transition, and recording one would make the history noisy in exactly
        # the way that stops people reading it.
        return

    before_text = text_history.deleted[0] if text_history.deleted else target.value_text
    before_json = json_history.deleted[0] if json_history.deleted else target.value_json
    _record(
        connection,
        target,
        SettingChangeAction.update,
        before=_stored_text(before_text, before_json),
        after=_stored_text(target.value_text, target.value_json),
    )


def _after_delete(
    mapper: object, connection: Connection, target: DomainSetting
) -> None:
    _record(
        connection,
        target,
        SettingChangeAction.delete,
        before=_stored_text(target.value_text, target.value_json),
        after=None,
    )


event.listen(DomainSetting, "after_insert", _after_insert)
event.listen(DomainSetting, "after_update", _after_update)
event.listen(DomainSetting, "after_delete", _after_delete)
