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
COMMENT_THREAD = Path("templates/admin/inbox/_comment_thread.html").read_text()
DRAWER = Path("templates/admin/inbox/_contact_drawer.html").read_text()
EMPTY_STATE = Path("templates/admin/inbox/_empty_state.html").read_text()
AUTHORITATIVE_CONTEXT = Path(
    "templates/admin/inbox/_authoritative_context.html"
).read_text()
CONVERSATION_HISTORY = Path(
    "templates/admin/inbox/_conversation_history.html"
).read_text()
FLOATING_SURFACES = Path("templates/admin/inbox/_floating_surfaces.html").read_text()
INDEX = Path("templates/admin/inbox/index.html").read_text()
COMMENTS = Path("templates/admin/inbox/comments.html").read_text()
LAYOUT = Path("templates/layouts/admin.html").read_text()
OVERLAYS = Path("templates/admin/inbox/_overlays.html").read_text()
QUEUE = Path("templates/admin/inbox/_queue_macros.html").read_text()
SIDEBAR = Path("templates/admin/inbox/_sidebar.html").read_text()
TICKET_PANEL = Path("templates/admin/inbox/_ticket_panel.html").read_text()
TRIAGE = Path("templates/components/ui/triage.html").read_text()
JAVASCRIPT = Path("static/js/admin-inbox.js").read_text()
REPLICA_CSS = Path("static/css/admin-inbox-replica.css").read_text()
ROUTES = Path("app/web/admin/inbox.py").read_text()


# --- Workspace frame -----------------------------------------------------


def test_workspace_frame_fills_the_viewport_below_the_admin_topbar():
    assert 'class="flex h-dvh overflow-hidden' in LAYOUT
    assert 'style="height: 100vh; height: 100dvh;"' in LAYOUT
    assert 'class="flex min-w-0 flex-1 flex-col overflow-hidden"' in LAYOUT
    assert "flex h-16 shrink-0 items-center" in LAYOUT
    assert '<main class="relative min-h-0 flex-1' in LAYOUT

    wrapper = INDEX.index('id="inbox-content-wrapper"')
    alpine = INDEX.index('x-data="inboxWorkspace')
    frame = INDEX.index("flex h-full min-h-0 w-full overflow-hidden rounded-2xl border")

    assert wrapper < alpine < frame
    assert "inbox-content-wrapper mx-auto h-full min-h-0 w-full max-w-none" in INDEX
    assert "box-border px-4 py-6 sm:px-6 lg:px-8" in INDEX
    assert 'class="relative h-full min-h-0 w-full"' in INDEX
    assert "border-slate-200/60 bg-white" in INDEX
    assert "dark:border-slate-700/60 dark:bg-slate-900" in INDEX
    assert "bg-slate-100 bg-noise bg-mesh dark:bg-slate-900" in INDEX


def test_crm_replication_surfaces_exclude_customer_placeholder_data():
    assert 'href="/static/css/admin-inbox-replica.css' in INDEX
    assert 'data-replica-placeholder="reply-failure"' in FLOATING_SURFACES
    assert 'data-replica-placeholder="incoming-whatsapp-call"' in FLOATING_SURFACES
    assert "Dummy data" not in DRAWER
    assert "Profile completeness" not in AUTHORITATIVE_CONTEXT
    assert "Retention risk" not in AUTHORITATIVE_CONTEXT
    assert "contact_context.tickets" in AUTHORITATIVE_CONTEXT
    assert "contact_context.projects" in AUTHORITATIVE_CONTEXT
    assert "crm_preview" in JAVASCRIPT
    assert "data-social-comment-thread" in COMMENT_THREAD
    assert 'action="/admin/inbox/{{ timeline.id }}/reply"' in COMMENT_THREAD
    assert 'name="whatsapp_template_components"' in OVERLAYS
    assert 'name="subscriber_id"' in OVERLAYS
    assert "newConversation.templateFields" in OVERLAYS
    assert "whatsapp-contacts" in JAVASCRIPT
    assert "contact.subscriber_id" in JAVASCRIPT

    assert "inbox-ticket-panel" in TICKET_PANEL
    assert ':action="`/admin/inbox/${selectedId}/tickets`"' in TICKET_PANEL
    assert "ticketPanelOpen" not in OVERLAYS

    for contract in (
        "padding-block: 1.5rem",
        "padding-inline: 1rem",
        "@media (min-width: 640px)",
        "@media (min-width: 1024px)",
        "width: 30rem",
        "max-width: 42rem",
        "max-height: 22rem",
        "z-index: 200",
    ):
        assert contract in REPLICA_CSS


