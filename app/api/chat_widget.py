from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.request_meta import client_ip
from app.schemas.chat import FiberChatSessionCreate, FiberChatSessionResponse
from app.services import team_inbox_widget
from app.services.rate_limiter_adapter import allow_operation

router = APIRouter(prefix="/widget", tags=["chat-widget"])
ResultT = TypeVar("ResultT")


def _widget_call(action: Callable[[], ResultT]) -> ResultT:
    try:
        return action()
    except team_inbox_widget.TeamInboxWidgetError as exc:
        suffix = exc.code.rsplit(".", 1)[-1]
        status_code = {
            "invalid_token": 401,
            "session_mismatch": 403,
            "disabled": 503,
            "authority_external": 503,
            "subscriber_not_found": 404,
            "reseller_not_found": 404,
            "conversation_not_found": 404,
            "message_required": 400,
        }.get(suffix, 400)
        raise HTTPException(status_code=status_code, detail=exc.message) from exc


class WidgetMessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    client_message_id: str | None = Field(default=None, max_length=100)


class WidgetSatisfactionCreate(BaseModel):
    rating: int
    comment: str | None = None


def _principal(
    db: Session,
    x_visitor_token: str | None,
) -> team_inbox_widget.WidgetPrincipal:
    return _widget_call(
        lambda: team_inbox_widget.decode_widget_token(db, x_visitor_token or "")
    )


def _fiber_origin(request: Request) -> str:
    origin = str(request.headers.get("origin") or "").rstrip("/")
    if origin != settings.fiber_chat_allowed_origin:
        raise HTTPException(status_code=403, detail="Origin not allowed")
    return origin


def _page_origin(value: str) -> str:
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


@router.post("/fiber/session", response_model=FiberChatSessionResponse)
def fiber_widget_session_create(
    payload: FiberChatSessionCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> FiberChatSessionResponse:
    """Start one anonymous fiber-site chat inside native Team Inbox."""

    allowed_origin = _fiber_origin(request)
    if _page_origin(str(payload.page_url)) != allowed_origin:
        raise HTTPException(
            status_code=400, detail="Page URL is outside the fiber site"
        )
    elapsed = (datetime.now(UTC) - payload.started_at.astimezone(UTC)).total_seconds()
    if elapsed < 2 or elapsed > 86400:
        raise HTTPException(status_code=400, detail="Invalid form timing")
    decision = allow_operation(
        f"fiber-chat-session:{client_ip(request)}",
        limit=5,
        window_seconds=900,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many chat starts. Please try again later.",
            headers={
                "Retry-After": str(decision.retry_after_seconds or 900),
            },
        )
    outcome = _widget_call(
        lambda: team_inbox_widget.broker_fiber_visitor_session_committed(
            db,
            command=team_inbox_widget.FiberWidgetSessionCommand(
                client_session_id=payload.client_session_id,
                full_name=payload.full_name,
                email=str(payload.email),
                phone=payload.phone,
                message=payload.message,
                page_url=str(payload.page_url),
                referrer_url=str(payload.referrer_url)
                if payload.referrer_url is not None
                else None,
                started_at=payload.started_at,
                actor="transport:fiber-website-chat",
            ),
        )
    )
    return FiberChatSessionResponse(
        session_id=outcome.session_id,
        visitor_token=outcome.visitor_token,
        conversation_id=outcome.conversation_id,
        message_id=outcome.message_id,
        ws_url=outcome.ws_url,
        api_base=outcome.api_base,
        resolution_status=outcome.resolution_status,
        replayed=outcome.replayed,
    )


@router.get("/session/{session_id}/messages")
def widget_session_messages(
    session_id: str,
    limit: int = 50,
    x_visitor_token: str | None = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
) -> dict:
    principal = _principal(db, x_visitor_token)
    if principal.session_id != session_id:
        raise HTTPException(status_code=403, detail="Session mismatch")
    return _widget_call(
        lambda: team_inbox_widget.list_session_messages(
            db,
            principal=principal,
            limit=limit,
        )
    )


@router.post("/session/{session_id}/message")
def widget_session_message_create(
    session_id: str,
    payload: WidgetMessageCreate,
    request: Request,
    x_visitor_token: str | None = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
) -> dict:
    principal = _principal(db, x_visitor_token)
    decision = allow_operation(
        f"chat-widget-message:{principal.session_id}:{client_ip(request)}",
        limit=30,
        window_seconds=60,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many messages. Please slow down.",
            headers={
                "Retry-After": str(decision.retry_after_seconds or 60),
            },
        )
    return _widget_call(
        lambda: team_inbox_widget.add_visitor_message_committed(
            db,
            session_id=session_id,
            principal=principal,
            body=payload.body,
            client_message_id=payload.client_message_id,
        )
    )


@router.post("/session/{session_id}/read")
def widget_session_read(
    session_id: str,
    x_visitor_token: str | None = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
) -> dict:
    principal = _principal(db, x_visitor_token)
    return _widget_call(
        lambda: team_inbox_widget.mark_session_read_committed(
            db,
            session_id=session_id,
            principal=principal,
        )
    )


@router.post("/session/{session_id}/satisfaction")
def widget_session_satisfaction(
    session_id: str,
    payload: WidgetSatisfactionCreate,
    x_visitor_token: str | None = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
) -> dict:
    principal = _principal(db, x_visitor_token)
    return _widget_call(
        lambda: team_inbox_widget.record_session_satisfaction_committed(
            db,
            session_id=session_id,
            principal=principal,
            rating=payload.rating,
            comment=payload.comment,
        )
    )
