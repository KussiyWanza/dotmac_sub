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
      byAgentOpen: Boolean(
        new URLSearchParams(window.location.search).get("assigned_person_id") ||
          new URLSearchParams(window.location.search).get("activity_from") ||
          new URLSearchParams(window.location.search).get("activity_to"),
      ),
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
      reconnectTimer: null,
      reconnectAttempts: 0,
      pollTimer: null,
      typingTimer: null,
      inFlight: new Set(),
      filterLoading: false,
      activeFilterXhr: null,
      pendingStatusFilter: null,
      listRequestSequence: 0,
      activeListRequest: null,
      pendingListRequest: null,
      listRequestError: "",
      lastSuccessfulListUrl: window.location.href,
      newConversation: {
        channel: "email",
        contactName: "",
        contactId: "",
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
            if (request.operator) {
              this.filterLoading = true;
              this.activeFilterXhr = event.detail.xhr;
            }
            return;
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
          if (sequence === this.activeListRequest?.sequence) {
            const wasOperator = this.activeListRequest.operator;
            this.activeListRequest = null;
            if (wasOperator) this.filterLoading = false;
          }
          if (event.detail?.xhr === this.activeFilterXhr) {
            this.activeFilterXhr = null;
            this.filterLoading = false;
            this.pendingStatusFilter = null;
          }
          if (failed && sequence === this.listRequestSequence) {
            this.listRequestError = "Could not update conversations. Try again.";
            history.replaceState({}, "", this.lastSuccessfulListUrl);
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
        });
        document.body.addEventListener("htmx:afterSwap", (event) => {
          const target = event.detail?.target;
          if (!target) return;
          if (target.id === "triage-detail") {
            this.mode = "detail";
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
            }
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
          if (selected) this.refreshThread(selected, true);
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

      showList() {
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
        this.mode = "detail";
        this.newMessagesAvailable = false;
        this.clearTypingPresence();
        document
          .querySelector("[data-triage-shell]")
          ?.setAttribute("data-triage-mode", "detail");
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
      async markConversationRead(conversationId) {
        if (!conversationId) return;
        try {
          await fetch(`/admin/inbox/${conversationId}/read`, {
            method: "POST",
            headers: { "X-CSRF-Token": csrfToken() },
            redirect: "manual",
          });
        } catch (error) {
          return;
        }
        this.refreshSidebar("read_state");
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
          const response = await fetch("/admin/inbox/filters/save", {
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
        if (id) this.selectedId = id;
      },
      closeContact() {
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
        this.newConversationOpen = false;
      },
      prepareNewConversation() {
        this.newConversationSubmitting = true;
      },
      newConversationChannelChanged() {
        this.newConversation.contactId = "";
        this.newConversation.contactResults = [];
        this.newConversation.contactError = "";
        if (this.newConversation.channel === "whatsapp") {
          this.loadWhatsAppTemplates();
        }
      },
      async searchWhatsAppContacts() {
        const term = this.newConversation.contactQuery.trim();
        this.newConversation.contactId = "";
        this.newConversation.contactName = term;
        if (term.length < 2) {
          this.newConversation.contactResults = [];
          return;
        }
        this.newConversation.contactLoading = true;
        this.newConversation.contactError = "";
        try {
          const response = await fetch(
            `/admin/inbox/whatsapp-contacts?search=${encodeURIComponent(term)}`,
          );
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error("Contact search failed.");
          this.newConversation.contactResults = payload.contacts || [];
        } catch (error) {
          this.newConversation.contactResults = [];
          this.newConversation.contactError =
            error.message || "Contact search failed.";
        } finally {
          this.newConversation.contactLoading = false;
        }
      },
      selectWhatsAppContact(contact) {
        this.newConversation.contactId = contact.id || "";
        this.newConversation.contactName = contact.name || "";
        this.newConversation.contactQuery = contact.name || "";
        this.newConversation.recipient = contact.whatsapp_address || "";
        this.newConversation.contactResults = [];
      },
      clearWhatsAppContactSelection() {
        this.newConversation.contactId = "";
      },
      async loadWhatsAppTemplates() {
        if (this.newConversation.templateLoading) return;
        this.newConversation.templateLoading = true;
        this.newConversation.templateError = "";
        try {
          const response = await fetch("/admin/inbox/whatsapp-templates");
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

      showToast(message) {
        this.toastMessage = message;
        window.clearTimeout(this.toastTimer);
        this.toastTimer = window.setTimeout(() => {
          this.toastMessage = "";
        }, 4200);
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

      refreshThread(id, force) {
        const conversationId = id || this.selectedId;
        if (!conversationId) return;
        if (!force && this.composerFocused()) {
          this.newMessagesAvailable = true;
          return;
        }
        window.htmx.ajax("GET", `/admin/inbox/${conversationId}`, {
          target: "#triage-detail",
          swap: "innerHTML",
        });
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
        const url = new URL(window.location.href);
        if (this.selectedId) {
          url.searchParams.set("conversation_id", this.selectedId);
        }
        this.newListActivityAvailable = false;
        this.requestInboxList(url, {
          intent,
          historyMode: "none",
          target: "#inbox-conversation-queue",
          select: "#inbox-conversation-queue",
          swap: "outerHTML",
        });
      },

      navigatePage(urlValue) {
        const url = new URL(urlValue, window.location.origin);
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
        ids.forEach((id) => {
          this.socket.send(
            JSON.stringify({ type: "subscribe", topic: `conversation:${id}` }),
          );
        });
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
            else this.refreshThread(this.selectedId);
          } else {
            this.showToast("New activity in the inbox.");
          }
          if (eventType === "message_new" || eventType === "agent_notification") {
            this.playSound();
          }
        }
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
      introductionText,

      init() {
        this.draft = localStorage.getItem(`${KEYS.draftPrefix}${conversationId}`) || "";
        this.$watch("draft", (value) => {
          if (value) localStorage.setItem(`${KEYS.draftPrefix}${conversationId}`, value);
          else localStorage.removeItem(`${KEYS.draftPrefix}${conversationId}`);
          this.resizeTextarea();
        });
        this.$nextTick(() => this.resizeTextarea());
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
          const response = await fetch(
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
        try {
          const response = await fetch(
            `/admin/inbox/${this.conversationId}/ai-polish`,
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrfToken(),
              },
              body: JSON.stringify({ text: this.draft, context: "crm_reply" }),
            },
          );
          const payload = await response.json().catch(() => ({}));
          if (!payload.ok) {
            throw new Error(payload.error || "Suggestion unavailable.");
          }
          this.polishSuggestion = payload;
        } catch (error) {
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
          const response = await fetch(
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
        this.replyTo = detail || null;
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
        this.sending = false;
        this.workspace()?.showToast?.(
          result.message ||
            (result.status === "success" ? "Reply sent." : "Reply failed."),
        );
        if (result.status !== "success") return;

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

        const workspace = this.workspace();
        workspace?.refreshThread?.(this.conversationId, true);
        workspace?.refreshConversationList?.("reply");
      },

      finishSendRequest(event) {
        if (event.detail?.successful) return;
        this.sending = false;
        this.workspace()?.showToast?.("Could not send reply. Try again.");
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
        this.sending = true;
        this.workspace()?.publishTyping?.(this.conversationId, false);
      },
    };
  };
})();