def test_contact_drawer_exposes_authoritative_conversation_history_tab():
    assert 'role="tablist"' in DRAWER
    assert "contactTab = 'conversations'" in DRAWER
    assert "conversation_history.total_count" in DRAWER
    assert 'role="tabpanel"' in DRAWER
    assert "contact_context.recent_conversations" in CONVERSATION_HISTORY
    assert 'href="{{ item.url }}"' in CONVERSATION_HISTORY
    assert 'datetime="{{ item.last_message_at.isoformat() }}"' in CONVERSATION_HISTORY
    assert (
        "Showing the {{ history.items | length }} most recent" in CONVERSATION_HISTORY
    )


def test_social_comments_have_dedicated_workspace_and_filter_entry_point():
    main_channel_options = tuple(
        item.value
        for item in team_inbox_projection.InboxChannelType
        if item.value not in team_inbox_projection.SOCIAL_COMMENT_CHANNELS
    )

    assert 'href="/admin/inbox/comments"' in SIDEBAR
    assert "social_comment_count" in SIDEBAR
    assert "facebook_comment" not in main_channel_options
    assert "instagram_comment" not in main_channel_options
    assert 'name="channel_type"' in COMMENTS
    assert 'action="/admin/inbox/{{ selected_id }}/reply"' in COMMENTS
    assert (
        'name="reply_to_message_id" value="{{ node.reply_context.message_id }}"'
        in COMMENTS
    )
    assert "parent_provider_comment_id" not in COMMENTS
    assert "provider_comment_id" not in COMMENTS
    assert "provider_account_id" not in COMMENTS
    assert "post media" in COMMENTS.lower()
    assert "max-w-[1800px]" in COMMENTS
    assert "md:w-[360px]" in COMMENTS
    assert 'aria-label="Post comments"' in COMMENTS
    assert 'aria-label="Post media"' in COMMENTS
    assert "Top-level public comments unavailable" in COMMENTS
    assert "data-inline-comment-reply" in COMMENTS
    assert '"/comments"' in ROUTES
    assert "can_reply_to_social_comments" in COMMENTS
    assert "selected.action_eligibility" not in COMMENTS
    assert '"can_reply_to_social_comments": can_reply_to_social_comments' in ROUTES


# --- Slice 1: read state -------------------------------------------------


def test_thread_publishes_unread_state_and_composer_clears_it():
    """The workspace rendered unread state it never cleared server-side."""
    assert "data-conversation-unread=" in CONVERSATION
    assert 'thread.dataset.conversationUnread === "true"' in JAVASCRIPT
    assert "markConversationRead" in JAVASCRIPT
    assert "/read" in JAVASCRIPT


def test_mark_read_posts_with_csrf_header():
    marker = JAVASCRIPT.index("async markConversationRead")
    body = JAVASCRIPT[marker : marker + 1200]
    assert '"X-CSRF-Token": csrfToken()' in body
    assert 'method: "POST"' in body
    assert 'Accept: "application/json"' in body
    assert "this.applyConversationRead(id)" in body
    assert 'this.refreshSidebar("read_state")' not in body
    assert "this.markConversationRead(id, 1)" in body


