from __future__ import annotations

from fastapi import WebSocket

from app.models.system_user import SystemUser
from app.services.auth_dependencies import claims_for_principal
from app.services.auth_flow import decode_access_token
from app.services.db_session_adapter import db_session_adapter
from app.services.team_inbox_widget import decode_widget_token

WEBSOCKET_AUTH_SUBPROTOCOL = "dotmac-auth"


def _requested_subprotocols(websocket: WebSocket) -> tuple[str, ...]:
    raw = websocket.headers.get("sec-websocket-protocol") or ""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def accepted_auth_subprotocol(websocket: WebSocket) -> str | None:
    protocols = _requested_subprotocols(websocket)
    if WEBSOCKET_AUTH_SUBPROTOCOL in protocols:
        return WEBSOCKET_AUTH_SUBPROTOCOL
    return None


def _subprotocol_token(websocket: WebSocket) -> str | None:
    protocols = _requested_subprotocols(websocket)
    try:
        marker = protocols.index(WEBSOCKET_AUTH_SUBPROTOCOL)
    except ValueError:
        return None
    token_index = marker + 1
    return protocols[token_index] if token_index < len(protocols) else None


def _websocket_token(websocket: WebSocket) -> str | None:
    # Browsers cannot set Authorization on a WebSocket handshake. Carry public
    # widget credentials in Sec-WebSocket-Protocol so they do not enter request
    # URLs, browser history, Nginx access logs, or Uvicorn request-line logs.
    # Cookies remain the preferred same-origin staff mechanism. The query
    # fallback supports one rolling-deployment window for older widget clients.
    return (
        _subprotocol_token(websocket)
        or websocket.cookies.get("session_token")
        or websocket.query_params.get("token")
    )


async def authenticate_staff_websocket(websocket: WebSocket) -> dict | None:
    """Authenticate a staff WebSocket and return an ``auth``-shaped dict.

    Shaped like ``require_user_auth``'s return value (principal_id / roles /
    scopes) so services that already take an auth dict — e.g. the workqueue's
    ``principal_from_auth`` — work unchanged over a socket.
    """
    token = _websocket_token(websocket)
    if not token:
        await websocket.close(code=4001, reason="Authentication required")
        return None

    db = db_session_adapter.create_session()
    try:
        payload = decode_access_token(db, token)
        principal_id = payload.get("principal_id") or payload.get("sub")
        if not principal_id:
            await websocket.close(code=4001, reason="Invalid token")
            return None

        principal_type = payload.get("principal_type") or "subscriber"
        roles, scopes = claims_for_principal(
            db, str(principal_id), str(principal_type), payload
        )
        return {
            "principal_id": str(principal_id),
            "person_id": str(principal_id),
            "principal_type": str(principal_type),
            "session_id": payload.get("session_id"),
            "roles": roles,
            "scopes": scopes,
        }
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return None
    finally:
        db.close()


async def authenticate_websocket(websocket: WebSocket) -> dict | None:
    """
    Authenticate WebSocket connection.

    Extracts JWT from the auth subprotocol or same-origin session cookie, with
    a temporary query-token fallback for rolling-deployment compatibility.
    Returns {subscriber_id, session_id} if valid, None otherwise.
    """
    token = _websocket_token(websocket)

    if not token:
        await websocket.close(code=4001, reason="Authentication required")
        return None

    db = db_session_adapter.create_session()
    try:
        try:
            widget_principal = decode_widget_token(db, token)
            return {
                "subscriber_id": f"chat_widget:{widget_principal.session_id}",
                "principal_id": f"chat_widget:{widget_principal.session_id}",
                "principal_type": "chat_widget",
                "session_id": widget_principal.session_id,
                "conversation_id": str(widget_principal.conversation_id),
                "surface": widget_principal.surface,
                "roles": [],
                "scopes": [],
            }
        except Exception:
            pass

        payload = decode_access_token(db, token)
        subscriber_id = payload.get("principal_id") or payload.get("sub")
        session_id = payload.get("session_id")

        if not subscriber_id:
            await websocket.close(code=4001, reason="Invalid token")
            return None

        principal_type = str(payload.get("principal_type") or "subscriber")
        roles, scopes = claims_for_principal(
            db, str(subscriber_id), principal_type, payload
        )
        display_name = None
        if principal_type == "system_user":
            user = db.get(SystemUser, subscriber_id)
            if user is not None:
                display_name = (
                    user.display_name
                    or f"{user.first_name} {user.last_name}".strip()
                    or user.email
                )
        return {
            "subscriber_id": str(subscriber_id),
            "principal_id": str(subscriber_id),
            "principal_type": principal_type,
            "display_name": display_name,
            "session_id": session_id,
            "roles": roles,
            "scopes": scopes,
        }
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return None
    finally:
        db.close()
