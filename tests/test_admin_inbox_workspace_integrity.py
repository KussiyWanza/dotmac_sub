"""Guards for the inbox workspace contract fixed in slices 1 and 2.

These are markup/wiring assertions rather than behaviour tests: each defect
they cover was a case where the owning service already did the right thing and
the workspace simply failed to call it, so the regression surface is the
template and the composer script, not the command boundary.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5.
"""

from __future__ import annotations

from pathlib import Path

from app.services import team_inbox_projection

CONVERSATION = Path("templates/admin/inbox/_conversation.html").read_text()
DRAWER = Path("templates/admin/inbox/_contact_drawer.html").read_text()
INDEX = Path("templates/admin/inbox/index.html").read_text()
OVERLAYS = Path("templates/admin/inbox/_overlays.html").read_text()
QUEUE = Path("templates/admin/inbox/_queue_macros.html").read_text()
SIDEBAR = Path("templates/admin/inbox/_sidebar.html").read_text()
JAVASCRIPT = Path("static/js/admin-inbox.js").read_text()
ROUTES = Path("app/web/admin/inbox.py").read_text()


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


# --- Response cohorts ---------------------------------------------------


def test_assignment_counts_expose_distinct_response_cohorts():
    fields = team_inbox_projection.InboxAssignmentCounts.__dataclass_fields__
    assert "unreplied" in fields
    assert "needs_attention" in fields


def test_needs_attention_has_its_own_filter_token_and_count():
    assert "assignment_counts.needs_attention" in SIDEBAR
    assert "applyAssignmentFilter('attention')" in SIDEBAR
    assert 'needs_attention: "true"' in JAVASCRIPT
    assert 'filters.get("needs_attention") === "true"' in JAVASCRIPT


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


# --- Stats and filters refactor -----------------------------------------


def test_sidebar_shell_header_and_icon_use_the_page_scoped_contract():
    for class_name in (
        "border-slate-200/60 bg-slate-50/50",
        "dark:border-slate-700/60 dark:bg-slate-900/60",
    ):
        assert class_name in INDEX
    for class_name in (
        "flex items-center justify-between gap-2 border-b border-slate-200/60 px-5 py-4",
        "h-11 w-11",
        "rounded-xl",
        "from-amber-500 to-orange-600",
        "shadow-lg shadow-amber-500/25",
        "h-[22px] w-[22px]",
        "text-lg font-bold text-slate-900",
        "font-semibold tabular-nums text-amber-600 dark:text-amber-400",
    ):
        assert class_name in SIDEBAR


def test_header_actions_have_live_states_and_tooltips():
    assert SIDEBAR.count('role="tooltip"') == 4
    assert "h-9 w-9" in SIDEBAR
    assert "hover:bg-amber-50 hover:text-amber-600" in SIDEBAR
    assert "text-emerald-600 hover:bg-emerald-50 hover:text-emerald-700" in SIDEBAR
    assert "text-slate-500 hover:bg-slate-100 hover:text-slate-700" in SIDEBAR
    assert "bg-amber-50 text-amber-700" in SIDEBAR
    assert "Manager dashboard" in SIDEBAR
    assert '@click="toggleManagerDashboard()"' in SIDEBAR
    assert "{% if can_manage_inbox %}" in SIDEBAR
    assert 'href="/admin/crm/inbox/settings"' in SIDEBAR


def test_live_indicator_and_search_match_the_sidebar_contract():
    for class_name in (
        "text-emerald-600 dark:text-emerald-400",
        "animate-pulse bg-emerald-500",
        "text-red-500 dark:text-red-400",
        "bg-red-500",
    ):
        assert class_name in SIDEBAR
    search_marker = 'id="inbox-conversation-search"'
    assert SIDEBAR.index("</header>") < SIDEBAR.index(search_marker)
    assert SIDEBAR.index(search_marker) < SIDEBAR.index(
        'aria-label="Inbox summary filters"'
    )
    assert 'placeholder="Search conversations..."' in SIDEBAR
    assert '@input.debounce.300ms="searchConversations($el.value)"' in SIDEBAR
    for class_name in (
        "px-4 py-3",
        "w-full rounded-xl border border-slate-200 bg-white py-2.5 pl-10 pr-4",
        "placeholder-slate-400 transition-all",
        "focus:border-amber-500 focus:ring-2 focus:ring-amber-500/20",
        "dark:border-slate-600 dark:bg-slate-800/50 dark:text-white",
        "dark:placeholder-slate-500 dark:focus:border-amber-500",
    ):
        assert class_name in SIDEBAR

    marker = JAVASCRIPT.index("searchConversations(value)")
    body = JAVASCRIPT[marker : marker + 700]
    assert 'url.searchParams.set("search", search)' in body
    assert 'url.searchParams.delete("page")' in body
    assert "conversation_id" in body
    assert 'target: "#inbox-sidebar-content"' in body