def test_mark_read_updates_only_the_unread_row_and_total():
    assert "data-inbox-unread-total" in SIDEBAR
    assert "data-conversation-unread=" in QUEUE
    assert "conversationIsLocallyRead" in QUEUE
    marker = JAVASCRIPT.index("applyConversationRead(conversationId)")
    body = JAVASCRIPT[marker : marker + 1300]
    assert 'row.dataset.conversationUnread = "false"' in body
    assert 'document.querySelector("[data-inbox-unread-total]")' in body
    assert "Math.max(0, current - 1)" in body
    assert 'get("unread") === "true"' in body
    assert 'this.refreshConversationList("read_state")' in body


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


def test_reply_submission_refreshes_inbox_fragments_without_page_navigation():
    assert 'hx-post="/admin/inbox/{{ timeline.id }}/reply"' in CONVERSATION
    assert 'hx-swap="none"' in CONVERSATION
    assert '@inbox-reply-completed.window="completeSend($event.detail)"' in CONVERSATION
    assert "completeSend(result)" in JAVASCRIPT
    assert "workspace?.refreshThreadForMessage?.(" in JAVASCRIPT
    assert "/messages/${id}" in JAVASCRIPT
    assert "this.refreshConversationRow(conversationId)" in JAVASCRIPT
    assert 'workspace?.refreshConversationList?.("reply")' not in JAVASCRIPT
    assert 'this.draft = ""' in JAVASCRIPT
    assert "window.location.reload" not in JAVASCRIPT
    assert "admin-inbox.js?v=20260817b" in INDEX


def test_reply_toast_tracks_authoritative_delivery_without_covering_send_action():
    assert "data-inbox-send-toast" in INDEX
    assert "bottom-20 left-1/2" in INDEX
    assert 'role="status"' in INDEX
    assert 'aria-live="polite"' in INDEX

    prepare = JAVASCRIPT.index("      prepareSend(event)")
    prepare_body = JAVASCRIPT[prepare : prepare + 1800]
    assert 'showToast?.("Message sending…", { persistent: true })' in prepare_body

    completion = JAVASCRIPT.index("      completeSend(result)")
    completion_body = JAVASCRIPT[completion : completion + 2100]
    assert "workspace?.trackOutboundSend?.(result.message_id)" in completion_body
    assert 'workspace?.showToast?.("Message scheduled.")' in completion_body

    delivery = JAVASCRIPT.index("      applyDeliveryStatus(data)")
    delivery_body = JAVASCRIPT[delivery : delivery + 2100]
    assert "messageId === this.outboundToastMessageId" in delivery_body
    assert 'this.showToast("Message sent.")' in delivery_body
    assert 'this.showToast("Message sending…", { persistent: true })' in delivery_body


def test_reply_and_realtime_refresh_the_message_once():
    marker = JAVASCRIPT.index("refreshThreadForMessage(conversationId")
    body = JAVASCRIPT[marker : marker + 1300]
    assert "recentlyRefreshedMessageIds.has(id)" in body
    assert "recentlyRefreshedMessageIds.add(id)" in body
    assert "recentlyRefreshedMessageIds.delete(id)" in body
    assert 'swap: "beforeend"' in body

    realtime_marker = JAVASCRIPT.index('eventType === "message_new"')
    realtime_body = JAVASCRIPT[realtime_marker : realtime_marker + 900]
    assert (
        "this.refreshThreadForMessage(this.selectedId, data.message_id)"
        in realtime_body
    )


def test_reply_request_always_releases_send_busy_state():
    assert "@htmx:after-request=" not in CONVERSATION
    assert '"htmx:afterRequest"' in JAVASCRIPT
    assert '"htmx:sendAbort"' in JAVASCRIPT
    assert '"htmx:timeout"' in JAVASCRIPT
    assert '"htmx:sendError"' in JAVASCRIPT
    assert '"htmx:responseError"' in JAVASCRIPT
    assert "this.$cleanup(() => this.replyLifecycleCleanup?.())" in JAVASCRIPT

    marker = JAVASCRIPT.index("finishSendRequest(event)")
    body = JAVASCRIPT[marker : marker + 850]
    assert body.index("this.sending = false") < body.index(
        "if (this.replyOutcomeHandled) return"
    )
    assert "this.replyOutcomeFromEvent(event)" in body
    assert "this.completeSend(outcome)" in body
    assert "Check the thread before retrying" in body


