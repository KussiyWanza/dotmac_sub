from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_customer_layout_hides_chat_during_read_only_impersonation() -> None:
    layout = _read("templates/layouts/customer.html")

    assert "{% if not (customer and customer.read_only) %}" in layout
    assert '{% set chat_session_endpoint = "/portal/chat/session" %}' in layout
    assert '{% include "components/chat_widget.html" %}' in layout


def test_chat_widget_has_accessible_dialog_and_history_error_ui() -> None:
    template = _read("templates/components/chat_widget.html")

    assert 'role="dialog"' in template
    assert 'aria-modal="true"' in template
    assert 'aria-labelledby="dm-chat-title"' in template
    assert 'id="dm-chat-empty"' in template
    assert 'id="dm-chat-history-error"' in template
    assert 'id="dm-chat-history-retry"' in template


def test_live_chat_js_handles_reconnect_subscription_and_pending_identity() -> None:
    js = _read("static/js/live-chat.js")

    assert "loadHistory(false).then(subscribeConversation)" in js
    assert "data-live-chat-ready" in js
    assert 'type: "subscribe"' in js
    assert "topic: `conversation:${state.conversationId}`" in js
    assert "updateConversationId(m.conversation_id)" in js
    assert "client_message_id: clientId" in js
    assert "sending: false" in js
    assert "if (state.sending) return" in js
    assert "setSending(true)" in js
    assert "setSending(false)" in js
    assert "reconcilePendingMessage(clientId" in js
    assert (
        "if (reconcilePendingMessage(clientId, m.message_id || m.id, m.created_at))"
        in js
    )
    assert "function reconcileOutboundEcho" in js
    assert "Math.abs(created - rowCreated) > 30000" in js
    assert "reconcileOutboundEcho(id, body, createdAt)" in js
    assert "delete state.pending[clientId]" in js
    assert "isAgentMessage(payload)" in js
    assert "payload.sender_type" in js
    assert "payload.from_customer" in js
    assert 'if (ev.key === "Escape")' in js
    assert 'document.addEventListener("keydown", trapFocus)' in js


def test_live_chat_css_covers_contrast_pending_and_safe_area_states() -> None:
    css = _read("static/css/live-chat.css")

    assert "--dm-chat-out-text: #0f172a" in css
    assert "--dm-chat-in-text: #0f172a" in css
    assert ".dark .dm-chat" in css
    assert ".dm-chat-msg-pending" in css
    assert ".dm-chat-msg-failed" in css
    assert ".dm-chat-msg-meta" in css
    assert "env(safe-area-inset-bottom)" in css
    assert "caret-color" in css
    assert "color: #0f172a !important" in css
    assert "-webkit-text-fill-color: #0f172a" in css
    assert "#dm-chat #dm-chat-input" in css
    assert ".dm-chat-msg-out" in css and "background: #dbeafe !important" in css
    assert ".dm-chat-msg-in" in css and "background: #e2e8f0 !important" in css
    assert ".dark .dm-chat-msg-in" in css and "-webkit-text-fill-color: #f8fafc" in css


def test_public_fiber_widget_uses_native_team_inbox_contract() -> None:
    js = _read("static/js/fiber-chat-widget.js")

    assert 'absoluteHttp("/widget/fiber/session")' in js
    assert 'form_version: "fiber-chat-v1"' in js
    assert "client_session_id: clientId" in js
    assert 'values.get("company_website")' in js
    assert 'topic: "conversation:" + session.conversation_id' in js
    assert "session.api_base + path" in js
    assert "session.ws_url" in js
    assert "crm.dotmac.io" not in js


def test_public_fiber_widget_websocket_is_origin_scoped() -> None:
    router = _read("app/websocket/router.py")

    assert 'auth_result.get("surface") == "fiber_website"' in router
    assert "settings.fiber_chat_allowed_origin" in router
    assert 'close(code=4003, reason="Origin not allowed")' in router