def test_sidebar_resize_handle_has_exact_shape_states_and_tooltip():
    marker = INDEX.index('aria-label="Drag to resize inbox"')
    handle = INDEX[max(0, marker - 3000) : marker + 2500]
    for class_name in (
        "absolute right-0 top-1/2 z-30",
        "h-14 w-3",
        "cursor-ew-resize",
        "rounded-l-lg rounded-r-none",
        "border border-r-0",
        "border-[rgba(245,158,11,0.45)]",
        "bg-[rgba(255,251,235,0.95)]",
        "shadow-[0_8px_18px_rgba(15,23,42,0.12)]",
        "dark:bg-[rgba(120,53,15,0.90)]",
        "dark:border-[rgba(245,158,11,0.50)]",
        "dark:shadow-[0_8px_18px_rgba(0,0,0,0.30)]",
        "h-[3px] w-[3px]",
        "gap-[3px]",
        "bg-[#D97706]",
        "dark:bg-[#FBBF24]",
        "left-full top-1/2 ml-[10px]",
        "max-w-56",
        "rounded-[6px]",
        "text-[11px] font-semibold",
        "bg-[#0F172A]",
        "dark:bg-[#F8FAFC] dark:text-[#0F172A]",
    ):
        assert class_name in handle
    assert 'x-show="!managerDashboardOpen"' in handle
    assert "hidden" in handle and "sm:flex" in handle
    assert "Drag to resize inbox" in handle


def test_sidebar_resize_drag_state_is_bounded_and_persisted():
    marker = JAVASCRIPT.index("startSidebarResize(event)")
    body = JAVASCRIPT[marker : marker + 2600]
    assert "window.innerWidth <= 639" in body
    assert "this.resizingSidebar = true" in body
    assert "this.resizingSidebar = false" in body
    assert 'document.body.style.cursor = "ew-resize"' in body
    assert 'document.body.style.userSelect = "none"' in body
    assert "288" in body
    assert "448" in body
    assert "localStorage.setItem(KEYS.sidebarWidth" in body
    assert "releasePointerCapture" in body


def test_new_conversation_modal_matches_layout_and_live_channel_contract():
    for class_name in (
        "bg-slate-950/70",
        "backdrop-blur-sm",
        "px-4 py-8",
        "max-w-[672px]",
        "rounded-2xl",
        "border-slate-200/60",
        "dark:border-slate-700/60 dark:bg-slate-900",
        "scale-95 opacity-0",
        "h-10 w-10",
        "from-amber-500 to-orange-600",
        "grid grid-cols-1 gap-4 sm:grid-cols-2",
        "rounded-xl border border-slate-200 bg-slate-50/50",
        "focus:border-amber-500 focus:ring-2 focus:ring-amber-500/20",
        "from-[#FE9A00] to-[#E17100]",
        "hover:-translate-y-0.5",
    ):
        assert class_name in OVERLAYS
    for field_name in (
        "channel_type",
        "service_team_id",
        "contact_name",
        "contact_address",
        "subject",
        "cc",
        "bcc",
        "template_id",
        "template_values",
        "body_text",
        "files",
    ):
        assert f'name="{field_name}"' in OVERLAYS
    assert '@click.self="closeNewConversation()"' in OVERLAYS
    assert '@submit="prepareNewConversation()"' in OVERLAYS
    assert (
        "newConversationSubmitting ? 'Starting\\u2026' : 'Start conversation'"
        in OVERLAYS
    )
    assert "facebook_messenger" in OVERLAYS
    assert "instagram_dm" in OVERLAYS
    assert 'enctype="multipart/form-data"' in OVERLAYS


