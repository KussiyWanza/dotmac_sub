/* Dotmac Fiber public chat widget. Conversations and messages are owned by
 * Sub Team Inbox; the browser holds only a short-lived opaque visitor token. */
(function () {
  "use strict";

  var config = window.DotMacFiberChatConfig || {};
  var apiUrl = String(config.apiUrl || "").replace(/\/$/, "");
  if (!apiUrl || document.getElementById("dm-fiber-chat")) return;

  var storageKey = "dotmac_fiber_chat_session_v1";
  var startedAt = new Date().toISOString();
  var session = readSession();
  var socket = null;
  var seen = {};
  var unread = 0;

  function uuid() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0;
      return (c === "x" ? r : (r & 3 | 8)).toString(16);
    });
  }

  function readSession() {
    try {
      var value = JSON.parse(localStorage.getItem(storageKey) || "null");
      return value && value.apiUrl === apiUrl ? value : null;
    } catch (_error) {
      return null;
    }
  }

  function saveSession(value) {
    session = value;
    localStorage.setItem(storageKey, JSON.stringify(value));
  }

  function clearSession() {
    session = null;
    localStorage.removeItem(storageKey);
    if (socket) socket.close();
    socket = null;
  }

  function absoluteHttp(path) {
    return new URL(path, apiUrl + "/").toString();
  }

  function absoluteWs(path) {
    var url = new URL(path, apiUrl + "/");
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    return url.toString();
  }

  var style = document.createElement("style");
  style.textContent = [
    "#dm-fiber-chat{position:fixed;right:22px;bottom:22px;z-index:2147483000;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#17202a}",
    "#dm-fiber-chat *{box-sizing:border-box}",
    ".dmfc-bubble{width:58px;height:58px;border:0;border-radius:50%;background:#0b7a45;color:#fff;box-shadow:0 10px 30px rgba(0,0,0,.25);cursor:pointer;display:grid;place-items:center}",
    ".dmfc-badge{position:absolute;right:-3px;top:-3px;min-width:21px;height:21px;padding:0 5px;border-radius:11px;background:#e33;color:#fff;font-size:12px;line-height:21px;font-weight:700}",
    ".dmfc-panel{position:absolute;right:0;bottom:72px;width:min(370px,calc(100vw - 28px));height:min(590px,calc(100vh - 110px));background:#fff;border-radius:18px;box-shadow:0 18px 60px rgba(0,0,0,.28);overflow:hidden;display:flex;flex-direction:column}",
    ".dmfc-header{background:#0b7a45;color:#fff;padding:18px 20px;display:flex;align-items:center;justify-content:space-between}",
    ".dmfc-title{font-size:17px;font-weight:750}.dmfc-subtitle{font-size:12px;opacity:.85;margin-top:3px}",
    ".dmfc-close{border:0;background:transparent;color:#fff;font-size:28px;line-height:1;cursor:pointer}",
    ".dmfc-body{flex:1;min-height:0;overflow:auto;padding:18px;background:#f5f7f8}",
    ".dmfc-intro{font-size:14px;line-height:1.5;margin:0 0 14px;color:#445}",
    ".dmfc-field{display:block;margin-bottom:11px;font-size:12px;font-weight:700;color:#344}",
    ".dmfc-field input,.dmfc-field textarea{display:block;width:100%;margin-top:5px;padding:11px 12px;border:1px solid #ccd4d9;border-radius:9px;background:#fff;font:inherit;font-size:14px;color:#17202a}",
    ".dmfc-field textarea{min-height:92px;resize:vertical}",
    ".dmfc-hp{position:absolute!important;left:-10000px!important;width:1px!important;height:1px!important;overflow:hidden!important}",
    ".dmfc-start,.dmfc-send{border:0;border-radius:9px;background:#0b7a45;color:#fff;font-weight:750;cursor:pointer}",
    ".dmfc-start{width:100%;padding:12px}.dmfc-start:disabled,.dmfc-send:disabled{opacity:.55;cursor:wait}",
    ".dmfc-log{display:flex;flex-direction:column;gap:10px}",
    ".dmfc-msg{max-width:82%;padding:10px 12px;border-radius:13px;font-size:14px;line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere}",
    ".dmfc-visitor{align-self:flex-end;background:#0b7a45;color:#fff;border-bottom-right-radius:4px}",
    ".dmfc-agent{align-self:flex-start;background:#fff;border:1px solid #e1e6e9;border-bottom-left-radius:4px}",
    ".dmfc-who{display:block;font-size:11px;font-weight:750;margin-bottom:4px;opacity:.72}",
    ".dmfc-composer{display:flex;gap:8px;padding:12px;border-top:1px solid #e6eaec;background:#fff}",
    ".dmfc-input{flex:1;min-width:0;border:1px solid #ccd4d9;border-radius:9px;padding:10px 11px;font:inherit;font-size:14px}",
    ".dmfc-send{padding:0 16px}.dmfc-status{padding:0 14px 10px;background:#fff;color:#9b2c2c;font-size:12px}",
    "@media(max-width:520px){#dm-fiber-chat{right:14px;bottom:14px}.dmfc-panel{position:fixed;inset:10px;width:auto;height:auto;border-radius:14px}.dmfc-bubble{width:54px;height:54px}}"
  ].join("");
  document.head.appendChild(style);

  var root = document.createElement("div");
  root.id = "dm-fiber-chat";
  root.innerHTML =
    '<button class="dmfc-bubble" type="button" aria-label="Chat with Dotmac" aria-expanded="false">' +
      '<svg width="25" height="25" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 4h16v12H8l-4 4V4Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>' +
      '<span class="dmfc-badge" hidden>0</span>' +
    '</button>' +
    '<section class="dmfc-panel" role="dialog" aria-modal="true" aria-label="Chat with Dotmac" hidden>' +
      '<header class="dmfc-header"><div><div class="dmfc-title">Chat with Dotmac</div><div class="dmfc-subtitle">We typically reply in a few minutes</div></div><button class="dmfc-close" type="button" aria-label="Close chat">&times;</button></header>' +
      '<div class="dmfc-body"></div><div class="dmfc-status" hidden></div>' +
    '</section>';
  document.body.appendChild(root);

  var bubble = root.querySelector(".dmfc-bubble");
  var badge = root.querySelector(".dmfc-badge");
  var panel = root.querySelector(".dmfc-panel");
  var body = root.querySelector(".dmfc-body");
  var status = root.querySelector(".dmfc-status");

  function setStatus(message) {
    status.textContent = message || "";
    status.hidden = !message;
  }

  function setUnread(value) {
    unread = value;
    badge.textContent = value > 9 ? "9+" : String(value);
    badge.hidden = value < 1;
  }

  function renderPrechat() {
    body.innerHTML =
      '<p class="dmfc-intro">Tell us how we can help. Your message will go directly to our Team Inbox.</p>' +
      '<form class="dmfc-prechat">' +
        '<label class="dmfc-field">Full name<input name="full_name" autocomplete="name" maxlength="200" required></label>' +
        '<label class="dmfc-field">Email<input name="email" type="email" autocomplete="email" maxlength="254" required></label>' +
        '<label class="dmfc-field">Phone <span style="font-weight:400">(optional)</span><input name="phone" type="tel" autocomplete="tel" maxlength="40"></label>' +
        '<label class="dmfc-field">How can we help?<textarea name="message" maxlength="2000" required></textarea></label>' +
        '<label class="dmfc-hp" aria-hidden="true">Company website<input name="company_website" tabindex="-1" autocomplete="off"></label>' +
        '<button class="dmfc-start" type="submit">Start chat</button>' +
      '</form>';
    body.querySelector("form").addEventListener("submit", startChat);
  }

  function renderChat() {
    body.innerHTML = '<div class="dmfc-log" aria-live="polite"></div>';
    var composer = document.createElement("form");
    composer.className = "dmfc-composer";
    composer.innerHTML = '<input class="dmfc-input" maxlength="2000" autocomplete="off" placeholder="Type a message…" aria-label="Message"><button class="dmfc-send" type="submit">Send</button>';
    panel.insertBefore(composer, status);
    composer.addEventListener("submit", sendMessage);
    loadHistory();
    connectSocket();
  }

  function removeComposer() {
    var old = panel.querySelector(".dmfc-composer");
    if (old) old.remove();
  }

  function appendMessage(message) {
    var id = message.id || message.message_id;
    if (id && seen[id]) return;
    if (id) seen[id] = true;
    var log = body.querySelector(".dmfc-log");
    if (!log) return;
    var agent = message.direction === "outbound" || message.sender_type === "agent" || message.from_customer === false;
    var row = document.createElement("div");
    row.className = "dmfc-msg " + (agent ? "dmfc-agent" : "dmfc-visitor");
    var who = document.createElement("span");
    who.className = "dmfc-who";
    who.textContent = agent ? (message.author_name || "Dotmac Support") : "You";
    var text = document.createElement("span");
    text.textContent = message.body || "";
    row.appendChild(who);
    row.appendChild(text);
    log.appendChild(row);
    body.scrollTop = body.scrollHeight;
  }

  function sessionFetch(path, options) {
    options = options || {};
    options.headers = options.headers || {};
    options.headers["X-Visitor-Token"] = session.visitor_token;
    return fetch(absoluteHttp(session.api_base + path), options).then(function (response) {
      if (response.status === 401) {
        clearSession();
        removeComposer();
        renderPrechat();
        throw new Error("Your chat session expired. Please start a new chat.");
      }
      return response;
    });
  }

  function loadHistory() {
    if (!session) return;
    sessionFetch("/session/" + session.session_id + "/messages?limit=50")
      .then(function (response) {
        if (!response.ok) throw new Error("Could not load chat history.");
        return response.json();
      })
      .then(function (data) {
        (data.messages || []).forEach(appendMessage);
      })
      .catch(function (error) { setStatus(error.message); });
  }

  function startChat(event) {
    event.preventDefault();
    setStatus("");
    var form = event.currentTarget;
    var button = form.querySelector("button");
    var values = new FormData(form);
    var clientId = uuid();
    button.disabled = true;
    fetch(absoluteHttp("/widget/fiber/session"), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        form_version: "fiber-chat-v1",
        client_session_id: clientId,
        full_name: values.get("full_name"),
        email: values.get("email"),
        phone: values.get("phone") || null,
        message: values.get("message"),
        page_url: window.location.href,
        referrer_url: document.referrer || null,
        started_at: startedAt,
        company_website: values.get("company_website") || ""
      })
    }).then(function (response) {
      if (!response.ok) return response.json().catch(function () { return {}; }).then(function (data) {
        throw new Error(data.detail || "Could not start chat.");
      });
      return response.json();
    }).then(function (value) {
      value.apiUrl = apiUrl;
      saveSession(value);
      removeComposer();
      renderChat();
      setStatus("");
    }).catch(function (error) {
      setStatus(error.message);
      button.disabled = false;
    });
  }

  function sendMessage(event) {
    event.preventDefault();
    var input = event.currentTarget.querySelector("input");
    var button = event.currentTarget.querySelector("button");
    var message = input.value.trim();
    if (!message || !session) return;
    input.value = "";
    button.disabled = true;
    sessionFetch("/session/" + session.session_id + "/message", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({body: message, client_message_id: uuid()})
    }).then(function (response) {
      if (!response.ok) throw new Error("Message failed to send.");
      return response.json();
    }).then(appendMessage).catch(function (error) {
      setStatus(error.message);
      input.value = message;
    }).finally(function () { button.disabled = false; });
  }

  function connectSocket() {
    if (!session || socket) return;
    socket = new WebSocket(absoluteWs(session.ws_url) + "?token=" + encodeURIComponent(session.visitor_token));
    socket.onopen = function () {
      socket.send(JSON.stringify({type: "subscribe", topic: "conversation:" + session.conversation_id}));
    };
    socket.onmessage = function (event) {
      var envelope;
      try { envelope = JSON.parse(event.data); } catch (_error) { return; }
      if ((envelope.event || envelope.type) !== "message_new") return;
      var message = envelope.data || envelope.payload || envelope;
      if (message.direction !== "outbound") return;
      appendMessage(message);
      if (panel.hidden) setUnread(unread + 1);
    };
    socket.onclose = function () {
      socket = null;
      if (session) window.setTimeout(connectSocket, 3000);
    };
  }

  function openPanel() {
    panel.hidden = false;
    bubble.setAttribute("aria-expanded", "true");
    setUnread(0);
  }

  function closePanel() {
    panel.hidden = true;
    bubble.setAttribute("aria-expanded", "false");
  }

  bubble.addEventListener("click", function () { panel.hidden ? openPanel() : closePanel(); });
  root.querySelector(".dmfc-close").addEventListener("click", closePanel);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !panel.hidden) closePanel();
  });

  if (session) renderChat(); else renderPrechat();
})();