def test_reply_response_fallback_reads_the_typed_htmx_outcome():
    marker = JAVASCRIPT.index("replyOutcomeFromEvent(event)")
    body = JAVASCRIPT[marker : marker + 500]
    assert 'getResponseHeader?.("HX-Trigger")' in body
    assert 'JSON.parse(raw)["inbox-reply-completed"]' in body
    assert "replyOutcomeHandled: false" in JAVASCRIPT


def test_delivery_status_waits_for_the_exact_message_fragment():
    marker = JAVASCRIPT.index("      applyDeliveryStatus(data) {")
    body = JAVASCRIPT[marker : marker + 1500]
    assert "this.pendingDeliveryStatuses.set(messageId" in body
    assert "this.refreshThread(this.selectedId)" not in body
    assert "this.pendingDeliveryStatuses.delete(messageId)" in body
    assert "this.applyPendingDeliveryStatuses()" in JAVASCRIPT


def test_incremental_refresh_is_bounded_and_has_stable_fragment_targets():
    assert "window.htmx.config.timeout = 15000" in JAVASCRIPT
    assert 'id="inbox-message-list"' in CONVERSATION
    assert 'data-inbox-message-id="{{ entry.message.id }}"' in CONVERSATION
    assert 'id="inbox-conversation-row-{{ row.id }}"' in QUEUE


def test_delivery_status_updates_in_place_from_authoritative_realtime_hint():
    assert 'data-inbox-delivery-status="{{ message.id }}"' in TRIAGE
    assert 'eventType === "message_status_changed"' in JAVASCRIPT
    assert "applyDeliveryStatus(data)" in JAVASCRIPT


def test_conversation_drilldown_and_reply_fallback_preserve_queue_page_state():
    assert "conversation_queue_item(row, list_query" in QUEUE
    assert "conversation_url = list_query.url('/admin/inbox')" in QUEUE
    assert 'hx-push-url="{{ conversation_url }}"' in QUEUE
    assert "conversation_queue_item(row, list_query" in SIDEBAR
    assert 'name="next_url"' in CONVERSATION
    assert "queue_return_url | default('/admin/inbox')" in CONVERSATION
    assert ':value="window.__inboxReturnUrl ||' in CONVERSATION

    refresh_marker = JAVASCRIPT.index("refreshConversationList(intent")
    refresh_body = JAVASCRIPT[refresh_marker : refresh_marker + 900]
    assert "window.__inboxReturnUrl || window.location.href" in refresh_body
    assert 'url.pathname = "/admin/inbox"' in refresh_body
    assert 'url.searchParams.set("c", this.selectedId)' in refresh_body
    assert 'historyMode: intent === "reply" ? "replace" : "none"' in refresh_body

    pagination_marker = JAVASCRIPT.index("navigatePage(urlValue)")
    pagination_body = JAVASCRIPT[pagination_marker : pagination_marker + 500]
    assert 'url.searchParams.set("c", this.selectedId)' in pagination_body
    assert "window.__inboxReturnUrl" in pagination_body


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
    assert SIDEBAR.count('role="tooltip"') == 5
    assert "h-9 w-9" in SIDEBAR
    assert "hover:bg-amber-50 hover:text-amber-600" in SIDEBAR
    assert "text-emerald-600 hover:bg-emerald-50 hover:text-emerald-700" in SIDEBAR
    assert "text-slate-500 hover:bg-slate-100 hover:text-slate-700" in SIDEBAR
    assert "bg-amber-50 text-amber-700" in SIDEBAR
    assert "Manager dashboard" in SIDEBAR
    assert "Manager AI" in SIDEBAR
    assert '@click="toggleManagerDashboard()"' in SIDEBAR
    assert "{% if can_manage_inbox %}" in SIDEBAR
    assert "can(request, 'support:inbox_ai:read')" in SIDEBAR
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
        'aria-controls="inbox-stats-filters"'
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
    assert "this.requestInboxList(url" in body
    assert 'intent: "search"' in body
    assert 'historyMode: "replace"' in body