def test_manager_dashboard_is_permission_gated_floating_and_non_blocking():
    assert "{% if can_manage_inbox and manager_dashboard %}" in OVERLAYS
    assert 'can_manage_inbox = can(request, "support:ticket:update")' in ROUTES
    assert "build_manager_dashboard_projection(" in ROUTES
    marker = OVERLAYS.index('id="inbox-manager-dashboard"')
    panel = OVERLAYS[marker : marker + 18000]
    for contract in (
        "fixed inset-x-4 top-20",
        "h-[calc(100vh-6rem)]",
        "sm:left-auto sm:w-[448px]",
        "rounded-xl border border-slate-200 bg-white",
        "dark:border-slate-700 dark:bg-slate-900",
        "grid grid-cols-2 gap-3",
        "grid grid-cols-3 gap-2",
        "grid grid-cols-5 gap-1",
        "sm:grid-cols-4",
        "Online Agents",
        "Chats With Online Agents",
        "Needs Attention",
        "Resolved Today",
        "Channel Split",
        "Active Chats",
    ):
        assert contract in panel
    assert 'aria-modal="false"' in panel
    assert '@click.outside="managerDashboardOpen = false"' in panel
    assert '@keydown.escape.window="managerDashboardOpen = false"' in panel
    assert "bg-slate-950/70" not in panel


def test_header_secondary_actions_do_not_open_modals():
    assert '@click="toggleSound()"' in SIDEBAR
    assert "this.soundEnabled = !this.soundEnabled" in JAVASCRIPT
    assert 'href="/admin/crm/inbox/settings"' in SIDEBAR
    assert 'settings_router = APIRouter(prefix="/crm/inbox"' in ROUTES
    assert "team_inbox_settings_entrypoint" in ROUTES


def test_queue_section_is_a_vertical_stack_in_the_requested_order():
    activity = SIDEBAR.index("New activity in the inbox.")
    bulk = SIDEBAR.index('id="inbox-bulk-form"')
    conversation_list = SIDEBAR.index("data-conversation-list")
    empty = SIDEBAR.index("No conversations.")
    pagination = SIDEBAR.index("inbox_pagination(")
    assert activity < bulk < conversation_list < empty < pagination
    assert 'id="inbox-conversation-queue"' in SIDEBAR
    assert "flex min-h-0 flex-1 flex-col" in SIDEBAR
    assert 'x-show="newListActivityAvailable"' in SIDEBAR
    assert '@click="refreshConversationList()"' in SIDEBAR
    assert "rounded-lg border border-amber-200 bg-amber-50" in SIDEBAR
    assert "bg-amber-600" in SIDEBAR
    assert "hover:bg-amber-700" in SIDEBAR


def test_bulk_toolbar_is_horizontal_and_uses_live_action_fields():
    marker = SIDEBAR.index('id="inbox-bulk-form"')
    toolbar = SIDEBAR[marker : marker + 8000]
    for contract in (
        "flex items-center gap-2 overflow-x-auto whitespace-nowrap",
        "border-slate-200/60 bg-white/80",
        "dark:border-slate-700/60 dark:bg-slate-900/70",
        'name="action"',
        'name="status_value"',
        'name="priority"',
        'name="service_team_id"',
        'name="label_id"',
        "disabled:bg-slate-400",
        '@click="clearSelection()"',
    ):
        assert contract in toolbar
    assert toolbar.index("x-show=\"bulkAction === 'label'\"") < toolbar.index(
        'name="label_id"'
    )


def test_inbox_queue_row_has_exact_structure_states_and_channel_colours():
    for contract in (
        "border-b border-l-[3px] border-slate-200",
        "dark:border-slate-800/50",
        "px-4 py-3.5",
        "h-4 w-4",
        "h-11 w-11",
        "from-[#314158] to-[#1D293D]",
        "h-4 w-4",
        "border-l-[#F59E0B]",
        "from-[rgba(245,158,11,0.12)]",
        "to-[rgba(249,115,22,0.06)]",
        "hover:from-[rgba(245,158,11,0.08)]",
        "hover:to-[rgba(249,115,22,0.04)]",
        "bg-[#06B6D4]",
    ):
        assert contract in QUEUE
    for colour in (
        "#8B5CF6",
        "#22C55E",
        "#1877F2",
        "#EC4899",
        "#F59E0B",
        "#F97316",
        "#0EA5E9",
    ):
        assert colour in QUEUE
    assert "row.labels[:2]" in QUEUE
    assert "Sent to ticket" in QUEUE
    for priority in ("Low", "Medium", "High", "Urgent"):
        assert f'"{priority}"' in QUEUE


def test_empty_state_and_inbox_pagination_are_scoped_to_the_queue():
    for contract in (
        "h-16 w-16",
        "border border-slate-200 bg-slate-100",
        "No conversations.",
        "No results for &lsquo;{{ search }}&rsquo;",
        "All caught up!",
    ):
        assert contract in SIDEBAR
    for contract in (
        'hx-target="#inbox-conversation-queue"',
        'hx-select="#inbox-conversation-queue"',
        'hx-swap="outerHTML"',
        "border border-slate-200 bg-white",
        "hover:bg-slate-50",
        "Page {{ page_meta.page }}",
        ">Back</a>",
        ">Next</a>",
    ):
        assert contract in QUEUE
    assert "Rows per page" not in QUEUE


