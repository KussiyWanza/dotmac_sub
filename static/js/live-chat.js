/* Live chat client. Brokers a native team-inbox session through the sub, then
 * talks to the sub widget API: WebSocket for real-time, REST for send/history.
 *
 * The browser never supplies identity — it only ever holds the opaque
 * visitor_token returned by the broker. */
(function () {
  "use strict";

  var root = document.getElementById("dm-chat");
  if (!root) return;

  var els = {
    root: root,
    bubble: document.getElementById("dm-chat-bubble"),
    unread: document.getElementById("dm-chat-unread"),
    panel: document.getElementById("dm-chat-panel"),
    close: document.getElementById("dm-chat-close"),
    log: document.getElementById("dm-chat-log"),
    typing: document.getElementById("dm-chat-typing"),
    form: document.getElementById("dm-chat-form"),
    input: document.getElementById("dm-chat-input"),
    send: document.getElementById("dm-chat-send"),
    status: document.getElementById("dm-chat-status"),
    empty: document.getElementById("dm-chat-empty"),
    historyError: document.getElementById("dm-chat-history-error"),
    historyRetry: document.getElementById("dm-chat-history-retry"),
  };

  var sessionEndpoint = root.getAttribute("data-session-endpoint") ||
    "/api/v1/me/chat/session";

  var state = {
    session: null, // {visitor_token, conversation_id, ws_url, api_base, session_id}
    ws: null,
    started: false,
    open: false,
    unread: 0,
    seen: {}, // message id -> true (dedupe)
    pending: {},
    sending: false,
    conversationId: null,
    typingTimer: null,
    reconnect: 0,
    reconnectTimer: null,
    gen: 0, // session generation; bumped on re-scope to invalidate stale sockets
  };

  // ── rendering ──────────────────────────────────────────────────────────
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function updateEmptyState() {
    if (!els.empty) return;
    els.empty.hidden = els.log.children.length > 0;
  }

  function updateConversationId(id) {
    if (!id) return;
    state.conversationId = id;
    if (state.session) state.session.conversation_id = id;
  }

  function setSending(on) {
    state.sending = on;
    els.send.disabled = on || !state.session;
  }

  function isAgentMessage(payload) {
    return payload.direction === "outbound" ||
      payload.sender_type === "agent" ||
      payload.from_customer === false;
  }

  function appendMessage(msg) {
    // msg: {id, body, direction, author_name, created_at}
    var id = msg.id || msg.message_id;
    if (id && state.seen[id]) return;
    if (id) state.seen[id] = true;
    var pending = !!msg.pending;
    var failed = !!msg.failed;
    var dir = isAgentMessage(msg) ? "in" : "out"; // outbound (agent)=incoming to us
    var row = document.createElement("div");
    row.className = "dm-chat-msg dm-chat-msg-" + dir;
    if (pending) row.className += " dm-chat-msg-pending";
    if (failed) row.className += " dm-chat-msg-failed";
    if (id) row.dataset.messageId = id;
    if (msg.client_message_id) row.dataset.clientMessageId = msg.client_message_id;
    if (msg.created_at) row.dataset.createdAt = msg.created_at;
    var who = dir === "in" ? (msg.author_name || "Support") : "You";
    row.innerHTML =
      '<span class="dm-chat-msg-who">' + esc(who) + "</span>" +
      '<span class="dm-chat-msg-body">' + esc(msg.body) + "</span>" +
      '<span class="dm-chat-msg-meta">' + (pending ? "Sending…" : "") + "</span>";
    els.log.appendChild(row);
    els.log.scrollTop = els.log.scrollHeight;
    updateEmptyState();
  }

  function reconcilePendingMessage(clientId, serverId, createdAt) {
    if (!clientId || !state.pending[clientId]) return false;
    var row = state.pending[clientId];
    row.classList.remove("dm-chat-msg-pending", "dm-chat-msg-failed");
    if (serverId) {
      row.dataset.messageId = serverId;
      state.seen[serverId] = true;
    }
    if (createdAt) row.dataset.createdAt = createdAt;
    var meta = row.querySelector(".dm-chat-msg-meta");
    if (meta) meta.textContent = "";
    delete state.pending[clientId];
    return true;
  }

  function reconcileOutboundEcho(id, body, createdAt) {
    var created = createdAt ? Date.parse(createdAt) : 0;
    var rows = Object.keys(state.pending).map(function (key) {
      return state.pending[key];
    });
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var rowCreated = row.dataset.createdAt ? Date.parse(row.dataset.createdAt) : 0;
      if (created && rowCreated && Math.abs(created - rowCreated) > 30000) continue;
      var text = row.querySelector(".dm-chat-msg-body");
      if (text && text.textContent === body) {
        return reconcilePendingMessage(row.dataset.clientMessageId, id, createdAt);
      }
    }
    return false;
  }

  function setStatus(text) {
    if (!text) {
      els.status.hidden = true;
      els.status.textContent = "";
    } else {
      els.status.hidden = false;
      els.status.textContent = text;
    }
  }

  function setUnread(n) {
    state.unread = n;
    if (n > 0) {
      els.unread.hidden = false;
      els.unread.textContent = n > 9 ? "9+" : String(n);
    } else {
      els.unread.hidden = true;
    }
  }

  function enableComposer(on) {
    els.input.disabled = !on;
    els.send.disabled = !on || state.sending;
  }

  function cookieValue(name) {
    var escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    var match = document.cookie.match(new RegExp("(?:^|;\\s*)" + escaped + "=([^;]+)"));
    return match ? decodeURIComponent(match[1]) : "";
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) return meta.content;
    return cookieValue("csrf_token");
  }

  // ── Widget REST (direct, X-Visitor-Token) ──────────────────────────────
  function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers["X-Visitor-Token"] = state.session.visitor_token;
    return fetch(state.session.api_base + path, opts);
  }

  function loadHistory(showErrors) {
    if (els.historyError) els.historyError.hidden = true;
    return api(
      "/session/" + state.session.session_id + "/messages?limit=50"
    )
      .then(function (r) {
        if (!r.ok) throw new Error("history failed");
        return r.json();
      })
      .then(function (data) {
        (data.messages || []).forEach(appendMessage);
        updateEmptyState();
      })
      .catch(function () {
        if (showErrors && els.historyError) els.historyError.hidden = false;
      });
  }

  function sendMessage(body) {
    var clientId = "client-" + Date.now() + "-" + Math.random().toString(16).slice(2);
    var createdAt = new Date().toISOString();
    appendMessage({
      id: clientId,
      client_message_id: clientId,
      body: body,
      direction: "inbound",
      created_at: createdAt,
      pending: true,
    });
    state.pending[clientId] = els.log.querySelector('[data-client-message-id="' + clientId + '"]');
    return api("/session/" + state.session.session_id + "/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body: body, client_message_id: clientId }),
    })
      .then(function (r) {
        if (!r.ok) throw new Error("send failed");
        return r.json();
      })
      .then(function (m) {
        updateConversationId(m.conversation_id);
        if (reconcilePendingMessage(clientId, m.message_id || m.id, m.created_at)) return;
        appendMessage({
          id: m.message_id || m.id,
          body: m.body,
          direction: "inbound",
          created_at: m.created_at,
        });
      })
      .catch(function (e) {
        var row = state.pending[clientId];
        if (row) {
          row.classList.remove("dm-chat-msg-pending");
          row.classList.add("dm-chat-msg-failed");
          var meta = row.querySelector(".dm-chat-msg-meta");
          if (meta) meta.textContent = "Failed";
        }
        throw e;
      });
  }

  function markRead() {
    if (!state.session) return;
    api("/session/" + state.session.session_id + "/read", { method: "POST" })
      .catch(function () {});
    setUnread(0);
  }

  // ── WebSocket ──────────────────────────────────────────────────────────
  function connectWs() {
    if (!state.session || !state.session.ws_url) return;
    // Bind this socket to the current session generation; a re-scope bumps
    // state.gen so a stale socket's callbacks (esp. onclose reconnect) no-op.
    var myGen = state.gen;
    var ws;
    try {
      ws = new WebSocket(
        state.session.ws_url,
        ["dotmac-auth", state.session.visitor_token]
      );
    } catch (e) {
      return;
    }
    state.ws = ws;

    ws.onopen = function () {
      state.reconnect = 0;
      setStatus("");
      subscribeConversation();
    };

    ws.onmessage = function (ev) {
      var data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      var type = data.event || data.type;
      var payload = data.data || data.payload || data;
      if (type === "message_new") {
        updateConversationId(payload.conversation_id);
        var id = payload.id || payload.message_id;
        var body = payload.body;
        var createdAt = payload.created_at;
        if (!isAgentMessage(payload) && reconcileOutboundEcho(id, body, createdAt)) {
          return;
        }
        appendMessage({
          id: id,
          body: body,
          direction: payload.direction || "outbound",
          author_name: payload.author_name,
          created_at: createdAt,
        });
        if (!state.open) setUnread(state.unread + 1);
        else markRead();
      } else if (type === "user_typing") {
        showTyping();
      }
    };

    ws.onclose = function () {
      // Ignore a stale socket closing after the widget was re-scoped.
      if (myGen !== state.gen) return;
      state.ws = null;
      // Backoff reconnect while the panel is alive.
      if (state.started) {
        var delay = Math.min(1000 * Math.pow(2, state.reconnect++), 15000);
        setStatus("Reconnecting…");
        state.reconnectTimer = setTimeout(connectWs, delay);
      }
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  function showTyping() {
    els.typing.hidden = false;
    if (state.typingTimer) clearTimeout(state.typingTimer);
    state.typingTimer = setTimeout(function () {
      els.typing.hidden = true;
    }, 3000);
  }

  function sendTyping() {
    if (state.ws && state.ws.readyState === 1) {
      try {
        state.ws.send(JSON.stringify({
          type: "typing",
          topic: `conversation:${state.conversationId}`,
          data: { is_typing: true },
        }));
      } catch (e) {}
    }
  }

  function subscribeConversation() {
    if (!state.ws || state.ws.readyState !== 1 || !state.conversationId) return;
    state.ws.send(JSON.stringify({
      type: "subscribe",
      topic: `conversation:${state.conversationId}`,
    }));
  }

  // ── lifecycle ──────────────────────────────────────────────────────────
  function start() {
    if (state.started) return Promise.resolve();
    state.started = true;
    enableComposer(false);
    setStatus("Connecting…");
    return fetch(sessionEndpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: (function () {
        var headers = { "Content-Type": "application/json" };
        var token = csrfToken();
        if (token) headers["X-CSRF-Token"] = token;
        return headers;
      })(),
      body: "{}",
    })
      .then(function (r) {
        if (r.status === 503) throw new Error("disabled");
        if (!r.ok) throw new Error("session failed");
        return r.json();
      })
      .then(function (s) {
        state.session = s;
        updateConversationId(s.conversation_id);
        setStatus("");
        enableComposer(true);
        els.input.focus();
        connectWs();
        return loadHistory(false).then(subscribeConversation);
      })
      .catch(function (e) {
        state.started = false;
        setStatus(
          e && e.message === "disabled"
            ? "Chat is currently unavailable."
            : "Couldn't start chat. Please try again."
        );
      });
  }

  function openPanel() {
    els.panel.hidden = false;
    els.bubble.setAttribute("aria-expanded", "true");
    root.setAttribute("data-live-chat-ready", "true");
    state.open = true;
    start().then(markRead);
  }

  function closePanel() {
    els.panel.hidden = true;
    els.bubble.setAttribute("aria-expanded", "false");
    state.open = false;
  }

  // ── wiring ─────────────────────────────────────────────────────────────
  els.bubble.addEventListener("click", function () {
    state.open ? closePanel() : openPanel();
  });
  els.close.addEventListener("click", closePanel);
  if (els.historyRetry) {
    els.historyRetry.addEventListener("click", function () {
      if (state.session) loadHistory(true);
    });
  }

  els.form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var body = (els.input.value || "").trim();
    if (!body || !state.session) return;
    if (state.sending) return;
    els.input.value = "";
    setSending(true);
    sendMessage(body).catch(function () {
      setStatus("Message failed to send.");
      els.input.value = body;
    }).finally(function () {
      setSending(false);
    });
  });

  var typingThrottle = 0;
  els.input.addEventListener("input", function () {
    var now = Date.now();
    if (now - typingThrottle > 1500) {
      typingThrottle = now;
      sendTyping();
    }
  });

  function trapFocus(ev) {
    if (!state.open) return;
    if (ev.key === "Escape") {
      closePanel();
      els.bubble.focus();
    }
  }
  document.addEventListener("keydown", trapFocus);

  // ── external API: open the widget scoped to a ticket/project ────────────
  // A "Chat about this" button calls dmLiveChat.openWith(endpoint) with a
  // scoped broker path (e.g. /api/v1/me/chat/session?ticket_id=…). Re-scoping
  // tears down any current session so the new (contextual) one starts fresh.
  function openWith(endpoint) {
    if (endpoint && endpoint !== sessionEndpoint) {
      sessionEndpoint = endpoint;
      // Invalidate the old session generation so any in-flight socket / pending
      // reconnect for the previous context no-ops.
      state.gen++;
      if (state.reconnectTimer) {
        clearTimeout(state.reconnectTimer);
        state.reconnectTimer = null;
      }
      try {
        if (state.ws) state.ws.close();
      } catch (e) {
        /* ignore */
      }
      state.ws = null;
      state.started = false;
      state.session = null;
      state.conversationId = null;
      // Reset per-conversation state so the new context starts clean (else the
      // dedupe map / pending echoes / backoff leak across contexts).
      state.seen = {};
      state.pending = {};
      state.reconnect = 0;
      state.unread = 0;
      if (state.typingTimer) {
        clearTimeout(state.typingTimer);
        state.typingTimer = null;
      }
      if (els.log) els.log.innerHTML = "";
    }
    if (state.open) {
      start().then(markRead);
    } else {
      openPanel();
    }
  }
  window.dmLiveChat = { openWith: openWith };

  root.hidden = false;
  updateEmptyState();
})();
