/* Admin omnichannel inbox workspace.
 *
 * Server projections remain authoritative. This controller owns only browser
 * interaction, local preferences, draft state, realtime hints, and explicitly
 * labelled demo adapters for capabilities whose APIs are not available yet.
 */
(function () {
  "use strict";

  const KEYS = {
    sidebarWidth: "dotmac.inbox.sidebarWidth",
    filtersOpen: "dotmac.inbox.filtersOpen",
    soundEnabled: "dotmac.inbox.soundEnabled",
    draftPrefix: "dotmac.inbox.draft.",
  };
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const parseStoredBoolean = (key, fallback) => {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value === "true";
  };
  const editableTarget = (target) =>
    Boolean(
      target &&
        (target.matches("input, textarea, select, [contenteditable='true']") ||
          target.closest("[contenteditable='true']")),
    );
  const csrfToken = () =>
    document.querySelector('meta[name="csrf-token"]')?.content || "";
  const fetchWithTimeout = async (url, options = {}, timeoutMs = 15000) => {
    const controller = new AbortController();
    const upstreamSignal = options.signal;
    const abortFromUpstream = () => controller.abort();
    if (upstreamSignal?.aborted) controller.abort();
    else upstreamSignal?.addEventListener("abort", abortFromUpstream, {
      once: true,
    });
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(url, { ...options, signal: controller.signal });
    } finally {
      window.clearTimeout(timer);
      upstreamSignal?.removeEventListener("abort", abortFromUpstream);
    }
  };

  window.inboxTeamFilterBuilder = function inboxTeamFilterBuilder(initialJson) {
    const blankCondition = (bucket = "and") => ({
      id: `${Date.now()}-${Math.random()}`,
      bucket,
      operator: "=",
      value: "",
      values: [],
    });
    const rowCondition = (row, bucket) => {
      if (!Array.isArray(row) || row.length < 4) return null;
      if (row[0] !== "InboxConversation" || row[1] !== "service_team_id") {
        return null;
      }
      const operator = String(row[2] || "=");
      const rawValue = row[3];
      return {
        id: `${Date.now()}-${Math.random()}`,
        bucket,
        operator,
        value: Array.isArray(rawValue) ? "" : String(rawValue || ""),
        values: Array.isArray(rawValue) ? rawValue.map(String) : [],
      };
    };
    const parseConditions = () => {
      if (!initialJson) return [];
      try {
        const parsed =
          typeof initialJson === "string" ? JSON.parse(initialJson) : initialJson;
        const entries = Array.isArray(parsed) ? parsed : [];
        const conditions = [];
        entries.forEach((entry) => {
          if (Array.isArray(entry)) {
            const condition = rowCondition(entry, "and");
            if (condition) conditions.push(condition);
            return;
          }
          if (entry && Array.isArray(entry.or)) {
            entry.or.forEach((row) => {
              const condition = rowCondition(row, "or");
              if (condition) conditions.push(condition);
            });
          }
        });
        return conditions;
      } catch (_error) {
        return [];
      }
    };
    const initialConditions = parseConditions();
    return {
      conditions: initialConditions,
      filtersJson: typeof initialJson === "string" ? initialJson : "",

      usesMany(condition) {
        return ["in", "not in"].includes(condition.operator);
      },

      needsTeam(condition) {
        return !["is", "is not"].includes(condition.operator);
      },

      addCondition() {
        this.conditions.push(
          blankCondition(
            this.conditions.some((item) => item.bucket === "and") ? "or" : "and",
          ),
        );
      },

      removeCondition(id) {
        this.conditions = this.conditions.filter((item) => item.id !== id);
        this.syncFilters();
      },

      operatorChanged(condition) {
        condition.value = "";
        condition.values = [];
        this.syncFilters();
      },

      transportRow(condition) {
        let value = null;
        if (this.usesMany(condition)) value = condition.values;
        else if (this.needsTeam(condition)) value = condition.value;
        return [
          "InboxConversation",
          "service_team_id",
          condition.operator,
          value,
        ];
      },

      syncFilters() {
        const ready = this.conditions.filter((condition) => {
          if (!this.needsTeam(condition)) return true;
          return this.usesMany(condition)
            ? condition.values.length > 0
            : Boolean(condition.value);
        });
        const andRows = ready
          .filter((condition) => condition.bucket === "and")
          .map((condition) => this.transportRow(condition));
        const orRows = ready
          .filter((condition) => condition.bucket === "or")
          .map((condition) => this.transportRow(condition));
        const payload = [...andRows];
        if (orRows.length) payload.push({ or: orRows });
        this.filtersJson = payload.length ? JSON.stringify(payload) : "";
      },

      apply(form) {
        this.syncFilters();
        form.requestSubmit();
      },

      clear(form) {
        this.conditions = [];
        this.filtersJson = "";
        form.requestSubmit();
      },
    };
  };

  window.inboxWorkspace = function inboxWorkspace(config) {
    const crmPreview =
      new URLSearchParams(window.location.search).get("crm_preview") || "";
    const previewIncludes = (name) =>
      crmPreview === "all" || crmPreview.split(",").includes(name);
    return {
      selectedId: config.selectedId || "",
      myTeamIds: config.myTeamIds || "",
      actorId: config.actorId || "",
      commentMode: Boolean(config.commentMode) || previewIncludes("comment"),
      crmPreview,
      mode: config.initialMode || "list",
      sidebarWidth: clamp(
        Number(localStorage.getItem(KEYS.sidebarWidth) || 320),
        288,
        448,
      ),
      resizingSidebar: false,
      filtersOpen: parseStoredBoolean(KEYS.filtersOpen, false),
      byAgentOpen: false,
      savedViewName: "",
      selectedIds: [],
      bulkAction: "status",
      soundEnabled: parseStoredBoolean(KEYS.soundEnabled, false),
      realtimeConnected: false,
      contactOpen: false,
      newConversationOpen: false,
      newConversationSubmitting: false,
      managerDashboardOpen: false,
      ticketPanelOpen: false,
      commandPaletteOpen: false,
      shortcutHelpOpen: false,
      commandQuery: "",
      presenceText: "",
      typingAgents: {},
      typingPruneTimer: null,
      newMessagesAvailable: false,
      newListActivityAvailable: false,
      toastMessage: "",
      outboundToastMessageId: "",
      replyFailure: previewIncludes("reply-failed")
        ? { detail: "The channel did not accept this message. Try again." }
        : null,
      realtimeNotifications: previewIncludes("notifications")
        ? [
            {
              id: "preview-reminder",
              kind: "reminder",
              title: "Follow-up reminder",
              subtitle: "Acme Fibre Upgrade",
              preview: "Customer asked for an update before close of business.",
              time: "Now",
            },
          ]
        : [],
      incomingCall: previewIncludes("incoming-call")
        ? { name: "Ada Customer" }
        : null,
      activeCall: crmPreview === "active-call"
        ? { name: "Ada Customer", seconds: 42 }
        : null,
      callMuted: false,
      callOnHold: false,
      socket: null,
      subscribedTopics: new Set(),
      reconnectTimer: null,
      reconnectAttempts: 0,
      pollTimer: null,
      typingTimer: null,
      inFlight: new Set(),
      recentlyRefreshedMessageIds: new Set(),
      pendingDeliveryStatuses: new Map(),
      threadRefreshTimer: null,
      readStateInFlight: new Set(),
      locallyReadConversationIds: [],
      filterLoading: false,
      inboxRefreshState: "idle",
      inboxRefreshTimer: null,
      conversationOpening: false,
      activeFilterXhr: null,
      pendingStatusFilter: null,
      listRequestSequence: 0,
      activeListRequest: null,
      pendingListRequest: null,
      listRequestError: "",
      lastSuccessfulListUrl: window.location.href,
      detailRequestSequence: 0,
      activeDetailRequest: null,
      pendingDetailRequest: null,
      contactRequestSequence: 0,
      activeContactRequest: null,
      pendingContactRequest: null,
      contactSearchSequence: 0,
      contactSearchController: null,
      newConversation: {
        channel: "email",
        contactName: "",
        contactId: "",
        subscriberId: "",
        contactQuery: "",
        contactResults: [],
        contactLoading: false,
        contactError: "",
        recipient: "",
        countryCode: "NG",
        subject: "",
        cc: "",
        bcc: "",
        template: "",
        selectedTemplate: null,
        whatsappTemplates: [],
        templateFields: [],
        templateLoading: false,
        templateError: "",
        templateValues: "",
        body: "",
        files: [],
        error: "",
      },
      ticketDraft: { title: "", priority: "normal", description: "" },
      commands: [
        { id: "new", label: "New conversation", hint: "Start an outbound conversation", shortcut: "N" },
        { id: "reply", label: "Focus reply composer", hint: "Jump to the current thread reply", shortcut: "R" },
        { id: "resolve", label: "Resolve current conversation", hint: "Mark the selected conversation resolved", shortcut: "E" },
        { id: "contact", label: "Toggle contact details", hint: "Show or hide customer context", shortcut: "" },
        { id: "ticket", label: "Create support ticket", hint: "Open the ticket split panel", shortcut: "" },
        { id: "unreplied", label: "Open unreplied", hint: "Filter conversations needing a reply", shortcut: "" },
      ],

      init() {
        document.documentElement.style.setProperty(
          "--inbox-sidebar-width",
          `${this.sidebarWidth}px`,
        );
        this.bindHtmx();
        this.connectRealtime();
        this.startFallbackPolling();
        this.scrollThread();
        this.clearDraftAfterSuccessfulSend();
        this.$nextTick(() => this.syncSelectedCheckboxes());
      },

      desktopSidebarStyle() {
        return `--inbox-sidebar-width:${this.sidebarWidth}px;width:var(--inbox-sidebar-width)`;
      },

      startSidebarResize(event) {
        if (window.innerWidth <= 639) return;
        event.preventDefault();
        const handle = event.currentTarget;
        const pointerId = event.pointerId;
        handle.setPointerCapture?.(pointerId);
        const startX = event.clientX;
        const startWidth = this.sidebarWidth;
        const previousBodyCursor = document.body.style.cursor;
        const previousBodyUserSelect = document.body.style.userSelect;
        const previousRootCursor = document.documentElement.style.cursor;
        this.resizingSidebar = true;
        document.body.style.cursor = "ew-resize";
        document.body.style.userSelect = "none";
        document.documentElement.style.cursor = "ew-resize";
        const move = (moveEvent) => {
          if (moveEvent.pointerId !== pointerId) return;
          moveEvent.preventDefault();
          this.sidebarWidth = clamp(
            startWidth + moveEvent.clientX - startX,
            288,
            448,
          );
          document.documentElement.style.setProperty(
            "--inbox-sidebar-width",
            `${this.sidebarWidth}px`,
          );
        };
        const stop = (stopEvent) => {
          if (
            stopEvent?.pointerId !== undefined &&
            stopEvent.pointerId !== pointerId
          ) {
            return;
          }
          localStorage.setItem(KEYS.sidebarWidth, String(this.sidebarWidth));
          this.resizingSidebar = false;
          document.body.style.cursor = previousBodyCursor;
          document.body.style.userSelect = previousBodyUserSelect;
          document.documentElement.style.cursor = previousRootCursor;
          if (handle.hasPointerCapture?.(pointerId)) {
            handle.releasePointerCapture(pointerId);
          }
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", stop);
          window.removeEventListener("pointercancel", stop);
          window.removeEventListener("blur", stop);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", stop);
        window.addEventListener("pointercancel", stop);
        window.addEventListener("blur", stop);
      },

      persistFilters() {
        localStorage.setItem(KEYS.filtersOpen, String(this.filtersOpen));
      },

      inboxRefreshLabel() {
        if (this.inboxRefreshState === "checking") {
          return "Checking for updates…";
        }
        if (this.inboxRefreshState === "updated") {
          return "Inbox updated just now";
        }
        if (this.inboxRefreshState === "error") {
          return "Couldn’t update — retrying";
        }
        return "Waiting for new activity";
      },

      inboxRefreshStarted() {
        window.clearTimeout(this.inboxRefreshTimer);
        this.inboxRefreshState = "checking";
      },

      inboxRefreshFinished(failed = false) {
        window.clearTimeout(this.inboxRefreshTimer);
        this.inboxRefreshState = failed ? "error" : "updated";
        if (failed) return;
        this.inboxRefreshTimer = window.setTimeout(() => {
          if (this.inboxRefreshState === "updated") {
            this.inboxRefreshState = "idle";
          }
        }, 2200);
      },

      activeFilterChips() {
        const filters = new URLSearchParams(window.location.search);
        const chips = [];
        const add = (key, label, keys = [key]) => {
          if (filters.has(key)) chips.push({ key, label, keys });
        };
        const title = (value) =>
          String(value || "")
            .replaceAll("_", " ")
            .replace(/\b\w/g, (letter) => letter.toUpperCase());

        if (filters.get("unassigned") === "true") {
          chips.push({
            key: "unassigned",
            label: "Unassigned",
            keys: ["unassigned", "open_only"],
          });
        } else if (filters.get("open_only") === "true") {
          chips.push({ key: "open_only", label: "Active", keys: ["open_only"] });
        }
        if (filters.get("status")) {
          chips.push({
            key: "status",
            label: title(filters.get("status")),
            keys: ["status"],
          });
        }
        add("has_ticket", "Sent to ticket");
        add("needs_response", "Unreplied");
        add("needs_attention", "Needs attention");
        add("ai_handling", "AI handling");
        add("unread", "Unread");
        add("snoozed", "Snoozed");
        add("muted", "Muted");
        add("reply_window_status", "Reply window expired");
        if (filters.get("assigned_person_id")) {
          chips.push({
            key: "assigned_person_id",
            label:
              filters.get("assigned_person_id") === this.actorId
                ? "Assigned to me"
                : "By agent",
            keys: ["assigned_person_id"],
          });
        }
        add("service_team_ids", "My team");
        if (filters.get("channel_type")) {
          chips.push({
            key: "channel_type",
            label: title(filters.get("channel_type")),
            keys: ["channel_type"],
          });
        }
        add("service_team_id", "Team");
        add("priority_at_most", "Priority");
        add("filters", "Advanced team");
        add("activity_from", "Activity from");
        add("activity_to", "Activity to");
        return chips;
      },

      activeFilterCount() {
        return this.activeFilterChips().length;
      },

      removeActiveFilter(chip) {
        const url = new URL(window.location.href);
        (chip?.keys || []).forEach((key) => url.searchParams.delete(key));
        url.searchParams.delete("page");
        if (this.selectedId) {
          url.searchParams.set("conversation_id", this.selectedId);
        }
        this.requestInboxList(url, {
          intent: "operator_filter",
          historyMode: "push",
        });
      },

      toggleSound() {
        this.soundEnabled = !this.soundEnabled;
        localStorage.setItem(KEYS.soundEnabled, String(this.soundEnabled));
        if (this.soundEnabled) this.playSound();
      },

      playSound() {
        if (!this.soundEnabled || !window.AudioContext) return;
        try {
          const context = new AudioContext();
          const oscillator = context.createOscillator();
          const gain = context.createGain();
          oscillator.frequency.value = 660;
          gain.gain.setValueAtTime(0.04, context.currentTime);
          gain.gain.exponentialRampToValueAtTime(
            0.001,
            context.currentTime + 0.18,
          );
          oscillator.connect(gain);
          gain.connect(context.destination);
          oscillator.start();
          oscillator.stop(context.currentTime + 0.18);
        } catch (_error) {
          // Sound is an optional enhancement.
        }
      },

      bindHtmx() {
        if (window.__dotmacInboxHtmxBound) return;
        window.__dotmacInboxHtmxBound = true;
        // A stalled fragment must release the inbox loader and leave the
        // operator in control. HTMX defaults to no timeout.
        if (!window.htmx.config.timeout) window.htmx.config.timeout = 15000;
        document.body.addEventListener("htmx:configRequest", (event) => {
          const form = event.detail?.elt;
          if (form?.id !== "inbox-filter-form") return;
          if (event.detail.parameters?.priority_at_most === "") {
            delete event.detail.parameters.priority_at_most;
          }
        });
        document.body.addEventListener("htmx:beforeRequest", (event) => {
          const path = event.detail?.requestConfig?.path || "";
          const target = event.detail?.target?.id || "";
          if (
            target === "inbox-sidebar-content" ||
            target === "inbox-conversation-queue"
          ) {
            const request = this.pendingListRequest || {
              sequence: ++this.listRequestSequence,
              intent: "external",
              operator: false,
            };
            this.pendingListRequest = null;
            if (
              this.activeListRequest &&
              this.activeListRequest.sequence < request.sequence
            ) {
              const stale = this.activeListRequest;
              this.activeListRequest = null;
              stale.xhr.abort();
            }
            event.detail.xhr.__inboxListSequence = request.sequence;
            event.detail.xhr.__inboxListIntent = request.intent;
            this.activeListRequest = {
              ...request,
              xhr: event.detail.xhr,
            };
            this.inboxRefreshStarted();
            if (request.operator) {
              this.filterLoading = true;
              this.activeFilterXhr = event.detail.xhr;
            }
            return;
          }
          if (target === "triage-detail") {
            const request = this.pendingDetailRequest || {
              sequence: ++this.detailRequestSequence,
              conversationId: this.conversationIdFromPath(path) || this.selectedId,
              intent: "external",
              blocking: false,
            };
            this.pendingDetailRequest = null;
            if (
              this.activeDetailRequest &&
              this.activeDetailRequest.sequence < request.sequence
            ) {
              const stale = this.activeDetailRequest;
              this.activeDetailRequest = null;
              stale.xhr.abort();
            }
            event.detail.xhr.__inboxDetailSequence = request.sequence;
            event.detail.xhr.__inboxDetailConversationId = request.conversationId;
            event.detail.xhr.__inboxDetailBlocking = request.blocking;
            this.activeDetailRequest = { ...request, xhr: event.detail.xhr };
            this.conversationOpening = request.blocking;
            return;
          }
          if (target === "inbox-contact-content") {
            const request = this.pendingContactRequest || {
              sequence: ++this.contactRequestSequence,
              conversationId: this.conversationIdFromPath(path),
            };
            this.pendingContactRequest = null;
            if (
              this.activeContactRequest &&
              this.activeContactRequest.sequence < request.sequence
            ) {
              const stale = this.activeContactRequest;
              this.activeContactRequest = null;
              stale.xhr.abort();
            }
            event.detail.xhr.__inboxContactSequence = request.sequence;
            event.detail.xhr.__inboxContactConversationId = request.conversationId;
            this.activeContactRequest = { ...request, xhr: event.detail.xhr };
            return;
          }
          if (target === "inbox-message-list") {
            event.detail.xhr.__inboxMessageConversationId =
              event.detail.target
                ?.closest("[data-conversation-thread]")
                ?.dataset.conversationThread || this.conversationIdFromPath(path);
            event.detail.xhr.__inboxMessageDetailSequence =
              this.detailRequestSequence;
          }
          const key = `${event.detail?.requestConfig?.verb || "GET"}:${path}:${target}`;
          if (this.inFlight.has(key)) {
            event.preventDefault();
            return;
          }
          this.inFlight.add(key);
          event.detail.xhr.__inboxRequestKey = key;
        });
        const release = (event, failed = false) => {
          const sequence = event.detail?.xhr?.__inboxListSequence;
          const requestFailed = failed || event.detail?.successful === false;
          if (sequence === this.activeListRequest?.sequence) {
            const wasOperator = this.activeListRequest.operator;
            this.activeListRequest = null;
            this.inboxRefreshFinished(requestFailed);
            if (wasOperator) this.filterLoading = false;
          }
          if (event.detail?.xhr === this.activeFilterXhr) {
            this.activeFilterXhr = null;
            this.filterLoading = false;
            this.pendingStatusFilter = null;
          }
          if (requestFailed && sequence === this.listRequestSequence) {
            this.listRequestError = "Could not update conversations. Try again.";
            history.replaceState({}, "", this.lastSuccessfulListUrl);
          }
          const detailSequence = event.detail?.xhr?.__inboxDetailSequence;
          if (detailSequence === this.activeDetailRequest?.sequence) {
            const wasBlocking = this.activeDetailRequest.blocking;
            this.activeDetailRequest = null;
            if (wasBlocking && detailSequence === this.detailRequestSequence) {
              this.conversationOpening = false;
            }
            if (requestFailed && detailSequence === this.detailRequestSequence) {
              this.showToast("Could not open conversation. Try again.");
            }
          }
          const contactSequence = event.detail?.xhr?.__inboxContactSequence;
          if (contactSequence === this.activeContactRequest?.sequence) {
            this.activeContactRequest = null;
            if (requestFailed) {
              this.showToast("Could not load contact details. Try again.");
            }
          }
          if (failed && event.detail?.target?.id === "inbox-message-list") {
            this.newMessagesAvailable = true;
            this.showToast(
              "A new message is available. Refresh the thread to load it.",
            );
          }
          const key = event.detail?.xhr?.__inboxRequestKey;
          if (key) this.inFlight.delete(key);
        };
        document.body.addEventListener("htmx:afterRequest", release);
        document.body.addEventListener("htmx:sendAbort", release);
        document.body.addEventListener("htmx:timeout", (event) =>
          release(event, true),
        );
        document.body.addEventListener("htmx:sendError", (event) =>
          release(event, true),
        );
        document.body.addEventListener("htmx:responseError", (event) =>
          release(event, true),
        );
        document.body.addEventListener("htmx:beforeSwap", (event) => {
          const sequence = event.detail?.xhr?.__inboxListSequence;
          if (sequence && sequence !== this.listRequestSequence) {
            event.detail.shouldSwap = false;
          }
          const detailSequence = event.detail?.xhr?.__inboxDetailSequence;
          const detailConversationId =
            event.detail?.xhr?.__inboxDetailConversationId;
          if (
            detailSequence &&
            (detailSequence !== this.detailRequestSequence ||
              String(detailConversationId || "") !== String(this.selectedId || ""))
          ) {
            event.detail.shouldSwap = false;
          }
          const contactSequence = event.detail?.xhr?.__inboxContactSequence;
          if (contactSequence && contactSequence !== this.contactRequestSequence) {
            event.detail.shouldSwap = false;
          }
          const messageConversationId =
            event.detail?.xhr?.__inboxMessageConversationId;
          const messageDetailSequence =
            event.detail?.xhr?.__inboxMessageDetailSequence;
          if (
            messageConversationId &&
            (String(messageConversationId) !== String(this.selectedId || "") ||
              messageDetailSequence !== this.detailRequestSequence)
          ) {
            event.detail.shouldSwap = false;
          }
        });
        document.body.addEventListener("htmx:afterSwap", (event) => {
          const target = event.detail?.target;
          if (!target) return;
          if (target.id === "triage-detail") {
            this.mode = "detail";
            this.conversationOpening = false;
            document
              .querySelector("[data-triage-shell]")
              ?.setAttribute("data-triage-mode", "detail");
            const thread = target.querySelector("[data-conversation-thread]");
            if (thread) {
              this.selectedId = thread.dataset.conversationThread || "";
              this.clearTypingPresence();
              this.subscribeVisibleTopics();
              this.updateSelectedHighlight();
              this.scrollThread();
              this.newMessagesAvailable = false;
              if (thread.dataset.conversationUnread === "true") {
                this.markConversationRead(this.selectedId);
              }
              this.applyPendingDeliveryStatuses();
            }
          }
          if (target.id === "inbox-message-list") {
            if (!document.contains(target)) return;
            target.querySelector("[data-inbox-empty-thread]")?.remove();
            this.applyPendingDeliveryStatuses();
            this.newMessagesAvailable = false;
            this.scrollThread();
          }
          if (
            target.id === "inbox-sidebar-content" ||
            target.id === "inbox-conversation-queue"
          ) {
            this.lastSuccessfulListUrl = window.location.href;
            this.listRequestError = "";
            this.syncSelectedCheckboxes();
            this.updateSelectedHighlight();
            this.subscribeVisibleTopics();
          }
        });
        document.body.addEventListener("htmx:beforeCleanupElement", (event) => {
          this.cleanupInboxElement(event.detail?.elt);
        });
        document.addEventListener("click", (event) => {
          const link = event.target.closest(
            "#inbox-sidebar-content a[href^='/admin/inbox?']",
          );
          if (
            !link ||
            link.closest(".conversation-item") ||
            link.hasAttribute("hx-get") ||
            event.metaKey ||
            event.ctrlKey ||
            event.shiftKey ||
            event.altKey
          ) {
            return;
          }
          event.preventDefault();
          const url = new URL(link.href, window.location.origin);
          if (this.selectedId) {
            url.searchParams.set("conversation_id", this.selectedId);
          }
          this.requestInboxList(url, {
            intent: "operator_filter",
            historyMode: "push",
          });
        });
        window.addEventListener("popstate", () => {
          const url = new URL(window.location.href);
          const selected =
            url.searchParams.get("conversation_id") || url.searchParams.get("c");
          this.selectedId = selected || "";
          this.clearTypingPresence();
          this.requestInboxList(url, {
            intent: "history",
            historyMode: "none",
          });
          if (selected) {
            this.refreshThread(selected, true, {
              intent: "history",
              blocking: true,
            });
          }
          else this.showList();
        });
      },

      filterRequestStarted(status = null) {
        this.newMessagesAvailable = false;
        this.newListActivityAvailable = false;
        this.filterLoading = true;
        this.beginListRequest("operator_filter", true);
        if (status !== null) {
          this.pendingStatusFilter = status;
          const url = new URL(window.location.href);
          url.searchParams.delete("open_only");
          url.searchParams.delete("has_ticket");
          if (status) url.searchParams.set("status", status);
          else url.searchParams.delete("status");
          history.replaceState({}, "", url);
        }
      },

      beginListRequest(intent, operator = false) {
        const request = {
          sequence: ++this.listRequestSequence,
          intent,
          operator,
        };
        if (this.activeListRequest) {
          const stale = this.activeListRequest;
          this.activeListRequest = null;
          stale.xhr.abort();
        }
        this.pendingListRequest = request;
        this.listRequestError = "";
        if (operator) this.filterLoading = true;
        return request;
      },

      requestInboxList(urlValue, options = {}) {
        const url =
          urlValue instanceof URL
            ? urlValue
            : new URL(urlValue, window.location.origin);
        const intent = options.intent || "operator_filter";
        const operator = !["poll", "read_state", "realtime"].includes(intent);
        if (!operator && this.activeListRequest?.operator) return;
        this.beginListRequest(intent, operator);
        if (options.historyMode === "push") history.pushState({}, "", url);
        if (options.historyMode === "replace") history.replaceState({}, "", url);
        window.htmx.ajax("GET", `${url.pathname}${url.search}`, {
          target: options.target || "#inbox-sidebar-content",
          select: options.select,
          swap: options.swap || "innerHTML",
        });
      },

      conversationIdFromPath(path) {
        const match = String(path || "").match(
          /^\/admin\/inbox\/([0-9a-f-]{36})(?:\/|$)/i,
        );
        return match?.[1] || "";
      },

      beginDetailRequest(conversationId, intent, blocking) {
        const request = {
          sequence: ++this.detailRequestSequence,
          conversationId: String(conversationId || ""),
          intent,
          blocking: Boolean(blocking),
        };
        this.pendingDetailRequest = request;
        if (request.blocking) this.conversationOpening = true;
        return request;
      },

      beginContactRequest(conversationId) {
        this.pendingContactRequest = {
          sequence: ++this.contactRequestSequence,
          conversationId: String(conversationId || ""),
        };
      },

      cancelDetailRequest() {
        window.clearTimeout(this.threadRefreshTimer);
        this.threadRefreshTimer = null;
        this.detailRequestSequence += 1;
        this.pendingDetailRequest = null;
        const active = this.activeDetailRequest;
        this.activeDetailRequest = null;
        active?.xhr.abort();
        this.conversationOpening = false;
      },

      cancelContactRequest() {
        this.contactRequestSequence += 1;
        this.pendingContactRequest = null;
        const active = this.activeContactRequest;
        this.activeContactRequest = null;
        active?.xhr.abort();
      },

      showList() {
        this.cancelDetailRequest();
        this.mode = "list";
        this.clearTypingPresence();
        document
          .querySelector("[data-triage-shell]")
          ?.setAttribute("data-triage-mode", "list");
      },

      selectConversation(id) {
        const current = new URL(window.location.href);
        const returnUrl =
          current.pathname === "/admin/inbox"
            ? current
            : new URL(
                window.__inboxReturnUrl || "/admin/inbox",
                window.location.origin,
              );
        returnUrl.pathname = "/admin/inbox";
        returnUrl.searchParams.delete("conversation_id");
        returnUrl.searchParams.set("c", id);
        window.__inboxReturnUrl = `${returnUrl.pathname}${returnUrl.search}`;
        this.selectedId = id;
        this.pendingDeliveryStatuses.clear();
        this.beginDetailRequest(id, "navigation", true);
        this.newMessagesAvailable = false;
        this.clearTypingPresence();
        this.updateSelectedHighlight();
      },

      updateSelectedHighlight() {
        document.querySelectorAll(".conversation-item").forEach((row) => {
          const selected = row.dataset.conversationId === this.selectedId;
          row
            .querySelector("[data-conversation-link]")
            ?.toggleAttribute("aria-current", selected);
        });
      },

      toggleSelection(id, checked) {
        if (checked && !this.selectedIds.includes(id)) this.selectedIds.push(id);
        if (!checked) this.selectedIds = this.selectedIds.filter((item) => item !== id);
      },

      clearSelection() {
        this.selectedIds = [];
        document
          .querySelectorAll('#inbox-bulk-form input[name="conversation_ids"]')
          .forEach((input) => {
            input.checked = false;
          });
      },

      syncSelectedCheckboxes() {
        document
          .querySelectorAll('#inbox-bulk-form input[name="conversation_ids"]')
          .forEach((input) => {
            input.checked = this.selectedIds.includes(input.value);
          });
      },

      conversationIsLocallyRead(conversationId) {
        return this.locallyReadConversationIds.includes(String(conversationId));
      },

      applyConversationRead(conversationId) {
        const id = String(conversationId || "");
        if (!id || this.conversationIsLocallyRead(id)) return;
        const row = Array.from(
          document.querySelectorAll("[data-conversation-id]"),
        ).find((item) => item.dataset.conversationId === id);
        if (row?.dataset.conversationUnread !== "true") return;

        this.locallyReadConversationIds = [
          ...this.locallyReadConversationIds,
          id,
        ];
        row.dataset.conversationUnread = "false";
        const total = document.querySelector("[data-inbox-unread-total]");
        const current = Number.parseInt(total?.textContent || "0", 10);
        if (total && Number.isFinite(current)) {
          const next = Math.max(0, current - 1);
          total.textContent = String(next);
          total.setAttribute("aria-label", `${next} unread conversations`);
        }

        if (new URLSearchParams(window.location.search).get("unread") === "true") {
          this.refreshConversationList("read_state");
        }
      },

      navigateFilter(changes, clearAll = false) {
        const url = new URL(window.location.href);
        const assignmentKeys = [
          "status",
          "assigned_person_id",
          "service_team_ids",
          "unassigned",
          "needs_response",
          "needs_attention",
          "ai_handling",
          "activity_from",
          "activity_to",
          "open_only",
          "has_ticket",
          "page",
        ];
        const savedViewKeys = [
          ...assignmentKeys,
          "search",
          "channel_type",
          "service_team_id",
          "filters",
          "contact_resolution_status",
          "priority_at_most",
          "muted",
          "snoozed",
          "unread",
        ];
        (clearAll ? savedViewKeys : assignmentKeys).forEach((key) =>
          url.searchParams.delete(key),
        );
        // The two team params scope the same relation, so only one may be live
        // at a time. Setting either clears the other; leaving both in the URL
        // asked the server for two team filters at once, which it cannot
        // answer. The team scope is otherwise independent of the assignment
        // cohort above and is deliberately preserved across those clicks.
        if (changes && "service_team_ids" in changes) {
          url.searchParams.delete("service_team_id");
        }
        if (changes && "service_team_id" in changes) {
          url.searchParams.delete("service_team_ids");
        }
        Object.entries(changes || {}).forEach(([key, value]) => {
          if (value !== null && value !== undefined && value !== "") {
            url.searchParams.set(key, value);
          }
        });
        if (this.selectedId) {
          url.searchParams.set("conversation_id", this.selectedId);
        }
        this.requestInboxList(url, {
          intent: "operator_filter",
          historyMode: "push",
        });
      },

      searchConversations(value) {
        const url = new URL(window.location.href);
        const search = String(value || "").trim();
        if (search) url.searchParams.set("search", search);
        else url.searchParams.delete("search");
        url.searchParams.delete("page");
        if (this.selectedId) {
          url.searchParams.set("conversation_id", this.selectedId);
        }
        this.requestInboxList(url, {
          intent: "search",
          historyMode: "replace",
        });
      },

      // Operator read-state is server-owned. Opening an unread thread clears it
      // through the inbox command boundary; without this the workspace renders
      // an unread badge it can never retire.
      async markConversationRead(conversationId, retryAttempt = 0) {
        const id = String(conversationId || "");
        if (!id || this.readStateInFlight.has(id)) return;
        this.readStateInFlight.add(id);
        try {
          const response = await fetchWithTimeout(`/admin/inbox/${id}/read`, {
            method: "POST",
            headers: {
              Accept: "application/json",
              "X-CSRF-Token": csrfToken(),
            },
          });
          const result = await response.json();
          if (!response.ok || result.status !== "success") {
            throw new Error(result.message || "Could not mark conversation read");
          }
          this.applyConversationRead(id);
        } catch (error) {
          if (retryAttempt < 1) {
            window.setTimeout(() => this.markConversationRead(id, 1), 1500);
          }
        } finally {
          this.readStateInFlight.delete(id);
        }
      },

      applyAssignmentFilter(value) {
        if (value === "unassigned") {
          this.navigateFilter({ open_only: "true", unassigned: "true" });
        } else if (value === "unreplied") {
          this.navigateFilter({ needs_response: "true" });
        } else if (value === "attention") {
          this.navigateFilter({ needs_attention: "true" });
        } else if (value === "ai") {
          this.navigateFilter({ ai_handling: "true" });
        } else if (value) {
          this.navigateFilter({ assigned_person_id: value });
        } else {
          this.navigateFilter({});
        }
      },

      assignmentFilterActive(value) {
        const filters = new URLSearchParams(window.location.search);
        const assignee = filters.get("assigned_person_id") || "";
        if (value === "mine") return Boolean(this.actorId) && assignee === this.actorId;
        if (value === "agent") {
          return (
            (Boolean(assignee) && assignee !== this.actorId) ||
            filters.has("activity_from") ||
            filters.has("activity_to")
          );
        }
        if (value === "team") {
          return (
            Boolean(this.myTeamIds) &&
            filters.get("service_team_ids") === this.myTeamIds
          );
        }
        if (value === "ai") return filters.get("ai_handling") === "true";
        if (value === "unassigned") return filters.get("unassigned") === "true";
        if (value === "unreplied") return filters.get("needs_response") === "true";
        if (value === "attention") {
          return filters.get("needs_attention") === "true";
        }
        return ![
          "assigned_person_id",
          "service_team_ids",
          "unassigned",
          "needs_response",
          "needs_attention",
          "ai_handling",
          "activity_from",
          "activity_to",
        ].some((key) => filters.has(key));
      },

      // Scopes the queue to every team the operator belongs to — the same set
      // the "My team" badge counts, so the number and the list agree.
      applyTeamFilter() {
        if (!this.myTeamIds) {
          this.showToast("You are not a member of any service team.");
          return;
        }
        this.navigateFilter({ service_team_ids: this.myTeamIds });
      },

      applySavedView(payload) {
        const changes = {};
        Object.entries(payload || {}).forEach(([key, value]) => {
          if (value === true) changes[key] = "true";
          else if (value !== false && value !== null && value !== "") changes[key] = value;
        });
        this.navigateFilter(changes, true);
      },

      savedViewIsActive(payload) {
        const filters = new URLSearchParams(window.location.search);
        const keys = [
          "status",
          "search",
          "channel_type",
          "service_team_id",
          "service_team_ids",
          "filters",
          "assigned_person_id",
          "needs_response",
          "needs_attention",
          "contact_resolution_status",
          "priority_at_most",
          "muted",
          "snoozed",
          "open_only",
          "unassigned",
          "unread",
          "ai_handling",
          "has_ticket",
          "activity_from",
          "activity_to",
        ];
        const normalized = (value) => {
          if (value === true) return "true";
          if (value === false || value === null || value === undefined) return "";
          return String(value);
        };
        return keys.every(
          (key) => (filters.get(key) || "") === normalized((payload || {})[key]),
        );
      },

      async saveCurrentView() {
        const name = this.savedViewName.trim();
        if (!name) {
          this.showToast("Enter a name for this view.");
          return;
        }
        const source = new URLSearchParams(window.location.search);
        const data = new FormData();
        data.set("name", name);
        const mapping = {
          status: "status_value",
          search: "search",
          channel_type: "channel_type",
          service_team_id: "service_team_id",
          service_team_ids: "service_team_ids",
          filters: "filters",
          assigned_person_id: "assigned_person_id",
          needs_response: "needs_response",
          needs_attention: "needs_attention",
          contact_resolution_status: "contact_resolution_status",
          priority_at_most: "priority_at_most",
          muted: "muted",
          snoozed: "snoozed",
          open_only: "open_only",
          unassigned: "unassigned",
          unread: "unread",
          ai_handling: "ai_handling",
          has_ticket: "has_ticket",
          activity_from: "activity_from",
          activity_to: "activity_to",
        };
        Object.entries(mapping).forEach(([queryKey, formKey]) => {
          if (source.has(queryKey)) data.set(formKey, source.get(queryKey));
        });
        try {
          const response = await fetchWithTimeout("/admin/inbox/filters/save", {
            method: "POST",
            body: data,
            headers: { "X-CSRF-Token": csrfToken() },
          });
          if (!response.ok) throw new Error("Unable to save view");
          this.savedViewName = "";
          this.showToast("Saved view created.");
          this.refreshSidebar("saved_view");
        } catch (error) {
          this.showToast(error.message || "Unable to save view.");
        }
      },

      openContact(id) {
        this.ticketPanelOpen = false;
        this.contactOpen = true;
        if (id) this.beginContactRequest(id);
      },
      closeContact() {
        this.cancelContactRequest();
        this.contactOpen = false;
      },
      openNewConversation() {
        this.managerDashboardOpen = false;
        this.newConversationSubmitting = false;
        this.newConversationOpen = true;
        if (this.newConversation.channel === "whatsapp") {
          this.loadWhatsAppTemplates();
        }
        this.$nextTick(() =>
          this.$refs.newConversationDialog?.querySelector("select, input")?.focus(),
        );
      },
      closeNewConversation() {
        if (this.newConversationSubmitting) return;
        this.contactSearchController?.abort();
        this.contactSearchController = null;
        this.newConversationOpen = false;
      },
      prepareNewConversation() {
        this.newConversationSubmitting = true;
      },
      newConversationChannelChanged() {
        this.newConversation.contactId = "";
        this.newConversation.subscriberId = "";
        this.newConversation.contactResults = [];
        this.newConversation.contactError = "";
        if (this.newConversation.channel === "whatsapp") {
          this.loadWhatsAppTemplates();
        }
      },
      async searchWhatsAppContacts() {
        const term = this.newConversation.contactQuery.trim();
        this.newConversation.contactId = "";
        this.newConversation.subscriberId = "";
        this.newConversation.contactName = term;
        if (term.length < 2) {
          this.contactSearchController?.abort();
          this.contactSearchController = null;
          this.newConversation.contactResults = [];
          this.newConversation.contactLoading = false;
          return;
        }
        const sequence = ++this.contactSearchSequence;
        this.contactSearchController?.abort();
        this.contactSearchController = new AbortController();
        this.newConversation.contactLoading = true;
        this.newConversation.contactError = "";
        try {
          const response = await fetchWithTimeout(
            `/admin/inbox/whatsapp-contacts?search=${encodeURIComponent(term)}`,
            { signal: this.contactSearchController.signal },
          );
          const payload = await response.json().catch(() => ({}));
          if (
            sequence !== this.contactSearchSequence ||
            this.newConversation.contactQuery.trim() !== term
          ) {
            return;
          }
          if (!response.ok) throw new Error("Contact search failed.");
          this.newConversation.contactResults = payload.contacts || [];
        } catch (error) {
          if (error?.name === "AbortError") return;
          this.newConversation.contactResults = [];
          this.newConversation.contactError =
            error.message || "Contact search failed.";
        } finally {
          if (sequence === this.contactSearchSequence) {
            this.newConversation.contactLoading = false;
            this.contactSearchController = null;
          }
        }
      },
      selectWhatsAppContact(contact) {
        this.newConversation.contactId = contact.party_id || "";
        this.newConversation.subscriberId = contact.subscriber_id || "";
        this.newConversation.contactName = contact.name || "";
        this.newConversation.contactQuery = contact.name || "";
        this.newConversation.recipient = contact.whatsapp_address || "";
        this.newConversation.contactResults = [];
      },
      clearWhatsAppContactSelection() {
        this.newConversation.contactId = "";
        this.newConversation.subscriberId = "";
      },
      async loadWhatsAppTemplates() {
        if (this.newConversation.templateLoading) return;
        this.newConversation.templateLoading = true;
        this.newConversation.templateError = "";
        try {
          const response = await fetchWithTimeout("/admin/inbox/whatsapp-templates");
          const payload = await response.json().catch(() => ({}));
          if (!response.ok || payload.error) {
            throw new Error(payload.error || "WhatsApp templates are unavailable.");
          }
          this.newConversation.whatsappTemplates = (payload.templates || []).filter(
            (item) => String(item.status || "").toLowerCase() === "approved",
          );
        } catch (error) {
          this.newConversation.whatsappTemplates = [];
          this.newConversation.templateError =
            error.message || "WhatsApp templates are unavailable.";
        } finally {
          this.newConversation.templateLoading = false;
        }
      },
      selectNewConversationTemplate(event) {
        const key = event.target.value;
        const selected = this.newConversation.whatsappTemplates.find(
          (item) => `${item.name}::${item.language}` === key,
        );
        this.newConversation.selectedTemplate = selected || null;
        this.newConversation.templateFields = this.whatsappTemplateFields(selected);
        this.refreshWhatsAppPreview();
      },
      whatsappTemplateFields(template) {
        if (!template) return [];
        const fields = [];
        const variables = (text) =>
          [...new Set(
            Array.from(String(text || "").matchAll(/\{\{\s*(\d+)\s*\}\}/g))
              .map((match) => Number(match[1])),
          )].sort((left, right) => left - right);
        (template.components || []).forEach((component) => {
          const type = String(component.type || "").toUpperCase();
          if (type === "HEADER") {
            const format = String(component.format || "TEXT").toUpperCase();
            if (format === "TEXT") {
              variables(component.text).forEach((index) => fields.push({
                key: `header-text-${index}`,
                section: "header",
                kind: "text",
                index,
                label: `Header value ${index}`,
                value: "",
              }));
            } else if (["IMAGE", "VIDEO", "DOCUMENT"].includes(format)) {
              fields.push({
                key: "header-media",
                section: "header",
                kind: format.toLowerCase(),
                index: 1,
                label: `${format[0]}${format.slice(1).toLowerCase()} URL`,
                value: "",
              });
            }
          }
          if (type === "BODY") {
            variables(component.text).forEach((index) => fields.push({
              key: `body-text-${index}`,
              section: "body",
              kind: "text",
              index,
              label: `Body value ${index}`,
              value: "",
            }));
          }
          if (type === "BUTTONS") {
            (component.buttons || []).forEach((button, buttonIndex) => {
              if (
                String(button.type || "").toUpperCase() === "URL" &&
                String(button.url || "").includes("{{1}}")
              ) {
                fields.push({
                  key: `button-url-${buttonIndex}`,
                  section: "button",
                  kind: "text",
                  index: 1,
                  buttonIndex,
                  label: `URL button ${buttonIndex + 1} value`,
                  value: "",
                });
              }
            });
          }
        });
        return fields;
      },
      whatsappTemplateComponents() {
        const fields = this.newConversation.templateFields;
        const components = [];
        ["header", "body"].forEach((section) => {
          const sectionFields = fields
            .filter((field) => field.section === section)
            .sort((left, right) => left.index - right.index);
          if (!sectionFields.length) return;
          components.push({
            type: section,
            parameters: sectionFields.map((field) =>
              field.kind === "text"
                ? { type: "text", text: field.value }
                : {
                    type: field.kind,
                    [field.kind]: { link: field.value },
                  },
            ),
          });
        });
        fields
          .filter((field) => field.section === "button")
          .forEach((field) => components.push({
            type: "button",
            sub_type: "url",
            index: String(field.buttonIndex),
            parameters: [{ type: "text", text: field.value }],
          }));
        return components;
      },
      whatsappTemplateComponentsJson() {
        return JSON.stringify(this.whatsappTemplateComponents());
      },
      refreshWhatsAppPreview() {
        const selected = this.newConversation.selectedTemplate;
        if (!selected) return;
        const bodyComponent = (selected.components || []).find(
          (item) => String(item.type || "").toUpperCase() === "BODY",
        );
        let preview = String(bodyComponent?.text || "");
        this.newConversation.templateFields
          .filter((field) => field.section === "body")
          .forEach((field) => {
            preview = preview.replace(
              new RegExp(`\\{\\{\\s*${field.index}\\s*\\}\\}`, "g"),
              field.value || `{{${field.index}}}`,
            );
          });
        this.newConversation.body = preview;
      },
      toggleManagerDashboard() {
        this.managerDashboardOpen = !this.managerDashboardOpen;
        if (this.managerDashboardOpen) {
          this.newConversationOpen = false;
        }
      },
      openTicketPanel() {
        this.contactOpen = false;
        this.ticketPanelOpen = true;
      },
      dismissNotification(id) {
        this.realtimeNotifications = this.realtimeNotifications.filter(
          (notification) => notification.id !== id,
        );
      },
      openNotification(notification) {
        this.dismissNotification(notification.id);
        this.showToast("Preview notification opened.");
      },
      declineIncomingCall() {
        this.incomingCall = null;
        this.showDemoNotice("WhatsApp calling");
      },
      acceptIncomingCall() {
        const name = this.incomingCall?.name || "Customer";
        this.incomingCall = null;
        this.activeCall = { name, seconds: 0 };
        this.showDemoNotice("WhatsApp calling");
      },
      endActiveCall() {
        this.activeCall = null;
        this.callMuted = false;
        this.callOnHold = false;
        this.showDemoNotice("WhatsApp calling");
      },
      formatCallDuration(seconds) {
        const value = Number(seconds || 0);
        return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
      },
      closeOverlays() {
        this.newConversationOpen = false;
        this.newConversationSubmitting = false;
        this.managerDashboardOpen = false;
        this.ticketPanelOpen = false;
        this.commandPaletteOpen = false;
        this.shortcutHelpOpen = false;
      },

      stageNewConversationFiles(event) {
        this.newConversation.files = Array.from(event.target.files || []).map((file) => ({
          name: file.name,
          size: file.size,
        }));
      },


      showDemoNotice(capability) {
        this.showToast(
          `${capability} is connected to demo state until its API is available.`,
        );
      },

      showToast(message, { persistent = false } = {}) {
        this.toastMessage = message;
        window.clearTimeout(this.toastTimer);
        this.toastTimer = null;
        if (!persistent) {
          this.toastTimer = window.setTimeout(() => {
            this.toastMessage = "";
          }, 4200);
        }
      },

      trackOutboundSend(messageId) {
        const id = String(messageId || "").trim();
        if (!id) return;
        this.outboundToastMessageId = id;
        this.showToast("Message sending…", { persistent: true });
      },

      scrollThread() {
        this.$nextTick(() => {
          const thread = document.querySelector("[data-thread-scroll]");
          if (thread) thread.scrollTop = thread.scrollHeight;
        });
      },

      composerFocused() {
        return Boolean(
          document.activeElement?.closest("[data-reply-composer]"),
        );
      },

      composerHasTransientState() {
        const composer = document.querySelector("[data-reply-composer]");
        return composer?.dataset.composerDirty === "true";
      },

      refreshThread(id, force = false, options = {}) {
        const conversationId = id || this.selectedId;
        if (!conversationId) return;
        if (
          !force &&
          (this.composerFocused() || this.composerHasTransientState())
        ) {
          this.newMessagesAvailable = true;
          return;
        }
        if (!options.blocking && this.activeDetailRequest?.blocking) {
          this.newMessagesAvailable = true;
          return;
        }
        this.beginDetailRequest(
          conversationId,
          options.intent || "background",
          Boolean(options.blocking),
        );
        window.htmx.ajax("GET", `/admin/inbox/${conversationId}`, {
          target: "#triage-detail",
          swap: "innerHTML",
        });
      },

      scheduleThreadRefresh(conversationId, intent = "realtime") {
        window.clearTimeout(this.threadRefreshTimer);
        this.threadRefreshTimer = window.setTimeout(() => {
          this.threadRefreshTimer = null;
          if (String(conversationId || "") !== String(this.selectedId || "")) {
            return;
          }
          this.refreshThread(conversationId, false, { intent });
        }, 150);
      },

      refreshThreadForMessage(conversationId, messageId, force = false) {
        const id = String(messageId || "");
        if (!id || String(conversationId || "") !== String(this.selectedId)) {
          this.newMessagesAvailable = true;
          return false;
        }
        if (document.querySelector(`[data-inbox-message-id="${CSS.escape(id)}"]`)) {
          return false;
        }
        const target = document.querySelector("#inbox-message-list");
        if (!target) {
          this.newMessagesAvailable = true;
          return false;
        }
        if (id && this.recentlyRefreshedMessageIds.has(id)) return false;
        if (id) {
          this.recentlyRefreshedMessageIds.add(id);
          window.setTimeout(
            () => this.recentlyRefreshedMessageIds.delete(id),
            10000,
          );
        }
        window.htmx.ajax(
          "GET",
          `/admin/inbox/${conversationId}/messages/${id}`,
          { target, swap: "beforeend" },
        );
        this.refreshConversationRow(conversationId);
        return true;
      },

      refreshConversationRow(conversationId) {
        const id = String(conversationId || "");
        const row = document.querySelector(
          `[data-conversation-id="${CSS.escape(id)}"]`,
        );
        if (!id || !row) return false;
        const url = new URL(
          window.__inboxReturnUrl || window.location.href,
          window.location.origin,
        );
        url.pathname = `/admin/inbox/${id}/queue-row`;
        url.searchParams.delete("conversation_id");
        if (this.selectedId) url.searchParams.set("c", this.selectedId);
        window.htmx.ajax("GET", `${url.pathname}${url.search}`, {
          target: row,
          swap: "outerHTML",
        });
        return true;
      },

      refreshSidebar(intent = "manual_refresh") {
        const url = new URL(window.location.href);
        if (this.selectedId) {
          url.searchParams.set("conversation_id", this.selectedId);
        }
        this.requestInboxList(url, {
          intent,
          historyMode: "none",
        });
      },

      refreshConversationList(intent = "manual_refresh") {
        const url = new URL(
          window.__inboxReturnUrl || window.location.href,
          window.location.origin,
        );
        url.pathname = "/admin/inbox";
        url.searchParams.delete("conversation_id");
        if (this.selectedId) {
          url.searchParams.set("c", this.selectedId);
        }
        window.__inboxReturnUrl = `${url.pathname}${url.search}`;
        this.newListActivityAvailable = false;
        this.requestInboxList(url, {
          intent,
          historyMode: intent === "reply" ? "replace" : "none",
          target: "#inbox-conversation-queue",
          select: "#inbox-conversation-queue",
          swap: "outerHTML",
        });
      },

      navigatePage(urlValue) {
        const url = new URL(urlValue, window.location.origin);
        url.searchParams.delete("conversation_id");
        if (this.selectedId) url.searchParams.set("c", this.selectedId);
        window.__inboxReturnUrl = `${url.pathname}${url.search}`;
        this.requestInboxList(url, {
          intent: "pagination",
          historyMode: "push",
          target: "#inbox-conversation-queue",
          select: "#inbox-conversation-queue",
          swap: "outerHTML",
        });
      },

      connectRealtime() {
        if (this.socket && this.socket.readyState <= WebSocket.OPEN) return;
        const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
        try {
          this.socket = new WebSocket(`${scheme}//${window.location.host}/ws/inbox`);
        } catch (_error) {
          this.realtimeConnected = false;
          return;
        }
        this.socket.addEventListener("open", () => {
          this.realtimeConnected = true;
          this.reconnectAttempts = 0;
          this.subscribedTopics = new Set();
          this.subscribeVisibleTopics();
        });
        this.socket.addEventListener("message", (event) => {
          try {
            this.handleRealtimeEvent(JSON.parse(event.data));
          } catch (_error) {
            // Ignore malformed best-effort hints and rely on polling.
          }
        });
        this.socket.addEventListener("close", () => {
          this.realtimeConnected = false;
          this.subscribedTopics = new Set();
          this.clearTypingPresence();
          this.scheduleReconnect();
        });
        this.socket.addEventListener("error", () => {
          this.realtimeConnected = false;
          this.clearTypingPresence();
        });
      },

      scheduleReconnect() {
        window.clearTimeout(this.reconnectTimer);
        const delay = Math.min(30000, 1000 * 2 ** this.reconnectAttempts);
        this.reconnectAttempts += 1;
        this.reconnectTimer = window.setTimeout(() => this.connectRealtime(), delay);
      },

      subscribeVisibleTopics() {
        if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
        const ids = new Set(
          Array.from(document.querySelectorAll("[data-conversation-id]"))
            .map((row) => row.dataset.conversationId)
            .filter(Boolean),
        );
        if (this.selectedId) ids.add(this.selectedId);
        const desiredTopics = new Set(
          Array.from(ids).map((id) => `conversation:${id}`),
        );
        this.subscribedTopics.forEach((topic) => {
          if (desiredTopics.has(topic)) return;
          this.socket.send(JSON.stringify({ type: "unsubscribe", topic }));
        });
        desiredTopics.forEach((topic) => {
          if (this.subscribedTopics.has(topic)) return;
          this.socket.send(
            JSON.stringify({ type: "subscribe", topic }),
          );
        });
        this.subscribedTopics = desiredTopics;
      },

      publishTyping(conversationId, isTyping) {
        if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
        this.socket.send(
          JSON.stringify({
            type: "typing",
            topic: `conversation:${conversationId}`,
            data: { is_typing: Boolean(isTyping) },
          }),
        );
      },

      clearTypingPresence() {
        this.typingAgents = {};
        this.presenceText = "";
        window.clearTimeout(this.typingPruneTimer);
        this.typingPruneTimer = null;
      },

      updateTypingPresenceText(now = Date.now()) {
        Object.entries(this.typingAgents).forEach(([userId, agent]) => {
          if (!agent || agent.expiresAt <= now) delete this.typingAgents[userId];
        });
        const names = Object.values(this.typingAgents).map((agent) => agent.name);
        if (!names.length) {
          this.presenceText = "";
        } else if (names.length === 1) {
          this.presenceText = `${names[0]} is replying`;
        } else if (names.length === 2) {
          this.presenceText = `${names[0]} and ${names[1]} are replying`;
        } else {
          this.presenceText = `${names[0]} and ${names.length - 1} others are replying`;
        }
      },

      scheduleTypingPrune() {
        window.clearTimeout(this.typingPruneTimer);
        const expiries = Object.values(this.typingAgents)
          .map((agent) => agent.expiresAt)
          .filter(Boolean);
        if (!expiries.length) {
          this.typingPruneTimer = null;
          return;
        }
        const delay = Math.max(50, Math.min(...expiries) - Date.now());
        this.typingPruneTimer = window.setTimeout(() => {
          this.updateTypingPresenceText();
          this.scheduleTypingPrune();
        }, delay);
      },

      handleRealtimeEvent(envelope) {
        const eventType = envelope.event || envelope.type;
        const data = envelope.data || {};
        if (eventType === "heartbeat" || eventType === "connection_ack") return;
        if (eventType === "user_typing") {
          if (
            data.conversation_id === this.selectedId &&
            data.user_id !== this.actorId
          ) {
            const agentName = String(data.agent_name || "").trim();
            const userId = String(data.user_id || agentName || "unknown");
            if (data.is_typing) {
              this.typingAgents[userId] = {
                name: agentName || "Another agent",
                expiresAt: Date.now() + 3500,
              };
            } else {
              delete this.typingAgents[userId];
            }
            this.updateTypingPresenceText();
            this.scheduleTypingPrune();
          }
          return;
        }
        if (eventType === "message_status_changed") {
          this.applyDeliveryStatus(data);
        }
        if (
          [
            "message_new",
            "message_status_changed",
            "conversation_updated",
            "conversation_summary",
            "agent_notification",
            "inbox_updated",
          ].includes(eventType)
        ) {
          this.newListActivityAvailable = true;
          if (data.conversation_id === this.selectedId) {
            if (this.composerFocused()) this.newMessagesAvailable = true;
            else if (eventType === "message_new") {
              this.refreshThreadForMessage(this.selectedId, data.message_id);
            } else if (eventType !== "message_status_changed") {
              this.scheduleThreadRefresh(this.selectedId, "realtime");
            }
          } else {
            this.showToast("New activity in the inbox.");
          }
          if (eventType === "message_new" || eventType === "agent_notification") {
            this.playSound();
          }
          if (
            eventType === "message_new" &&
            data.from_customer &&
            new URL(window.location.href).searchParams.get("reply_window_status") ===
              "expired"
          ) {
            this.refreshSidebar("realtime");
          }
        }
      },

      applyDeliveryStatus(data) {
        if (String(data.conversation_id || "") !== String(this.selectedId)) {
          return;
        }
        const messageId = String(data.message_id || "");
        const status = String(data.delivery_status || "").trim().toLowerCase();
        if (!messageId || !status) return;
        if (messageId === this.outboundToastMessageId) {
          if (status === "delivered" || status === "sent") {
            this.outboundToastMessageId = "";
            this.showToast("Message sent.");
          } else if (status === "failed" || status === "cancelled") {
            this.outboundToastMessageId = "";
            this.showToast("Message delivery failed. Open the message status to retry.");
          } else {
            this.showToast("Message sending…", { persistent: true });
          }
        }
        const statusNode = Array.from(
          document.querySelectorAll("[data-inbox-delivery-status]"),
        ).find((node) => node.dataset.inboxDeliveryStatus === messageId);
        if (!statusNode) {
          this.pendingDeliveryStatuses.set(messageId, { ...data });
          return;
        }
        this.pendingDeliveryStatuses.delete(messageId);
        statusNode.textContent = status
          .replace(/_/g, " ")
          .replace(/^./, (value) => value.toUpperCase());
        statusNode.classList.toggle("text-rose-600", status === "failed");
        statusNode.classList.toggle("text-slate-400", status !== "failed");
        if (status === "failed" && messageId !== this.outboundToastMessageId) {
          this.showToast(
            "Message delivery failed. Open the message status to retry.",
          );
        }
      },

      applyPendingDeliveryStatuses() {
        Array.from(this.pendingDeliveryStatuses.values()).forEach((data) => {
          this.applyDeliveryStatus(data);
        });
      },

      cleanupInboxElement(root) {
        if (!root) return;
        const elements = [root, ...(root.querySelectorAll?.("*") || [])];
        elements.forEach((element) => {
          if (element.__inboxReplyWindowTimer) {
            window.clearInterval(element.__inboxReplyWindowTimer);
            element.__inboxReplyWindowTimer = null;
          }
          element.__inboxMentionCleanup?.();
        });
      },

      // Polling never stops entirely, it only slows down. A healthy socket used
      // to switch it off completely, and the socket cannot be trusted to
      // announce work the client has not already subscribed to — so a
      // connected agent was the one who stopped seeing new conversations
      // arrive. The staff-audience event covers the normal case; this is the
      // backstop for a dropped publish.
      startFallbackPolling() {
        window.clearInterval(this.pollTimer);
        let ticks = 0;
        this.pollTimer = window.setInterval(() => {
          if (document.visibilityState !== "visible") return;
          if (this.filterLoading) return;
          ticks += 1;
          const dueWhileConnected = ticks % 6 === 0;
          if (!this.realtimeConnected || dueWhileConnected) {
            this.refreshSidebar("poll");
          }
        }, 5000);
      },

      filteredCommands() {
        const query = this.commandQuery.trim().toLowerCase();
        return query
          ? this.commands.filter((command) =>
              command.label.toLowerCase().includes(query),
            )
          : this.commands;
      },

      runCommand(id) {
        this.commandPaletteOpen = false;
        if (id === "new") this.openNewConversation();
        if (id === "reply") this.focusReply();
        if (id === "resolve") this.resolveCurrent();
        if (id === "contact") {
          this.contactOpen ? this.closeContact() : this.openContact(this.selectedId);
        }
        if (id === "ticket") this.openTicketPanel();
        if (id === "unreplied") this.applyAssignmentFilter("unreplied");
      },

      focusReply() {
        document
          .querySelector("[data-reply-composer] textarea")
          ?.focus({ preventScroll: false });
      },

      resolveCurrent() {
        const form = Array.from(
          document.querySelectorAll(
            `[data-conversation-thread="${this.selectedId}"] form[action$="/status"]`,
          ),
        ).find((item) => item.querySelector('[name="status_value"]')?.value === "resolved");
        form?.requestSubmit();
      },

      moveConversation(direction) {
        const links = Array.from(
          document.querySelectorAll(".conversation-item a[hx-get]"),
        );
        if (!links.length) return;
        let index = links.findIndex(
          (link) =>
            link.closest("[data-conversation-id]")?.dataset.conversationId ===
            this.selectedId,
        );
        if (index < 0) index = 0;
        index = clamp(index + direction, 0, links.length - 1);
        links[index].click();
        links[index].focus();
      },

      handleShortcut(event) {
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
          event.preventDefault();
          this.commandPaletteOpen = true;
          this.$nextTick(() => this.$refs.commandSearch?.focus());
          return;
        }
        if (event.key === "Escape") {
          this.closeOverlays();
          this.closeContact();
          return;
        }
        if (editableTarget(event.target)) return;
        const key = event.key.toLowerCase();
        if (key === "?") {
          event.preventDefault();
          this.shortcutHelpOpen = true;
        } else if (key === "r") {
          event.preventDefault();
          this.focusReply();
        } else if (key === "e") {
          event.preventDefault();
          this.resolveCurrent();
        } else if (key === "j") {
          event.preventDefault();
          this.moveConversation(1);
        } else if (key === "k") {
          event.preventDefault();
          this.moveConversation(-1);
        }
      },

      clearDraftAfterSuccessfulSend() {
        const params = new URLSearchParams(window.location.search);
        const message = params.get("message") || "";
        const conversationId =
          params.get("conversation_id") || params.get("c") || this.selectedId;
        if (conversationId && /^Reply (queued|sent)/.test(message)) {
          localStorage.removeItem(`${KEYS.draftPrefix}${conversationId}`);
        }
      },
    };
  };

  window.inboxComposer = function inboxComposer(conversationId, introductionText = "") {
    return {
      conversationId,
      mode: "reply",
      draft: "",
      files: [],
      uploading: false,
      sending: false,
      replyOutcomeHandled: false,
      replyLifecycleCleanup: null,
      idempotencyKey: "",
      replyTo: null,
      scheduled: false,
      scheduledAt: "",
      typingTimer: null,
      // Provenance of the current draft. `reply()` accepts macro_id/template_id
      // and the reply owner needs them for macro execution, template audit, and
      // WhatsApp provider-template identity. `identityBody` records the exact
      // inserted text so identity is dropped if the agent rewrites it — claiming
      // a macro was sent when the body no longer matches would be a false audit
      // record, and a WhatsApp provider template must match its approved body.
      macroId: "",
      templateId: "",
      identityBody: "",
      aiDraftLoading: false,
      aiDraftResult: null,
      aiDraftError: "",
      polishLoading: false,
      polishSuggestion: null,
      polishError: "",
      polishOriginalDraft: "",
      introductionText,

      init() {
        this.draft = localStorage.getItem(`${KEYS.draftPrefix}${conversationId}`) || "";
        this.$watch("draft", (value) => {
          if (value) localStorage.setItem(`${KEYS.draftPrefix}${conversationId}`, value);
          else localStorage.removeItem(`${KEYS.draftPrefix}${conversationId}`);
          this.resizeTextarea();
        });
        this.$nextTick(() => {
          this.resizeTextarea();
          this.bindReplyLifecycle();
        });
        this.$cleanup(() => this.replyLifecycleCleanup?.());
      },

      bindReplyLifecycle() {
        this.replyLifecycleCleanup?.();
        const form = this.$root.querySelector("[data-reply-form]");
        if (!form) return;
        const events = [
          "htmx:afterRequest",
          "htmx:sendAbort",
          "htmx:timeout",
          "htmx:sendError",
          "htmx:responseError",
        ];
        const finish = (event) => this.finishSendRequest(event);
        events.forEach((name) => form.addEventListener(name, finish));
        this.replyLifecycleCleanup = () => {
          events.forEach((name) => form.removeEventListener(name, finish));
          this.replyLifecycleCleanup = null;
        };
      },

      replyOutcomeFromEvent(event) {
        const raw = event.detail?.xhr?.getResponseHeader?.("HX-Trigger");
        if (!raw) return null;
        try {
          return JSON.parse(raw)["inbox-reply-completed"] || null;
        } catch (_error) {
          return null;
        }
      },

      workspace() {
        const element = document.querySelector("[data-inbox-workspace]");
        return element && window.Alpine?.$data
          ? window.Alpine.$data(element)
          : null;
      },

      onInput() {
        this.resizeTextarea();
        this.resolveSlashCommand();
        this.workspace()?.publishTyping?.(this.conversationId, true);
        window.clearTimeout(this.typingTimer);
        this.typingTimer = window.setTimeout(
          () => this.workspace()?.publishTyping?.(this.conversationId, false),
          1200,
        );
      },

      resizeTextarea() {
        const textarea = this.$refs.textarea;
        if (!textarea) return;
        textarea.style.height = "auto";
        textarea.style.height = `${Math.min(192, Math.max(88, textarea.scrollHeight))}px`;
      },

      resolveSlashCommand() {
        const match = this.draft.match(/(?:^|\s)\/([a-z-]+)$/i);
        if (!match) return;
        const query = match[1].toLowerCase();
        const options = Array.from(
          document.querySelectorAll("[data-reply-composer] select option[data-body]"),
        );
        const option = options.find((item) =>
          item.textContent.trim().toLowerCase().includes(query),
        );
        if (option) {
          this.draft = this.draft.replace(/\/[a-z-]+$/i, option.dataset.body || "");
          // Slash expansion splices into surrounding text, so the body will not
          // match the template verbatim — do not claim template identity.
          this.releaseIdentity();
        }
      },

      insertTemplate(event) {
        const option = event.target.selectedOptions[0];
        if (option?.dataset.body) {
          this.draft = option.dataset.body;
          this.claimIdentity({ templateId: option.value });
        }
        event.target.selectedIndex = 0;
        this.$nextTick(() => this.$refs.textarea?.focus());
      },
      insertIntroduction() {
        this.insertQuickResponse(this.introductionText);
      },
      // Accepts a bare string (ad-hoc quick response) or {text, macroId,
      // templateId} dispatched by the macro menu.
      insertQuickResponse(payload) {
        const detail =
          typeof payload === "string" ? { text: payload } : payload || {};
        const text = detail.text || "";
        if (!text) return;
        // A macro replaces the draft so its body is exactly what gets sent and
        // its identity stays truthful; ad-hoc snippets append as before.
        if (detail.macroId || detail.templateId) {
          this.draft = text;
          this.claimIdentity(detail);
        } else {
          this.draft = this.draft ? `${this.draft}\n${text}` : text;
          this.releaseIdentity();
        }
        this.$nextTick(() => this.$refs.textarea?.focus());
      },
      claimIdentity({ macroId = "", templateId = "" }) {
        this.macroId = macroId || "";
        this.templateId = templateId || "";
        this.identityBody = this.draft;
      },
      releaseIdentity() {
        this.macroId = "";
        this.templateId = "";
        this.identityBody = "";
      },
      // Identity survives only while the body is untouched.
      resolvedMacroId() {
        return this.draft === this.identityBody ? this.macroId : "";
      },
      resolvedTemplateId() {
        return this.draft === this.identityBody ? this.templateId : "";
      },
      async draftWithAI() {
        if (this.aiDraftLoading) return;
        this.aiDraftLoading = true;
        this.aiDraftError = "";
        this.aiDraftResult = null;
        try {
          const response = await fetchWithTimeout(
            `/admin/inbox/${this.conversationId}/ai-draft`,
            { method: "POST", headers: { "X-CSRF-Token": csrfToken() } },
          );
          const payload = await response.json().catch(() => ({}));
          if (!payload.ok) {
            throw new Error(payload.error || "AI Draft Unavailable");
          }
          this.aiDraftResult = payload;
        } catch (error) {
          this.aiDraftError = error.message || "AI Draft Unavailable";
        } finally {
          this.aiDraftLoading = false;
        }
      },
      insertAiDraft() {
        const text = this.aiDraftResult?.draft || "";
        if (!text) return;
        this.draft = text;
        this.releaseIdentity();
        this.$nextTick(() => {
          this.$refs.textarea?.dispatchEvent(new Event("input", { bubbles: true }));
          this.$refs.textarea?.focus();
        });
      },
      async polishDraft() {
        if (this.polishLoading || !this.draft.trim()) return;
        this.polishLoading = true;
        this.polishSuggestion = null;
        this.polishError = "";
        this.polishOriginalDraft = this.draft;
        try {
          const response = await fetchWithTimeout(
            `/admin/inbox/${this.conversationId}/ai-polish`,
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrfToken(),
              },
              body: JSON.stringify({
                text: this.draft,
                context: "crm_reply",
              }),
            },
          );
          const payload = await response.json().catch(() => ({}));
          if (!payload.ok) {
            throw new Error(payload.error || "Suggestion unavailable.");
          }
          this.polishSuggestion = payload;
        } catch (error) {
          this.polishError = error.message || "Suggestion unavailable.";
          this.workspace()?.showToast?.(
            error.message || "Suggestion unavailable.",
          );
        } finally {
          this.polishLoading = false;
        }
      },
      acceptPolish(text) {
        if (!text) return;
        this.draft = text;
        this.releaseIdentity();
        this.polishSuggestion = null;
        this.polishError = "";
        this.$nextTick(() => {
          this.$refs.textarea?.dispatchEvent(new Event("input", { bubbles: true }));
          this.$refs.textarea?.focus();
        });
      },
      dismissPolish() {
        this.polishSuggestion = null;
        this.polishError = "";
      },
      restorePolishDraft() {
        if (!this.polishOriginalDraft) return;
        this.draft = this.polishOriginalDraft;
        this.releaseIdentity();
        this.polishSuggestion = null;
        this.polishError = "";
        this.$nextTick(() => {
          this.$refs.textarea?.dispatchEvent(new Event("input", { bubbles: true }));
          this.$refs.textarea?.focus();
        });
      },
      // Uploads are staged server-side immediately and bound to the reply when
      // it sends, so an abandoned composer never leaves an attachment claiming
      // to belong to a message.
      async stageFiles(event) {
        const chosen = Array.from(event.target.files || []);
        event.target.value = "";
        if (!chosen.length) return;

        const staged = chosen.map((file) => ({
          name: file.name,
          size: file.size,
          uploading: true,
          id: null,
        }));
        this.files.push(...staged);
        this.uploading = true;

        const body = new FormData();
        chosen.forEach((file) => body.append("files", file));
        try {
          const response = await fetchWithTimeout(
            `/admin/inbox/${this.conversationId}/attachments`,
            { method: "POST", body, headers: { "X-CSRF-Token": csrfToken() } },
          );
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(payload.error || "Upload failed.");
          (payload.attachment_ids || []).forEach((id, index) => {
            if (staged[index]) staged[index].id = id;
          });
          staged.forEach((file) => {
            file.uploading = false;
          });
        } catch (error) {
          // Drop the rows rather than leave them looking attached.
          this.files = this.files.filter((file) => !staged.includes(file));
          this.workspace()?.showToast?.(error.message || "Upload failed.");
        } finally {
          this.uploading = this.files.some((file) => file.uploading);
        }
      },
      removeFile(index) {
        this.files.splice(index, 1);
      },
      attachmentIds() {
        return this.files
          .filter((file) => file.id)
          .map((file) => file.id)
          .join(",");
      },
      composerDirty() {
        return Boolean(
          this.draft.trim() ||
            this.files.length ||
            this.replyTo ||
            this.scheduled ||
            this.polishSuggestion ||
            this.aiDraftResult,
        );
      },
      toggleSchedule() {
        this.scheduled = !this.scheduled;
        // Clearing the value matters: an empty send_after means "send now", so
        // toggling off must not leave a stale time on the form.
        if (!this.scheduled) this.scheduledAt = "";
      },
      clearReply() {
        this.replyTo = null;
      },
      setReply(detail) {
        const id = String(detail?.id || "").trim();
        if (!id) {
          this.workspace()?.showToast?.("Could not quote that message. Reload the conversation and try again.");
          return;
        }
        this.replyTo = {
          id,
          author: String(detail?.author || "Customer"),
          excerpt: String(detail?.excerpt || "").slice(0, 160),
        };
        this.workspace()?.showToast?.("Quoted message selected.");
        this.$nextTick(() => this.$refs.textarea?.focus());
      },
      submitFromKeyboard(event) {
        event.currentTarget.form?.requestSubmit();
      },

      completeSend(result) {
        if (
          !result ||
          String(result.conversation_id || "") !== String(this.conversationId)
        ) {
          return;
        }
        this.replyOutcomeHandled = true;
        this.sending = false;
        const workspace = this.workspace();
        if (result.status !== "success") {
          workspace?.showToast?.(result.message || "Reply failed.");
          return;
        }

        const outcomeMessage = String(result.message || "");
        if (outcomeMessage.startsWith("Reply scheduled")) {
          workspace?.showToast?.("Message scheduled.");
        } else if (
          outcomeMessage.startsWith("Reply sent") ||
          outcomeMessage.startsWith("Reply could not be delivered")
        ) {
          workspace?.showToast?.(
            outcomeMessage.startsWith("Reply sent")
              ? "Message sent."
              : "Message delivery failed. Open the message status to retry.",
          );
        } else if (result.message_id) {
          workspace?.trackOutboundSend?.(result.message_id);
        } else {
          workspace?.showToast?.(outcomeMessage || "Message submitted.");
        }

        this.draft = "";
        this.files = [];
        this.replyTo = null;
        this.scheduled = false;
        this.scheduledAt = "";
        this.macroId = "";
        this.templateId = "";
        this.identityBody = "";
        this.polishSuggestion = null;
        localStorage.removeItem(`${KEYS.draftPrefix}${this.conversationId}`);

        workspace?.refreshThreadForMessage?.(
          this.conversationId,
          result.message_id,
          true,
        );
      },

      finishSendRequest(event) {
        this.sending = false;
        if (this.replyOutcomeHandled) return;
        const outcome = this.replyOutcomeFromEvent(event);
        if (outcome) {
          this.completeSend(outcome);
          return;
        }
        const workspace = this.workspace();
        if (workspace) workspace.outboundToastMessageId = "";
        this.replyOutcomeHandled = true;
        workspace?.showToast?.(
          "Reply status could not be confirmed. Check the thread before retrying.",
        );
      },

      prepareSend(event) {
        if (this.sending) {
          event.preventDefault();
          return;
        }
        if (!this.draft.trim() && !this.files.length) {
          event.preventDefault();
          this.workspace()?.showToast?.("Write a message or add an attachment.");
          return;
        }
        if (this.uploading) {
          event.preventDefault();
          this.workspace()?.showToast?.(
            "Wait for attachments to finish uploading.",
          );
          return;
        }
        // Attachments and scheduling both submit for real now: staged uploads
        // ride along as attachment_ids, and a chosen time rides as send_after.
        if (this.scheduled && !this.scheduledAt) {
          event.preventDefault();
          this.workspace()?.showToast?.("Choose when to send, or turn off Schedule.");
          return;
        }
        this.idempotencyKey =
          window.crypto?.randomUUID?.() ||
          `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const keyInput = event.currentTarget.querySelector(
          '[name="idempotency_key"]',
        );
        if (keyInput) keyInput.value = this.idempotencyKey;
        const replyInput = event.currentTarget.querySelector(
          '[name="reply_to_message_id"]',
        );
        if (replyInput) replyInput.value = this.replyTo?.id || "";
        this.replyOutcomeHandled = false;
        this.sending = true;
        this.workspace()?.showToast?.("Message sending…", { persistent: true });
        this.workspace()?.publishTyping?.(this.conversationId, false);
      },
    };
  };
})();

(() => {
  function updateReplyWindow(el) {
    const target = el.querySelector("[data-reply-window-countdown]");
    if (!target) return;
    const expiresAt = Date.parse(el.dataset.expiresAt || "");
    const serverTime = Date.parse(el.dataset.serverTime || "");
    if (!Number.isFinite(expiresAt) || !Number.isFinite(serverTime)) return;
    const skewMs = serverTime - Date.now();
    const remainingMs = expiresAt - (Date.now() + skewMs);
    if (remainingMs <= 0) {
      target.textContent =
        "The 24-hour reply window has expired. A free-form reply cannot be sent until the customer messages again.";
      el.classList.remove("border-emerald-200", "bg-emerald-50", "text-emerald-800");
      el.classList.add("border-amber-200", "bg-amber-50", "text-amber-900");
      return;
    }
    const totalMinutes = Math.ceil(remainingMs / 60000);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    target.textContent = `Reply window closes in ${hours}h ${minutes}m`;
    if (totalMinutes <= 60) {
      el.classList.remove("border-emerald-200", "bg-emerald-50", "text-emerald-800");
      el.classList.add("border-amber-200", "bg-amber-50", "text-amber-900");
    }
  }

  function initReplyWindows(root = document) {
    root.querySelectorAll("[data-reply-window]").forEach((el) => {
      if (el.dataset.replyWindowReady) return;
      el.dataset.replyWindowReady = "true";
      updateReplyWindow(el);
      el.__inboxReplyWindowTimer = window.setInterval(
        () => updateReplyWindow(el),
        30000,
      );
    });
  }

  function initMentionTextarea(textarea) {
    if (textarea.dataset.mentionsReady) return;
    textarea.dataset.mentionsReady = "true";
    const form = textarea.closest("form");
    const hidden = form?.querySelector("[data-mention-user-ids]");
    const menu = document.createElement("div");
    menu.className =
      "absolute z-40 hidden max-h-48 w-72 overflow-auto rounded-lg border border-slate-200 bg-white p-1 text-xs shadow-xl dark:border-slate-700 dark:bg-slate-900";
    menu.setAttribute("role", "listbox");
    textarea.parentElement?.classList.add("relative");
    textarea.parentElement?.appendChild(menu);
    const selected = new Map();
    let activeIndex = -1;
    let searchSequence = 0;
    let searchController = null;

    const syncHidden = () => {
      if (hidden) hidden.value = Array.from(selected.keys()).join(",");
    };
    const close = () => {
      menu.classList.add("hidden");
      menu.innerHTML = "";
      activeIndex = -1;
      textarea.removeAttribute("aria-activedescendant");
    };
    const options = () => Array.from(menu.querySelectorAll("[data-mention-option]"));
    const setActive = (index) => {
      const items = options();
      if (!items.length) return;
      activeIndex = (index + items.length) % items.length;
      items.forEach((item, itemIndex) => {
        const active = itemIndex === activeIndex;
        item.setAttribute("aria-selected", active ? "true" : "false");
        item.classList.toggle("bg-amber-50", active);
        item.classList.toggle("dark:bg-slate-800", active);
      });
      const activeItem = items[activeIndex];
      textarea.setAttribute("aria-activedescendant", activeItem.id);
      activeItem.scrollIntoView({ block: "nearest" });
    };
    const queryAtCursor = () => {
      const pos = textarea.selectionStart || 0;
      const before = textarea.value.slice(0, pos);
      const match = before.match(/(^|\s)@([A-Za-z0-9._ -]{0,40})$/);
      return match ? { text: match[2], start: pos - match[2].length - 1, end: pos } : null;
    };
    const choose = (item, token) => {
      selected.set(String(item.id), item.name);
      textarea.value =
        textarea.value.slice(0, token.start) +
        `@${item.name} ` +
        textarea.value.slice(token.end);
      textarea.focus();
      syncHidden();
      close();
    };
    const search = async () => {
      const token = queryAtCursor();
      if (!token || token.text.length < 1) {
        searchController?.abort();
        close();
        return;
      }
      const sequence = ++searchSequence;
      searchController?.abort();
      searchController = new AbortController();
      menu.classList.remove("hidden");
      menu.innerHTML = '<div class="px-3 py-2 text-slate-500">Searching...</div>';
      try {
        const url = new URL(textarea.dataset.mentionEndpoint, window.location.origin);
        url.searchParams.set("q", token.text);
        const response = await fetchWithTimeout(url, {
          headers: { Accept: "application/json" },
          signal: searchController.signal,
        });
        const data = await response.json();
        const currentToken = queryAtCursor();
        if (
          sequence !== searchSequence ||
          !currentToken ||
          currentToken.text !== token.text
        ) {
          return;
        }
        const users = Array.isArray(data.users) ? data.users : [];
        if (!users.length) {
          menu.innerHTML = '<div class="px-3 py-2 text-slate-500">No eligible colleagues</div>';
          return;
        }
        menu.innerHTML = "";
        users.forEach((item, index) => {
          const button = document.createElement("button");
          button.type = "button";
          button.id = `mention-option-${item.id}`;
          button.dataset.mentionOption = "true";
          button.setAttribute("role", "option");
          button.setAttribute("aria-selected", "false");
          button.className =
            "block min-h-10 w-full rounded-md px-3 text-left hover:bg-amber-50 dark:hover:bg-slate-800";
          button.textContent = `${item.name} (${item.email})`;
          button.addEventListener("click", () => choose(item, token));
          button.addEventListener("mouseenter", () => setActive(index));
          menu.appendChild(button);
        });
        setActive(0);
      } catch (error) {
        if (error?.name === "AbortError") return;
        menu.innerHTML = '<div class="px-3 py-2 text-rose-600">Mentions unavailable</div>';
      }
    };

    textarea.addEventListener("input", search);
    textarea.addEventListener("keydown", (event) => {
      const items = options();
      if (event.key === "Escape") {
        close();
        return;
      }
      if (menu.classList.contains("hidden") || !items.length) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActive(activeIndex + 1);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setActive(activeIndex - 1);
        return;
      }
      if (event.key === "Enter" && activeIndex >= 0) {
        event.preventDefault();
        items[activeIndex]?.click();
      }
    });
    const closeOnOutsideClick = (event) => {
      if (!menu.contains(event.target) && event.target !== textarea) close();
    };
    document.addEventListener("click", closeOnOutsideClick);
    textarea.__inboxMentionCleanup = () => {
      searchController?.abort();
      document.removeEventListener("click", closeOnOutsideClick);
      textarea.__inboxMentionCleanup = null;
    };
  }

  function initMentions(root = document) {
    root.querySelectorAll("[data-inbox-note-mentions]").forEach(initMentionTextarea);
  }

  function whatsappTemplateFields(template) {
    if (!template) return [];
    const fields = [];
    const variables = (text) =>
      [...new Set(
        Array.from(String(text || "").matchAll(/\{\{\s*(\d+)\s*\}\}/g))
          .map((match) => Number(match[1])),
      )].sort((left, right) => left - right);
    (template.components || []).forEach((component) => {
      const type = String(component.type || "").toUpperCase();
      if (type === "HEADER") {
        const format = String(component.format || "TEXT").toUpperCase();
        if (format === "TEXT") {
          variables(component.text).forEach((index) => fields.push({
            key: `header-text-${index}`,
            section: "header",
            kind: "text",
            index,
            label: `Header value ${index}`,
          }));
        } else if (["IMAGE", "VIDEO", "DOCUMENT"].includes(format)) {
          fields.push({
            key: "header-media",
            section: "header",
            kind: format.toLowerCase(),
            index: 1,
            label: `${format[0]}${format.slice(1).toLowerCase()} URL`,
          });
        }
      }
      if (type === "BODY") {
        variables(component.text).forEach((index) => fields.push({
          key: `body-text-${index}`,
          section: "body",
          kind: "text",
          index,
          label: `Body value ${index}`,
        }));
      }
      if (type === "BUTTONS") {
        (component.buttons || []).forEach((button, buttonIndex) => {
          if (
            String(button.type || "").toUpperCase() === "URL" &&
            String(button.url || "").includes("{{1}}")
          ) {
            fields.push({
              key: `button-url-${buttonIndex}`,
              section: "button",
              kind: "text",
              index: 1,
              buttonIndex,
              label: `URL button ${buttonIndex + 1} value`,
            });
          }
        });
      }
    });
    return fields;
  }

  function whatsappTemplateComponents(fieldsRoot) {
    const fields = Array.from(fieldsRoot.querySelectorAll("[data-template-field]"))
      .map((input) => ({
        section: input.dataset.section,
        kind: input.dataset.kind,
        index: Number(input.dataset.index || "1"),
        buttonIndex: input.dataset.buttonIndex,
        value: input.value,
      }));
    const components = [];
    ["header", "body"].forEach((section) => {
      const sectionFields = fields
        .filter((field) => field.section === section)
        .sort((left, right) => left.index - right.index);
      if (!sectionFields.length) return;
      components.push({
        type: section,
        parameters: sectionFields.map((field) =>
          field.kind === "text"
            ? { type: "text", text: field.value }
            : { type: field.kind, [field.kind]: { link: field.value } },
        ),
      });
    });
    fields
      .filter((field) => field.section === "button")
      .forEach((field) => components.push({
        type: "button",
        sub_type: "url",
        index: String(field.buttonIndex),
        parameters: [{ type: "text", text: field.value }],
      }));
    return components;
  }

  function initWhatsAppTemplateReopen(form) {
    if (form.dataset.templateReopenReady) return;
    form.dataset.templateReopenReady = "true";
    const select = form.querySelector("[data-whatsapp-template-select]");
    const fieldsRoot = form.querySelector("[data-whatsapp-template-fields]");
    const status = form.querySelector("[data-whatsapp-template-status]");
    const submit = form.querySelector("[data-whatsapp-template-submit]");
    const nameInput = form.querySelector("[data-whatsapp-template-name]");
    const languageInput = form.querySelector("[data-whatsapp-template-language]");
    const componentsInput = form.querySelector("[data-whatsapp-template-components]");
    const bodyInput = form.querySelector("[data-whatsapp-template-body]");
    const idempotencyInput = form.querySelector('[name="idempotency_key"]');
    let templates = [];

    const sync = () => {
      const selected = templates.find(
        (item) => `${item.name}::${item.language}` === select.value,
      );
      nameInput.value = selected?.name || "";
      languageInput.value = selected?.language || "";
      componentsInput.value = JSON.stringify(whatsappTemplateComponents(fieldsRoot));
      bodyInput.value = selected ? `[WhatsApp template: ${selected.name}]` : "";
      submit.disabled = !selected || !form.checkValidity();
    };
    const renderFields = (template) => {
      fieldsRoot.innerHTML = "";
      whatsappTemplateFields(template).forEach((field) => {
        const label = document.createElement("label");
        label.className = "block font-semibold text-amber-950 dark:text-amber-100";
        label.textContent = field.label;
        const input = document.createElement("input");
        input.required = true;
        input.dataset.templateField = "true";
        input.dataset.section = field.section;
        input.dataset.kind = field.kind;
        input.dataset.index = String(field.index);
        if (field.buttonIndex !== undefined) {
          input.dataset.buttonIndex = String(field.buttonIndex);
        }
        input.className =
          "mt-1 h-10 w-full rounded-lg border-amber-300 bg-white text-xs text-slate-800 focus:border-amber-500 focus:ring-amber-500 dark:border-amber-800 dark:bg-slate-950 dark:text-slate-100";
        input.addEventListener("input", sync);
        label.appendChild(input);
        fieldsRoot.appendChild(label);
      });
      sync();
    };

    select.addEventListener("change", () => {
      renderFields(templates.find(
        (item) => `${item.name}::${item.language}` === select.value,
      ));
    });
    form.addEventListener("submit", () => {
      idempotencyInput.value =
        window.crypto?.randomUUID?.() ||
        `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      sync();
    });

    fetchWithTimeout(form.dataset.templateEndpoint, {
      headers: { Accept: "application/json" },
    })
      .then((response) => response.json().then((payload) => ({ response, payload })))
      .then(({ response, payload }) => {
        if (!response.ok || payload.error) {
          throw new Error(payload.error || "WhatsApp templates are unavailable.");
        }
        templates = (payload.templates || []).filter(
          (item) => String(item.status || "").toLowerCase() === "approved",
        );
        select.innerHTML = '<option value="">Choose a template</option>';
        templates.forEach((template) => {
          const option = document.createElement("option");
          option.value = `${template.name}::${template.language}`;
          option.textContent = `${template.name} · ${template.language}`;
          select.appendChild(option);
        });
        status.textContent = templates.length
          ? "Only approved WhatsApp templates are available."
          : "No approved WhatsApp templates are available.";
        submit.disabled = true;
      })
      .catch((error) => {
        templates = [];
        select.innerHTML = '<option value="">Templates unavailable</option>';
        status.textContent = error.message || "WhatsApp templates are unavailable.";
        submit.disabled = true;
      });
  }

  function initWhatsAppTemplateReopenForms(root = document) {
    root.querySelectorAll("[data-whatsapp-template-reopen]").forEach(
      initWhatsAppTemplateReopen,
    );
  }

  document.addEventListener("DOMContentLoaded", () => {
    initReplyWindows();
    initMentions();
    initWhatsAppTemplateReopenForms();
  });
  document.body?.addEventListener("htmx:afterSwap", (event) => {
    initReplyWindows(event.target);
    initMentions(event.target);
    initWhatsAppTemplateReopenForms(event.target);
  });
})();