def test_realtime_activity_waits_for_an_explicit_queue_refresh():
    marker = JAVASCRIPT.index("refreshConversationList()")
    body = JAVASCRIPT[marker : marker + 650]
    assert "this.newListActivityAvailable = false" in body
    assert 'target: "#inbox-conversation-queue"' in body
    assert 'select: "#inbox-conversation-queue"' in body
    assert 'swap: "outerHTML"' in body
    event_marker = JAVASCRIPT.index("this.newListActivityAvailable = true")
    event_body = JAVASCRIPT[event_marker - 250 : event_marker + 500]
    assert "this.refreshSidebar()" not in event_body


def test_stats_filter_header_uses_the_page_scoped_amber_contract():
    for class_name in (
        "border-amber-200",
        "bg-amber-50",
        "text-amber-800",
        "hover:border-amber-300",
        "hover:bg-amber-100",
        "dark:border-amber-500/30",
        "dark:bg-amber-500/10",
        "dark:hover:border-amber-400/50",
        "dark:hover:bg-amber-500/20",
    ):
        assert class_name in SIDEBAR


def test_status_and_assignment_filters_use_flexible_wrapping_groups():
    group_classes = (
        "flex flex-wrap gap-1 rounded-xl bg-slate-100 p-1 dark:bg-slate-800/50"
    )
    assert SIDEBAR.count(group_classes) == 2
    assert 'name="status" value="open"' in SIDEBAR
    assert 'name="status" value="pending"' in SIDEBAR
    assert 'name="has_ticket" value="true"' in SIDEBAR
    assert 'name="status" value="resolved"' in SIDEBAR
    assert "bg-emerald-500" in SIDEBAR
    assert "bg-amber-500" in SIDEBAR


def test_assignment_filter_colours_and_counts_are_present():
    for label in (
        "Assigned to me",
        "My Team",
        "AI handling",
        "Unassigned",
        "Unreplied",
        "Needs attention",
        "By Agent",
    ):
        assert label in SIDEBAR
    for selected_colour in (
        "text-emerald-700",
        "text-cyan-700",
        "text-indigo-700",
        "text-amber-700",
        "text-rose-700",
        "text-orange-700",
        "text-violet-700",
    ):
        assert selected_colour in SIDEBAR
    assert "assignmentFilterActive" in SIDEBAR
    assert "assignmentFilterActive(value)" in JAVASCRIPT


def test_needs_attention_is_a_live_saved_filter():
    assert "showDemoNotice('Needs-attention filtering')" not in SIDEBAR
    assert 'needs_attention: "needs_attention"' in JAVASCRIPT
    assert "needs_attention: bool = Query(default=False)" in ROUTES
    assert "needs_attention: bool = Form(default=False)" in ROUTES


def test_channel_and_team_remain_separate_and_inbox_selector_stays_hidden():
    assert 'name="channel_type"' in SIDEBAR
    assert 'name="service_team_id"' in SIDEBAR
    assert ">Channel</span>" in SIDEBAR
    assert ">Team</span>" in SIDEBAR
    assert 'name="receiving_account_id"' not in SIDEBAR
    assert 'name="mailbox_id"' not in SIDEBAR


def test_by_agent_panel_uses_live_agent_and_activity_filters():
    assert 'id="inbox-by-agent-panel"' in SIDEBAR
    assert 'name="assigned_person_id"' in SIDEBAR
    assert 'name="activity_from"' in SIDEBAR
    assert 'name="activity_to"' in SIDEBAR
    assert "border-slate-200/60 bg-slate-50" in SIDEBAR
    assert "focus:border-violet-500" in SIDEBAR


def test_saved_views_have_active_state_and_save_all_live_filter_fields():
    assert "savedViewIsActive" in SIDEBAR
    assert "border-amber-300 bg-amber-50" in SIDEBAR
    assert "border-slate-200 bg-white" in SIDEBAR
    assert "bg-amber-600" in SIDEBAR
    assert "hover:text-red-500" in SIDEBAR
    for field_name in (
        "service_team_ids",
        "assigned_person_id",
        "needs_attention",
        "unread",
        "ai_handling",
        "has_ticket",
        "activity_from",
        "activity_to",
    ):
        assert f'{field_name}: "{field_name}"' in JAVASCRIPT
        assert f"{field_name}: " in ROUTES