def test_stats_filters_scroll_without_hiding_the_conversation_queue():
    summary_filters = SIDEBAR.index('aria-label="Inbox views and statistics"')
    filter_panel = SIDEBAR.index('id="inbox-stats-filters"')
    filter_form = SIDEBAR.index('id="inbox-filter-form"')
    assert filter_panel < summary_filters < filter_form
    assert 'class="inbox-filter-scroll space-y-3 pt-2"' in SIDEBAR
    assert filter_panel < SIDEBAR.index('id="inbox-conversation-queue"')
    for contract in (
        "max-height: min(30dvh, 22rem)",
        "overflow-y: auto",
        "overscroll-behavior: contain",
        "scrollbar-gutter: stable",
        "#inbox-conversation-queue",
        "min-height: 8rem",
    ):
        assert contract in REPLICA_CSS


def test_sidebar_filters_replace_stale_requests_and_expose_busy_state():
    assert 'hx-sync="this:replace"' in SIDEBAR
    assert 'hx-sync="#inbox-sidebar-content:replace"' in SIDEBAR
    assert ':aria-busy="filterLoading.toString()"' in SIDEBAR
    assert "Checking for updates" in JAVASCRIPT
    assert "stale.xhr.abort()" in JAVASCRIPT
    assert "if (this.filterLoading) return" in JAVASCRIPT
    assert 'document.body.addEventListener("htmx:sendAbort", release)' in JAVASCRIPT
    assert "InboxQueueComposition.sidebar" in ROUTES
    assert "InboxQueueComposition.queue_only" in ROUTES
    assert "manager_dashboard = None" in ROUTES
    assert 'hx-get="/admin/inbox/manager-dashboard"' in SIDEBAR
    assert "def team_inbox_manager_dashboard(" in ROUTES
    assert 'htmx_target == "inbox-conversation-queue"' in ROUTES


def test_inbox_refresh_status_precedes_stats_filters_and_conversation_list():
    status = SIDEBAR.index("data-inbox-refresh-status")
    disclosure = SIDEBAR.index('aria-controls="inbox-stats-filters"')
    queue = SIDEBAR.index('id="inbox-conversation-queue"')
    assert status < disclosure < queue
    for label in (
        "Waiting for new activity",
        "Checking for updates",
        "Inbox updated just now",
        "Couldn’t update — retrying",
    ):
        assert label in SIDEBAR or label in JAVASCRIPT
    for contract in (
        'inboxRefreshState: "idle"',
        "this.inboxRefreshStarted()",
        "this.inboxRefreshFinished(requestFailed)",
        "event.detail?.successful === false",
    ):
        assert contract in JAVASCRIPT


def test_conversation_click_shows_loading_without_hiding_list_until_swap():
    pane = INDEX.index("data-conversation-pane")
    loader = INDEX.index('x-show="conversationOpening"')
    detail = INDEX.index('id="triage-detail"')
    assert pane < loader < detail
    pane_markup = INDEX[pane - 100 : detail]
    assert "relative flex min-w-0 flex-1 overflow-hidden" in pane_markup
    assert "absolute inset-0" in pane_markup

    select_start = JAVASCRIPT.index("selectConversation(id) {")
    select_end = JAVASCRIPT.index("updateSelectedHighlight()", select_start)
    select_block = JAVASCRIPT[select_start:select_end]
    assert "this.selectedId = id;" in select_block
    assert 'this.beginDetailRequest(id, "navigation", true);' in select_block
    assert 'setAttribute("data-triage-mode", "detail")' not in select_block

    swap_start = JAVASCRIPT.index('if (target.id === "triage-detail")')
    swap_end = JAVASCRIPT.index(
        'if (\n            target.id === "inbox-sidebar-content"', swap_start
    )
    swap_block = JAVASCRIPT[swap_start:swap_end]
    assert 'this.mode = "detail";' in swap_block
    assert "this.conversationOpening = false;" in swap_block
    assert 'setAttribute("data-triage-mode", "detail")' in swap_block


