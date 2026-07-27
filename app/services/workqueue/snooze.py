"""Participant helpers for workqueue-owned personal snooze state.

``operations.agent_workqueue`` owns the public command and transaction. These
helpers validate and flush only; compatibility wrappers at the bottom delegate
back through that owner instead of retaining a parallel commit path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.workqueue import WorkqueueItemKind, WorkqueueSnooze
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.workqueue.types import ItemKind


class WorkqueueSnoozeError(DomainError):
    """Transport-neutral snooze validation failure."""


def _coerce_kind(item_kind: str | ItemKind) -> str:
    value = str(item_kind)
    try:
        WorkqueueItemKind(value)
    except ValueError as exc:
        raise WorkqueueSnoozeError(
            code="operations.agent_workqueue.invalid_item_kind",
            message=f"Unknown workqueue item kind: {value}",
        ) from exc
    return value


def snooze_item(
    db: Session,
    *,
    user_id: str | UUID,
    item_kind: str | ItemKind,
    item_id: str | UUID,
    snooze_until: datetime | None = None,
    until_next_reply: bool = False,
) -> WorkqueueSnooze:
    user_uuid = coerce_uuid(user_id)
    item_uuid = coerce_uuid(item_id)
    kind = _coerce_kind(item_kind)
    snooze = (
        db.query(WorkqueueSnooze)
        .filter(WorkqueueSnooze.user_id == user_uuid)
        .filter(WorkqueueSnooze.item_kind == kind)
        .filter(WorkqueueSnooze.item_id == item_uuid)
        .one_or_none()
    )
    if snooze is None:
        snooze = WorkqueueSnooze(
            user_id=user_uuid,
            item_kind=kind,
            item_id=item_uuid,
        )
        db.add(snooze)
    snooze.snooze_until = snooze_until
    snooze.until_next_reply = until_next_reply
    db.flush()
    return snooze


def clear_snooze(
    db: Session,
    *,
    user_id: str | UUID,
    item_kind: str | ItemKind,
    item_id: str | UUID,
) -> bool:
    snooze = (
        db.query(WorkqueueSnooze)
        .filter(WorkqueueSnooze.user_id == coerce_uuid(user_id))
        .filter(WorkqueueSnooze.item_kind == _coerce_kind(item_kind))
        .filter(WorkqueueSnooze.item_id == coerce_uuid(item_id))
        .one_or_none()
    )
    if snooze is None:
        return False
    db.delete(snooze)
    db.flush()
    return True


def active_snoozed_ids(
    db: Session,
    *,
    user_id: str | UUID,
    now: datetime | None = None,
) -> dict[ItemKind, set[UUID]]:
    """Item ids the user has snoozed, grouped by kind.

    ``until_next_reply`` snoozes stay active until the conversation gets a new
    inbound message (cleared by ``release_until_next_reply``); a snooze with no
    ``snooze_until`` and no ``until_next_reply`` is treated as indefinite.
    """
    current_time = now or datetime.now(UTC)
    rows = (
        db.query(WorkqueueSnooze.item_kind, WorkqueueSnooze.item_id)
        .filter(WorkqueueSnooze.user_id == coerce_uuid(user_id))
        .filter(
            or_(
                WorkqueueSnooze.until_next_reply.is_(True),
                WorkqueueSnooze.snooze_until.is_(None),
                and_(
                    WorkqueueSnooze.snooze_until.isnot(None),
                    WorkqueueSnooze.snooze_until > current_time,
                ),
            )
        )
        .all()
    )
    snoozed: dict[ItemKind, set[UUID]] = {kind: set() for kind in ItemKind}
    for item_kind, item_id in rows:
        try:
            kind = ItemKind(item_kind)
        except ValueError:
            # A kind the aggregator no longer projects (e.g. a retired source).
            continue
        snoozed[kind].add(item_id)
    return snoozed


def release_until_next_reply(db: Session, *, conversation_id: str | UUID) -> list[UUID]:
    """Drop ``until_next_reply`` snoozes for a conversation that just replied.

    Returns the user ids whose queue changed (callers use it to target realtime
    updates). Uncommitted — the caller owns the transaction.
    """
    rows = (
        db.query(WorkqueueSnooze)
        .filter(WorkqueueSnooze.item_kind == ItemKind.conversation.value)
        .filter(WorkqueueSnooze.item_id == coerce_uuid(conversation_id))
        .filter(WorkqueueSnooze.until_next_reply.is_(True))
        .all()
    )
    affected = [row.user_id for row in rows]
    for row in rows:
        db.delete(row)
    if rows:
        db.flush()
    return affected


# --- Compatibility entry points ---------------------------------------------


def snooze_item_committed(
    db: Session,
    *,
    user_id: str | UUID,
    item_kind: str | ItemKind,
    item_id: str | UUID,
    snooze_until: datetime | None = None,
    until_next_reply: bool = False,
) -> WorkqueueSnooze:
    from uuid import uuid4

    from app.services.owner_commands import CommandContext
    from app.services.workqueue.commands import (
        SnoozeMode,
        WorkqueueActionCommand,
        execute_action,
    )
    from app.services.workqueue.permissions import WorkqueuePrincipal
    from app.services.workqueue.types import ActionKind

    user_uuid = coerce_uuid(user_id)
    mode = SnoozeMode.indefinite
    if until_next_reply:
        mode = SnoozeMode.next_reply
    elif snooze_until is not None:
        mode = SnoozeMode.explicit
    request_id = uuid4()
    execute_action(
        db,
        WorkqueueActionCommand(
            context=CommandContext.system(
                actor=f"user:{user_uuid}",
                scope="workqueue:snooze",
                reason="Set personal workqueue snooze",
                command_id=request_id,
                idempotency_key=str(request_id),
            ),
            principal=WorkqueuePrincipal(
                person_id=user_uuid,
                roles=frozenset({"admin"}),
                scopes=frozenset(),
                can_view=True,
                can_act=True,
            ),
            item_kind=ItemKind(str(item_kind)),
            item_id=coerce_uuid(item_id),
            action=ActionKind.snooze,
            snooze_mode=mode,
            explicit_snooze_until=snooze_until,
        ),
    )
    snooze = (
        db.query(WorkqueueSnooze)
        .filter(
            WorkqueueSnooze.user_id == user_uuid,
            WorkqueueSnooze.item_kind == _coerce_kind(item_kind),
            WorkqueueSnooze.item_id == coerce_uuid(item_id),
        )
        .one()
    )
    db.refresh(snooze)
    return snooze


def clear_snooze_committed(
    db: Session,
    *,
    user_id: str | UUID,
    item_kind: str | ItemKind,
    item_id: str | UUID,
) -> None:
    from uuid import uuid4

    from app.services.owner_commands import CommandContext
    from app.services.workqueue.commands import (
        WorkqueueActionCommand,
        execute_action,
    )
    from app.services.workqueue.permissions import WorkqueuePrincipal
    from app.services.workqueue.types import ActionKind

    user_uuid = coerce_uuid(user_id)
    request_id = uuid4()
    execute_action(
        db,
        WorkqueueActionCommand(
            context=CommandContext.system(
                actor=f"user:{user_uuid}",
                scope="workqueue:snooze",
                reason="Clear personal workqueue snooze",
                command_id=request_id,
                idempotency_key=str(request_id),
            ),
            principal=WorkqueuePrincipal(
                person_id=user_uuid,
                roles=frozenset({"admin"}),
                scopes=frozenset(),
                can_view=True,
                can_act=True,
            ),
            item_kind=ItemKind(str(item_kind)),
            item_id=coerce_uuid(item_id),
            action=ActionKind.clear_snooze,
        ),
    )
