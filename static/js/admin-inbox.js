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

  window.inboxWorkspace = function inboxWorkspace(config) {
    return {
      selectedId: config.selectedId || "",
      myTeamIds: config.myTeamIds || "",
      actorId: config.actorId || "",
      mode: config.initialMode || "list",
      sidebarWidth: clamp(
        Number(localStorage.getItem(KEYS.sidebarWidth) || 320),
        288,
        448,
      ),
      filtersOpen: parseStoredBoolean(KEYS.filtersOpen, false),
      byAgentOpen: false,
      savedViewOpen: false,
      savedViewName: "",
      selectedIds: [],
      bulkAction: "status",
      soundEnabled: parseStoredBoolean(KEYS.soundEnabled, false),
      realtimeConnected: false,
      contactOpen: false,
      newConversationOpen: false,
      ticketPanelOpen: false,
      commandPaletteOpen: false,
      shortcutHelpOpen: false,
      commandQuery: "",
      presenceText: "",
      newMessagesAvailable: false,
      toastMessage: "",
      socket: null,
      reconnectTimer: null,
      reconnectAttempts: 0,
      pollTimer: null,
      typingTimer: null,
      inFlight: new Set(),
      newConversation: {
        channel: "email",
        inbox: "support",
        recipient: "",
        subject: "",
        cc: "",
        bcc: "",
        template: "",
        body: "",
        files: [],
        error: "",
      },
      ticketDraft: { title: "", priority: "Medium", description: "" },
      commands: [
        { id: "new", label: "New conversation", shortcut: "N" },
        { id: "reply", label: "Focus reply composer", shortcut: "R" },
        { id: "resolve", label: "Resolve current conversation", shortcut: "E" },
        { id: "contact", label: "Toggle contact details", shortcut: "" },
        { id: "unreplied", label: "Open unreplied", shortcut: "" },
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
        if (window.innerWidth < 768) return;
        event.currentTarget.setPointerCapture?.(event.pointerId);
        const startX = event.clientX;
        const startWidth = this.sidebarWidth;
        const move = (moveEvent) => {
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
        const stop = () => {
          localStorage.setItem(KEYS.sidebarWidth, String(this.sidebarWidth));
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", stop);
          window.removeEventListener("pointercancel", stop);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", stop, { once: true });
        window.addEventListener("pointercancel", stop, { once: true });
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
          const key = `${event.detail?.requestConfig?.verb || "GET"}:${path}:${target}`;
          if (this.inFlight.has(key)) {
            event.preventDefault();
            return;
          }
          this.inFlight.add(key);
          event.detail.xhr.__inboxRequestKey = key;
        });
        const release = (event) => {
          const key = event.detail?.xhr?.__inboxRequestKey;
          if (key) this.inFlight.delete(key);
        };
        document.body.addEventListener("htmx:afterRequest", release);
        document.body.addEventListener("htmx:sendError", release);
        document.body.addEventListener("htmx:responseError", release);
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
              this.subscribeVisibleTopics();
              this.updateSelectedHighlight();
              this.scrollThread();
              this.newMessagesAvailable = false;
              if (thread.dataset.conversationUnread === "true") {
                this.markConversationRead(this.selectedId);
              }
            }
          }
          if (target.id === "inbox-sidebar-content") {
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
          history.pushState({}, "", url);
          window.htmx.ajax("GET", `${url.pathname}${url.search}`, {
            target: "#inbox-sidebar-content",
            swap: "innerHTML",
          });
        });
        window.addEventListener("popstate", () => {
          const url = new URL(window.location.href);
          const selected =
            url.searchParams.get("conversation_id") || url.searchParams.get("c");
          this.selectedId = selected || "";
          this.refreshSidebar();
          if (selected) this.refreshThread(selected, true);
          else this.showList();
        });
      },

      filterRequestStarted() {
        this.newMessagesAvailable = false;
      },

      showList() {
        this.mode = "list";
        document
          .querySelector("[data-triage-shell]")
          ?.setAttribute("data-triage-mode", "list");
      },

      selectConversation(id) {
        this.selectedId = id;
        this.mode = "detail";
        this.newMessagesAvailable = false;
        document
          .querySelector("[data-triage-shell]")
          ?.setAttribute("data-triage-mode", "detail");
        this.updateSelectedHighlight();
      },

      updateSelectedHighlight() {
        document.querySelectorAll(".conversation-item").forEach((row) => {
          const selected = row.dataset.conversationId === this.selectedId;
          row.classList.toggle("border-l-amber-500", selected);
          row.classList.toggle("bg-amber-50", selected);
          row.querySelector("a")?.toggleAttribute("aria-current", selected);
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

      navigateFilter(changes) {
        const url = new URL(window.location.href);
        [
          "status",
          "assigned_person_id",
          "unassigned",
          "needs_response",
          "open_only",
          "page",
        ].forEach((key) => url.searchParams.delete(key));
        Object.entries(changes || {}).forEach(([key, value]) => {
          if (value !== null && value !== undefined && value !== "") {
            url.searchParams.set(key, value);
          }
        });
        if (this.selectedId) {
          url.searchParams.set("conversation_id", this.selectedId);
        }
        history.pushState({}, "", url);
        window.htmx.ajax("GET", `${url.pathname}${url.search}`, {
          target: "#inbox-sidebar-content",
          swap: "innerHTML",
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
        this.refreshSidebar();
      },

      applyAssignmentFilter(value) {
        if (value === "unassigned") {
          this.navigateFilter({ open_only: "true", unassigned: "true" });
        } else if (value === "unreplied") {
          this.navigateFilter({ needs_response: "true" });
        } else if (value) {
          this.navigateFilter({ assigned_person_id: value });
        } else {
          this.navigateFilter({});
        }
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
        this.navigateFilter(changes);
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
          needs_response: "needs_response",
          contact_resolution_status: "contact_resolution_status",
          priority_at_most: "priority_at_most",
          muted: "muted",
          snoozed: "snoozed",
          open_only: "open_only",
          unassigned: "unassigned",
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
          this.savedViewOpen = false;
          this.showToast("Saved view created.");
          this.refreshSidebar();
        } catch (error) {
          this.showToast(error.message || "Unable to save view.");
        }
      },

      openContact(id) {
        this.contactOpen = true;
        if (id) this.selectedId = id;
      },
      closeContact() {
        this.contactOpen = false;
      },
      openNewConversation() {
        this.newConversationOpen = true;
        this.$nextTick(() =>
          this.$refs.newConversationDialog?.querySelector("select, input")?.focus(),
        );
      },
      openTicketPanel() {
        this.ticketPanelOpen = true;
      },
      closeOverlays() {
        this.newConversationOpen = false;
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

      refreshSidebar() {
        const url = new URL(window.location.href);
        if (this.selectedId) {
          url.searchParams.set("conversation_id", this.selectedId);
        }
        window.htmx.ajax("GET", `${url.pathname}${url.search}`, {
          target: "#inbox-sidebar-content",
          swap: "innerHTML",
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
          this.scheduleReconnect();
        });
        this.socket.addEventListener("error", () => {
          this.realtimeConnected = false;
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

      handleRealtimeEvent(envelope) {
        const eventType = envelope.event || envelope.type;
        const data = envelope.data || {};
        if (eventType === "heartbeat" || eventType === "connection_ack") return;
        if (eventType === "user_typing") {
          if (data.conversation_id === this.selectedId && data.user_id !== this.actorId) {
            this.presenceText = data.is_typing ? "Another agent is replying" : "";
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
          this.refreshSidebar();
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

      startFallbackPolling() {
        window.clearInterval(this.pollTimer);
        this.pollTimer = window.setInterval(() => {
          if (!this.realtimeConnected && document.visibilityState === "visible") {
            this.refreshSidebar();
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

  window.inboxComposer = function inboxComposer(conversationId) {
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
        this.insertQuickResponse("Hello, this is the Dotmac support team. ");
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
      draftWithAI() {
        this.workspace()?.showDemoNotice?.("AI Draft");
        if (!this.draft) {
          this.draft =
            "Hello, thanks for contacting Dotmac. I’m reviewing your request and will update you shortly.";
          this.releaseIdentity();
        }
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