def test_conversation_detail_requests_are_latest_request_wins():
    for contract in (
        "detailRequestSequence: 0",
        "activeDetailRequest: null",
        "pendingDetailRequest: null",
        "__inboxDetailSequence",
        "stale.xhr.abort()",
        "detailSequence !== this.detailRequestSequence",
        "event.detail.shouldSwap = false",
    ):
        assert contract in JAVASCRIPT
    release = JAVASCRIPT.index("detailSequence === this.activeDetailRequest?.sequence")
    release_body = JAVASCRIPT[release : release + 520]
    assert "this.activeDetailRequest = null" in release_body
    assert "this.conversationOpening = false" in release_body
    assert "cancelDetailRequest()" in JAVASCRIPT
    assert "active?.xhr.abort()" in JAVASCRIPT
    show_list = JAVASCRIPT.index("showList() {")
    assert "this.cancelDetailRequest()" in JAVASCRIPT[show_list : show_list + 180]


def test_background_refresh_protects_dirty_composer_without_blocking_navigation():
    assert ':data-composer-dirty="composerDirty().toString()"' in CONVERSATION
    marker = JAVASCRIPT.index("composerHasTransientState()")
    body = JAVASCRIPT[marker : marker + 1100]
    assert 'composer?.dataset.composerDirty === "true"' in body
    assert "this.composerFocused() || this.composerHasTransientState()" in body
    assert "Boolean(options.blocking)" in body
    assert 'intent = "realtime"' in JAVASCRIPT
    assert "scheduleThreadRefresh(conversationId" in JAVASCRIPT
    assert "window.clearTimeout(this.threadRefreshTimer)" in JAVASCRIPT


def test_contact_detail_requests_do_not_change_selected_conversation_and_reject_stale_swaps():
    marker = JAVASCRIPT.index("openContact(id)")
    body = JAVASCRIPT[marker : marker + 220]
    assert "this.beginContactRequest(id)" in body
    assert "this.selectedId = id" not in body
    for contract in (
        "contactRequestSequence: 0",
        "__inboxContactSequence",
        "contactSequence !== this.contactRequestSequence",
    ):
        assert contract in JAVASCRIPT


def test_searches_cancel_stale_responses_and_fragment_cleanup_releases_resources():
    assert "const fetchWithTimeout = async" in JAVASCRIPT
    assert "window.setTimeout(() => controller.abort(), timeoutMs)" in JAVASCRIPT
    assert "this.contactSearchController?.abort()" in JAVASCRIPT
    assert "sequence !== this.contactSearchSequence" in JAVASCRIPT
    assert "searchController = new AbortController()" in JAVASCRIPT
    assert "sequence !== searchSequence" in JAVASCRIPT
    assert "htmx:beforeCleanupElement" in JAVASCRIPT
    assert "window.clearInterval(element.__inboxReplyWindowTimer)" in JAVASCRIPT
    assert 'document.removeEventListener("click", closeOnOutsideClick)' in JAVASCRIPT


def test_realtime_subscriptions_are_reconciled_to_visible_topics():
    marker = JAVASCRIPT.index("      subscribeVisibleTopics() {")
    body = JAVASCRIPT[marker : marker + 1300]
    assert 'type: "unsubscribe"' in body
    assert "if (this.subscribedTopics.has(topic)) return" in body
    assert "this.subscribedTopics = desiredTopics" in body


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
        "max-w-2xl",
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
        "fixed left-1/2 top-1/2",
        "h-[calc(100vh-6rem)]",
        "w-[calc(100%-2rem)] max-w-2xl",
        "-translate-x-1/2 -translate-y-1/2",
        "scale-95 opacity-0",
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


