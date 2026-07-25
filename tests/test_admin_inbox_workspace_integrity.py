"""Guards for the inbox workspace contract fixed in slices 1 and 2.

These are markup/wiring assertions rather than behaviour tests: each defect
they cover was a case where the owning service already did the right thing and
the workspace simply failed to call it, so the regression surface is the
template and the composer script, not the command boundary.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import team_inbox_projection

CONVERSATION = Path("templates/admin/inbox/_conversation.html").read_text()
DRAWER = Path("templates/admin/inbox/_contact_drawer.html").read_text()
SIDEBAR = Path("templates/admin/inbox/_sidebar.html").read_text()
EMPTY_STATE = Path("templates/admin/inbox/_empty_state.html").read_text()
JAVASCRIPT = Path("static/js/admin-inbox.js").read_text()


# --- Slice 1: read state -------------------------------------------------


def test_thread_publishes_unread_state_and_composer_clears_it():
    """The workspace rendered unread state it never cleared server-side."""
    assert "data-conversation-unread=" in CONVERSATION
    assert 'thread.dataset.conversationUnread === "true"' in JAVASCRIPT
    assert "markConversationRead" in JAVASCRIPT
    assert "/read" in JAVASCRIPT


def test_mark_read_posts_with_csrf_header():
    marker = JAVASCRIPT.index("async markConversationRead")
    body = JAVASCRIPT[marker : marker + 500]
    assert '"X-CSRF-Token": csrfToken()' in body
    assert 'method: "POST"' in body


# --- Slice 1: reply provenance ------------------------------------------


def test_reply_form_submits_macro_and_template_identity():
    """reply() accepts macro_id/template_id; the composer must send them.

    Without template_id an outbound WhatsApp send carries no approved
    provider-template identity.
    """
    assert 'name="macro_id"' in CONVERSATION
    assert 'name="template_id"' in CONVERSATION
    assert "resolvedMacroId()" in CONVERSATION
    assert "resolvedTemplateId()" in CONVERSATION


def test_macro_menu_dispatches_identity_not_just_text():
    assert '"macroId"' in CONVERSATION


def test_identity_is_released_when_the_agent_edits_the_body():
    """reply() substitutes the template body server-side, so a stale identity
    claim would discard the agent's edits and write a false audit record."""
    for fn in ("resolvedMacroId()", "resolvedTemplateId()"):
        marker = JAVASCRIPT.index(fn.rstrip("()") + "(")
        body = JAVASCRIPT[marker : marker + 160]
        assert "this.draft === this.identityBody" in body

    assert "releaseIdentity()" in JAVASCRIPT
    assert "claimIdentity(" in JAVASCRIPT


def test_ad_hoc_insertions_do_not_claim_identity():
    marker = JAVASCRIPT.index("insertQuickResponse(payload)")
    body = JAVASCRIPT[marker : marker + 700]
    assert "this.releaseIdentity()" in body


# --- Slice 1: duplicate cohort ------------------------------------------


def test_assignment_counts_expose_one_unreplied_cohort():
    fields = team_inbox_projection.InboxAssignmentCounts.__dataclass_fields__
    assert "unreplied" in fields
    assert "needs_attention" not in fields, (
        "needs_attention duplicated unreplied with an identical value"
    )


@pytest.mark.parametrize("markup", [SIDEBAR, EMPTY_STATE])
def test_duplicate_cohort_filter_is_gone(markup):
    assert "needs_attention" not in markup
    assert "applyAssignmentFilter('attention')" not in markup


def test_attention_token_no_longer_routed():
    """The command palette also routed this token; with the branch removed it
    fell through to assigned_person_id and filtered by a bogus id."""
    assert '"attention"' not in JAVASCRIPT
    assert 'applyAssignmentFilter("attention")' not in JAVASCRIPT


# --- Slice 2: collaboration ---------------------------------------------


def test_drawer_exposes_comment_thread_and_resolution():
    assert "/comments" in DRAWER
    assert "/resolve" in DRAWER
    assert "timeline.comments" in DRAWER


def test_composer_can_save_draft_as_macro_or_template():
    assert "/macros/create" in CONVERSATION
    assert "/templates/create" in CONVERSATION


# --- Slice 2: customer context sensitivity -------------------------------


def test_financial_and_network_detail_are_permission_gated():
    """Arrears and session IP are more sensitive than the conversation."""
    assert "can_view_financials" in DRAWER
    assert "can_view_network_detail" in DRAWER

    routes = Path("app/web/admin/inbox.py").read_text()
    assert 'can(request, "billing:account:read")' in routes
    assert 'can(request, "network:ip:read")' in routes


def test_arrears_hidden_without_billing_access_shows_reason():
    assert "Billing detail hidden" in DRAWER


def test_ip_is_gated_independently_of_connection_status():
    """Offline/last-seen answers the support question; the IP is separate."""
    # Anchor on the connection panel, not the identity badge that also reads
    # `subscriber_summary.connection` higher up the drawer.
    marker = DRAWER.index("{% set connection = subscriber_summary.connection %}")
    block = DRAWER[marker : marker + 1400]
    assert "Offline" in block
    assert "connection.last_seen_at" in block
    assert "can_view_network_detail and connection.ip" in block
