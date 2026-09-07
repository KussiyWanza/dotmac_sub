from __future__ import annotations

from pathlib import Path

from starlette.websockets import WebSocket

from app.websocket.auth import (
    WEBSOCKET_AUTH_SUBPROTOCOL,
    _websocket_token,
    accepted_auth_subprotocol,
)


def _socket(*, protocols: str = "", query: bytes = b"") -> WebSocket:
    headers = []
    if protocols:
        headers.append((b"sec-websocket-protocol", protocols.encode("ascii")))
    return WebSocket(
        {
            "type": "websocket",
            "path": "/ws/inbox",
            "query_string": query,
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "scheme": "ws",
        },
        receive=lambda: None,
        send=lambda _message: None,
    )


def test_websocket_token_prefers_non_url_auth_subprotocol() -> None:
    websocket = _socket(
        protocols="dotmac-auth, header.payload.signature",
        query=b"token=legacy-query-token",
    )

    assert _websocket_token(websocket) == "header.payload.signature"
    assert accepted_auth_subprotocol(websocket) == WEBSOCKET_AUTH_SUBPROTOCOL


def test_websocket_query_token_remains_a_rolling_deployment_fallback() -> None:
    websocket = _socket(query=b"token=legacy-query-token")

    assert _websocket_token(websocket) == "legacy-query-token"
    assert accepted_auth_subprotocol(websocket) is None


def test_nginx_disables_websocket_access_log() -> None:
    nginx = Path("deploy/nginx/selfcare.dotmac.io").read_text(encoding="utf-8")
    websocket_location = nginx.split("location /ws {", 1)[1].split("}", 1)[0]

    assert "access_log off;" in websocket_location