def test_inbox_icons_match_the_crm_paths_without_text_substitutes():
    chat_path = (
        "M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 "
        "9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 "
        "12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
    )
    assert chat_path in SIDEBAR
    assert chat_path in EMPTY_STATE
    assert "M12 5v14m7-7H5" in SIDEBAR
    assert "M12 5v14m7-7H5" in EMPTY_STATE
    assert "M12 5v14m7-7H5" in OVERLAYS
    assert "M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7" in SIDEBAR
    assert "M12 6v6m0 0v6m0-6h6m-6 0H6" in EMPTY_STATE
    assert "M16 7a4 4 0 11-8 0 4 4 0 018 0z" in CONVERSATION
    assert "M12 19l9 2-9-18-9 18 9-2zm0 0v-8" in CONVERSATION
    assert 'aria-label="Send message"' in CONVERSATION
    assert "M5 12h14M13 5l7 7-7 7" not in CONVERSATION

    for channel_path in (
        "M3 8l7.89 5.26a2 2 0 002.22 0L21 8",
        "M12.04 2c-5.46 0-9.91 4.45-9.91 9.91",
        "M24 12.073c0-6.627-5.373-12-12-12",
        "M12 0C8.74 0 8.333.015 7.053.072",
    ):
        assert channel_path in QUEUE
    assert ">f</span>" not in QUEUE
    assert '<rect x="4" y="4"' not in QUEUE
    assert 'channel_icon("whatsapp", "h-3 w-3")' in FLOATING_SURFACES
    assert "☎" not in FLOATING_SURFACES


def test_channel_colours_are_page_scoped_and_cover_every_supported_alias():
    expected_mappings = {
        'data-inbox-channel="email"': "--channel-color: 139, 92, 246",
        'data-inbox-channel="whatsapp"': "--channel-color: 34, 197, 94",
        'data-inbox-channel="sms"': "--channel-color: 249, 115, 22",
        'data-inbox-channel="phone"': "--channel-color: 249, 115, 22",
        'data-inbox-channel="telegram"': "--channel-color: 14, 165, 233",
        'data-inbox-channel="chat_widget"': "--channel-color: 245, 158, 11",
        'data-inbox-channel="facebook_messenger"': "--channel-color: 24, 119, 242",
        'data-inbox-channel="instagram_dm"': "--channel-color: 236, 72, 153",
    }
    for selector, colour in expected_mappings.items():
        assert selector in REPLICA_CSS
        assert colour in REPLICA_CSS

    assert "[data-inbox-workspace] .inbox-channel-indicator" in REPLICA_CSS
    assert "background: rgb(var(--channel-color))" in REPLICA_CSS
    assert "box-shadow: 0 0 12px rgba(var(--channel-color), .4)" in REPLICA_CSS
    assert "facebook_comment" in REPLICA_CSS
    assert "instagram_comment" in REPLICA_CSS
    assert 'data-inbox-channel="{{ timeline.channel_type }}"' in CONVERSATION
    assert 'data-inbox-channel="{{ timeline.channel_type }}"' in DRAWER
    assert 'data-inbox-channel="{{ channel_type }}"' in SIDEBAR
    assert 'data-inbox-channel="{{ conversation.channel_type }}"' in OVERLAYS


