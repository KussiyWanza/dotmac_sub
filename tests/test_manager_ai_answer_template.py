"""Exercise the real inbox template environment without AI or database calls."""

import pytest
from starlette.requests import Request

from app.services.team_inbox_manager_ai_chat import ManagerChatPageState
from app.web.admin import inbox


@pytest.mark.parametrize("mode", ["period", "conversation"])
def test_manager_ai_answer_formats_and_escapes_model_output(mode: str) -> None:
    state = ManagerChatPageState(
        conversations=(),
        selected_conversation_id=None,
        question="What needs attention?",
        answer='**Attention**\n\n- Follow up\n<script>alert("model")</script>',
        error=None,
        provider_enabled=True,
        generation_enabled=True,
        mode=mode,
        period="last_7_days",
        custom_start="",
        custom_end="",
        channel_type="",
        status_filter="",
        channel_options=(),
        status_options=(),
        period_facts=None,
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/inbox/manager-ai",
            "headers": [],
            "query_string": b"mode=period",
        }
    )
    template = inbox.templates.get_template("admin/inbox/manager_ai.html")
    # Render the real page content, excluding the unrelated admin shell's services.
    context = template.new_context({"request": request, "state": state})
    rendered = "".join(template.blocks["content"](context))

    assert "<strong>Attention</strong>" in rendered
    assert "<li>Follow up</li>" in rendered
    assert '&lt;script&gt;alert("model")&lt;/script&gt;' in rendered
    assert "<script>" not in rendered
