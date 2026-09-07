"""``/ws/workqueue`` — live queue updates for staff.

On connect we resolve the caller's workqueue scope and subscribe the socket to
exactly the channels that scope permits (own user channel, their teams, plus the
org channel for org-audience viewers). A client never picks its own channels, so
a socket cannot subscribe its way into another team's work.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.logging import get_logger
from app.services.db_session_adapter import db_session_adapter
from app.services.workqueue import (
    WorkqueuePermissionError,
    get_workqueue_scope,
    principal_from_auth,
)
from app.services.workqueue.events import channels_for_scope
from app.websocket.auth import accepted_auth_subprotocol, authenticate_staff_websocket
from app.websocket.events import InboundMessage, InboundMessageType
from app.websocket.manager import get_connection_manager

logger = get_logger(__name__)

router = APIRouter(tags=["websocket"])


def _resolve_channels(auth: dict, audience: str | None) -> list[str]:
    db = db_session_adapter.create_session()
    try:
        principal = principal_from_auth(db, auth)
        scope = get_workqueue_scope(db, principal, requested_audience=audience)
        return channels_for_scope(scope)
    finally:
        db.close()


@router.websocket("/ws/workqueue")
async def workqueue_websocket(websocket: WebSocket):
    await websocket.accept(subprotocol=accepted_auth_subprotocol(websocket))

    auth = await authenticate_staff_websocket(websocket)
    if not auth:
        return

    audience = websocket.query_params.get("audience")
    try:
        channels = _resolve_channels(auth, audience)
    except WorkqueuePermissionError as exc:
        logger.info("workqueue_ws_forbidden principal=%s", auth.get("principal_id"))
        await websocket.close(code=4003, reason=str(exc))
        return
    except Exception as exc:
        logger.warning("workqueue_ws_scope_failed error=%s", exc)
        await websocket.close(code=1011, reason="Scope resolution failed")
        return

    user_id = auth["principal_id"]
    manager = get_connection_manager()
    await manager.register_connection(
        user_id,
        websocket,
        topics=(*channels, "audience:staff"),
        ready_data={"channels": channels},
    )

    try:
        while True:
            raw = await websocket.receive_text()
            await _handle_client_message(user_id, websocket, raw, manager)
    except WebSocketDisconnect:
        logger.debug("workqueue_ws_disconnected user_id=%s", user_id)
    except Exception as exc:
        logger.warning("workqueue_ws_error user_id=%s error=%s", user_id, exc)
    finally:
        await manager.unregister_connection(user_id, websocket)


async def _handle_client_message(
    user_id: str, websocket: WebSocket, raw: str, manager
) -> None:
    """Only keep-alives are accepted — channels are server-assigned."""
    try:
        message = InboundMessage(**json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        logger.debug("workqueue_ws_invalid_message user_id=%s", user_id)
        return

    if message.type == InboundMessageType.PING:
        await manager.send_heartbeat(user_id, websocket)