def test_ticket_and_send_actions_use_the_exact_cyan_treatment():
    assert "background: #0891b2" in REPLICA_CSS
    assert "background: #0e7490" in REPLICA_CSS
    assert "inbox-create-ticket-action" in CONVERSATION
    assert "inbox-create-ticket-action" in DRAWER
    assert "inbox-create-ticket-action" in TICKET_PANEL
    assert "M12 5v14m7-7H5" in CONVERSATION
    assert "M12 5v14m7-7H5" in DRAWER
    assert "M12 5v14m7-7H5" in TICKET_PANEL
    assert "from-[#06B6D4] to-[#0891B2]" in CONVERSATION
    assert "inbox-send-action" in CONVERSATION
    assert "box-shadow: 0 10px 24px rgba(6, 182, 212, .3)" in REPLICA_CSS
    assert ".inbox-send-action:focus-visible" in REPLICA_CSS


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
        "@click.prevent='navigatePage(",
        "border border-slate-200 bg-white",
        "hover:bg-slate-50",
        "page_meta.start_item",
        "page_meta.navigation",
        'aria-current="page" aria-label="Page {{ page_number }}"',
        ">Previous</a>",
        ">Next</a>",
    ):
        assert contract in QUEUE
    assert '@click.prevent="navigatePage(' not in QUEUE
    assert "Rows per page" not in QUEUE


def test_realtime_activity_waits_for_an_explicit_queue_refresh():
    marker = JAVASCRIPT.index('refreshConversationList(intent = "manual_refresh")')
    body = JAVASCRIPT[marker : marker + 850]
    assert "this.newListActivityAvailable = false" in body
    assert 'target: "#inbox-conversation-queue"' in body
    assert 'select: "#inbox-conversation-queue"' in body
    assert 'swap: "outerHTML"' in body
    event_marker = JAVASCRIPT.index("this.newListActivityAvailable = true")
    event_body = JAVASCRIPT[event_marker - 250 : event_marker + 500]
    assert "this.refreshSidebar()" not in event_body


def test_every_list_request_uses_one_latest_request_wins_coordinator():
    for contract in (
        "requestInboxList(urlValue, options = {})",
        "beginListRequest(intent, operator = false)",
        "__inboxListSequence",
        "sequence !== this.listRequestSequence",
        "event.detail.shouldSwap = false",
        'intent: "search"',
        'intent: "pagination"',
        'intent: "history"',
        'this.refreshSidebar("poll")',
    ):
        assert contract in JAVASCRIPT


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
    assert SIDEBAR.count(group_classes) == 3
    assert 'name="status" value="open"' in SIDEBAR
    assert 'name="status" value="pending"' in SIDEBAR
    assert 'name="has_ticket" value="true"' in SIDEBAR
    assert 'name="status" value="resolved"' in SIDEBAR
    assert "bg-emerald-500" in SIDEBAR
    assert "bg-amber-500" in SIDEBAR


def test_blank_priority_is_omitted_from_inbox_htmx_filter_requests():
    assert 'form?.id !== "inbox-filter-form"' in JAVASCRIPT
    assert 'event.detail.parameters?.priority_at_most === ""' in JAVASCRIPT
    assert "delete event.detail.parameters.priority_at_most" in JAVASCRIPT
    assert "priority_at_most: str | None = Query(default=None)" in ROUTES


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


def test_stats_and_filters_use_one_grouped_progressive_disclosure():
    assert "Inbox Stats &amp; Filters" in SIDEBAR
    assert 'x-text="activeFilterCount()"' in SIDEBAR
    assert "Inbox views &amp; stats" in SIDEBAR
    assert ">Active</span>" in SIDEBAR
    assert "Active filters" in SIDEBAR
    assert "activeFilterChips()" in SIDEBAR
    assert "removeActiveFilter(chip)" in SIDEBAR
    for group in (
        "Status",
        "Assignment",
        "Attention",
        "Routing",
        "Advanced",
        "Saved views",
    ):
        assert f"<span>{group}</span>" in SIDEBAR
    assert SIDEBAR.count("<span>Unreplied</span>") == 1
    assert SIDEBAR.count("<span>Unassigned</span>") == 1
    assert 'aria-label="Secondary inbox filters"' not in SIDEBAR
    assert "activeFilterChips()" in JAVASCRIPT
    assert "activeFilterCount()" in JAVASCRIPT
    assert "removeActiveFilter(chip)" in JAVASCRIPT


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
