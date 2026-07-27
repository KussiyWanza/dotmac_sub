from __future__ import annotations

import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.workqueue import (
    WorkqueueItemRead,
    WorkqueueSnoozeCreate,
    WorkqueueSnoozeRead,
    WorkqueueViewRead,
)
from app.services import workqueue
from app.services.auth_dependencies import require_permission, require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.realtime_platform import (
    iter_topic_events,
    ready_event,
    reset_event,
    sse_message,
)
from app.services.response import list_response
from app.services.workqueue import WorkqueuePermissionError, WorkqueuePrincipal
from app.services.workqueue.commands import (
    SnoozeMode,
    WorkqueueActionCommand,
    execute_action,
)
from app.services.workqueue.events import channels_for_scope
from app.services.workqueue.types import ActionKind, ItemKind

router = APIRouter(prefix="/workqueue", tags=["workqueue"])
logger = logging.getLogger(__name__)

AUDIENCE_QUERY = Query(
    default=None,
    description="self | team | org — clamped to the audience the caller holds",
)


def _user_id(auth: dict) -> str:
    return str(auth.get("principal_id") or auth.get("person_id"))


def _principal(db: Session, auth: dict) -> WorkqueuePrincipal:
    try:
        return workqueue.principal_from_auth(db, auth)
    except WorkqueuePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _command_context(
    auth: dict,
    *,
    action: ActionKind,
    idempotency_key: str | None,
) -> CommandContext:
    command_id = uuid4()
    principal_id = _user_id(auth)
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{actor_type}:{principal_id}",
        scope="support:ticket:update",
        reason=f"Execute workqueue {action.value} API command",
        idempotency_key=str(idempotency_key or command_id),
    )


def _map_action_error(exc: DomainError) -> HTTPException:
    if exc.code.endswith("permission_denied") or exc.code.endswith("item_out_of_scope"):
        return HTTPException(status_code=403, detail=exc.message)
    if exc.code.endswith("item_not_found"):
        return HTTPException(status_code=404, detail=exc.message)
    if exc.code.endswith("idempotency_conflict"):
        return HTTPException(status_code=409, detail=exc.message)
    return HTTPException(status_code=422, detail=exc.message)


@router.get(
    "",
    response_model=ListResponse[WorkqueueItemRead],
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def list_workqueue(
    audience: str | None = AUDIENCE_QUERY,
    service_team_id: UUID | None = None,
    include_snoozed: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    try:
        rows = workqueue.list_workqueue(
            db,
            _principal(db, auth),
            requested_audience=audience,
            service_team_id=service_team_id,
            include_snoozed=include_snoozed,
            limit=limit,
            offset=offset,
        )
    except WorkqueuePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return list_response(
        [WorkqueueItemRead.model_validate(row) for row in rows], limit, offset
    )


@router.get(
    "/view",
    response_model=WorkqueueViewRead,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def workqueue_view(
    audience: str | None = AUDIENCE_QUERY,
    service_team_id: UUID | None = None,
    include_snoozed: bool = False,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    """Ranked hero band ("right now") plus one section per item source."""
    try:
        view = workqueue.build_workqueue(
            db,
            _principal(db, auth),
            requested_audience=audience,
            service_team_id=service_team_id,
            include_snoozed=include_snoozed,
        )
    except WorkqueuePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return WorkqueueViewRead.model_validate(view)


@router.get(
    "/events",
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def workqueue_events(
    request: Request,
    audience: str | None = AUDIENCE_QUERY,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    """SSE transport for the same server-scoped workqueue invalidations as WS."""
    try:
        scope = workqueue.get_workqueue_scope(
            db,
            _principal(db, auth),
            requested_audience=audience,
        )
        topics = channels_for_scope(scope)
    except WorkqueuePermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # Streaming responses keep dependencies alive until disconnect. Release
    # this lookup session before returning; the event stream needs no database.
    db_session_adapter.release_read_transaction(db)
    db.close()

    async def event_generator():
        yield sse_message(ready_event(topics, transport="sse"))
        if last_event_id:
            yield sse_message(reset_event(topics, reason="redis_pubsub_has_no_replay"))
        try:
            async for event in iter_topic_events(
                topics,
                stop_requested=request.is_disconnected,
            ):
                yield sse_message(event)
        except Exception as exc:
            logger.warning("workqueue_sse_stream_failed error=%s", exc)
            yield sse_message(reset_event(topics, reason="broker_unavailable"))

    return EventSourceResponse(
        event_generator(),
        ping=15,
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/snoozes",
    response_model=WorkqueueSnoozeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def snooze_item(
    payload: WorkqueueSnoozeCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    mode = SnoozeMode.indefinite
    if payload.until_next_reply:
        mode = SnoozeMode.next_reply
    elif payload.snooze_until is not None:
        mode = SnoozeMode.explicit
    try:
        outcome = execute_action(
            db,
            WorkqueueActionCommand(
                context=_command_context(
                    auth,
                    action=ActionKind.snooze,
                    idempotency_key=idempotency_key,
                ),
                principal=_principal(db, auth),
                item_kind=ItemKind(payload.item_kind),
                item_id=payload.item_id,
                action=ActionKind.snooze,
                snooze_mode=mode,
                explicit_snooze_until=payload.snooze_until,
            ),
        )
    except (DomainError, ValueError) as exc:
        if isinstance(exc, DomainError):
            raise _map_action_error(exc) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    snapshot = outcome.snooze
    if snapshot is None:
        raise HTTPException(status_code=500, detail="Snooze result evidence is missing")
    return WorkqueueSnoozeRead(
        id=snapshot.snooze_id,
        user_id=snapshot.system_user_id,
        item_kind=snapshot.item_kind.value,
        item_id=snapshot.item_id,
        snooze_until=snapshot.snooze_until,
        until_next_reply=snapshot.until_next_reply,
        created_at=snapshot.created_at,
    )


@router.delete(
    "/snoozes/{item_kind}/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def clear_snooze(
    item_kind: str,
    item_id: UUID,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    try:
        execute_action(
            db,
            WorkqueueActionCommand(
                context=_command_context(
                    auth,
                    action=ActionKind.clear_snooze,
                    idempotency_key=idempotency_key,
                ),
                principal=_principal(db, auth),
                item_kind=ItemKind(item_kind),
                item_id=item_id,
                action=ActionKind.clear_snooze,
            ),
        )
    except (DomainError, ValueError) as exc:
        if isinstance(exc, DomainError):
            raise _map_action_error(exc) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
