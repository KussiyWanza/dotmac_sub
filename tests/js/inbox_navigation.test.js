"use strict";

// Controller regressions: the real inbox script runs in a VM. DOM, network and
// HTMX event delivery are explicit doubles; this is NOT live browser acceptance.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const script = fs.readFileSync(
  process.env.INBOX_SCRIPT || path.join(__dirname, "../../static/js/admin-inbox.js"),
  "utf8",
);

function fixture(start = "/admin/inbox?view=all", selectedId = "") {
  const listeners = new Map();
  const requests = [];
  const pushes = [];
  const replacements = [];
  const timers = new Map();
  let clock = 0;
  const location = new URL(start, "https://inbox.example.test");
  const node = (id) => ({
    id, dataset: {}, querySelector: () => null, querySelectorAll: () => [],
    setAttribute() {}, removeAttribute() {},
  });
  const nodes = new Map([
    ["#inbox-sidebar-content", node("inbox-sidebar-content")],
    ["#inbox-conversation-queue", node("inbox-conversation-queue")],
    ["#triage-detail", node("triage-detail")],
  ]);
  function on(scope, type, listener) {
    const key = `${scope}:${type}`;
    if (!listeners.has(key)) listeners.set(key, []);
    listeners.get(key).push(listener);
  }
  const document = {
    body: { addEventListener: (type, listener) => on("body", type, listener) },
    addEventListener: (type, listener) => on("document", type, listener),
    querySelector: (selector) => nodes.get(selector) || null,
    querySelectorAll: () => [], contains: () => true,
  };
  function emit(type, detail = {}, scope = "body", extra = {}) {
    const event = {
      type, detail, target: detail.target || node("root"),
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, ...extra,
    };
    for (const listener of listeners.get(`${scope}:${type}`) || []) listener(event);
    return event;
  }
  const history = {
    pushState(_state, _title, value) { pushes.push(String(value)); location.href = String(value); },
    replaceState(_state, _title, value) { replacements.push(String(value)); location.href = String(value); },
  };
  const htmx = {
    config: { timeout: 0 },
    ajax(method, url, options) {
      const target = typeof options.target === "string" ? nodes.get(options.target) : options.target;
      const xhr = {
        status: 0, responseText: "", aborted: false,
        abort() {
          this.aborted = true; this.status = 0;
          emit("htmx:afterRequest", record.detail);
          emit("htmx:sendAbort", record.detail);
        },
      };
      const record = {
        method, url, options, xhr,
        detail: { xhr, target, requestConfig: { path: url, verb: method }, pathInfo: { finalRequestPath: url } },
      };
      requests.push(record);
      emit("htmx:beforeRequest", record.detail);
      return Promise.resolve();
    },
  };
  const window = {
    location, htmx, history, document,
    addEventListener: (type, listener) => on("window", type, listener),
    setTimeout: (callback) => { const id = ++clock; timers.set(id, callback); return id; },
    clearTimeout: (id) => timers.delete(id),
  };
  const parser = class {
    parseFromString(html) {
      return {
        querySelector(selector) {
          const expected = selector === "#inbox-conversation-queue"
            ? 'id="inbox-conversation-queue"' : "data-inbox-sidebar-content";
          return html.includes(expected) ? node("parsed") : null;
        },
      };
    }
  };
  vm.runInNewContext(script, {
    window, document, history, URL, URLSearchParams, DOMParser: parser,
    localStorage: { getItem: () => null, setItem() {} }, console,
    AbortController, CSS: { escape: String },
  });
  const workspace = window.inboxWorkspace({ actorId: "agent-1", myTeamIds: "team-1", selectedId });
  workspace.bindHtmx();
  const rendered = { page: 1, assignee: "", draft: "Unsent reply", thread: selectedId };
  function response(record, status = 200, html = null) {
    record.xhr.status = status;
    record.xhr.responseText = html ?? '<div data-inbox-sidebar-content><div id="inbox-conversation-queue">rows</div></div>';
    const detail = { ...record.detail, shouldSwap: status >= 200 && status < 300 && status !== 204 };
    emit("htmx:beforeSwap", detail);
    if (detail.shouldSwap) {
      const query = new URL(record.url, location).searchParams;
      rendered.page = Number(query.get("page") || 1);
      rendered.assignee = query.get("assigned_person_id") || "";
      emit("htmx:afterSwap", detail);
    }
    emit("htmx:afterRequest", { ...record.detail, successful: status >= 200 && status < 300 });
    return detail;
  }
  function failure(record, kind = "timeout") {
    record.xhr.status = 0;
    // Exact order of the checked-in HTMX ontimeout/onabort/onerror callbacks.
    emit("htmx:afterRequest", record.detail);
    emit(`htmx:${kind}`, record.detail);
  }
  function clickLink(url, extra = {}) {
    const link = { href: new URL(url, location).href, closest: () => null, hasAttribute: () => false };
    return emit("click", {}, "document", { button: 0, target: { closest: () => link }, ...extra });
  }
  return { workspace, window, nodes, location, requests, pushes, replacements, rendered, response, failure, emit, clickLink };
}

test("Next is handled once, keeps old URL while loading, and commits after queue swap", () => {
  const f = fixture();
  f.workspace.navigatePage("/admin/inbox?view=all&page=2");
  f.clickLink("/admin/inbox?view=all&page=2", { defaultPrevented: true });
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].options.target, "#inbox-conversation-queue");
  assert.equal(f.requests[0].xhr.aborted, false);
  assert.equal(f.pushes.length, 0);
  assert.equal(f.rendered.page, 1);
  f.response(f.requests[0]);
  assert.equal(f.rendered.page, 2);
  assert.equal(f.location.searchParams.get("page"), "2");
  assert.equal(f.pushes.length, 1);
  assert.equal(f.workspace.filterLoading, false);
});

test("Assigned to me is active only after its matching rows have swapped", () => {
  const f = fixture("/admin/inbox?view=all&page=3&channel_type=whatsapp");
  f.workspace.applyAssignmentFilter("agent-1");
  assert.equal(f.pushes.length, 0);
  assert.equal(f.workspace.assignmentFilterActive("mine"), false);
  assert.equal(f.workspace.filterLoading, true);
  const query = new URL(f.requests[0].url, f.location).searchParams;
  assert.equal(query.get("page"), null);
  assert.equal(query.get("channel_type"), "whatsapp");
  f.response(f.requests[0]);
  assert.equal(f.rendered.assignee, "agent-1");
  assert.equal(f.workspace.assignmentFilterActive("mine"), true);
  assert.ok(f.workspace.activeFilterChips().some(chip => chip.label === "Assigned to me"));
});

for (const kind of ["timeout", "sendAbort", "sendError"]) {
  test(`${kind}: old rows/URL survive and refresh status never reports success`, () => {
    const f = fixture();
    f.workspace.applyAssignmentFilter("agent-1");
    f.failure(f.requests[0], kind);
    assert.equal(f.pushes.length, 0);
    assert.equal(f.rendered.assignee, "");
    assert.equal(f.location.searchParams.get("assigned_person_id"), null);
    assert.equal(f.workspace.filterLoading, false);
    assert.equal(f.workspace.inboxRefreshState, "error");
    assert.match(f.workspace.listRequestError, /Could not update/);
    assert.equal(f.workspace.activeListRequest, null);
  });
}

test("Retry repeats the failed assignment intent, not the last successful all view", () => {
  const f = fixture(); f.workspace.applyAssignmentFilter("agent-1"); f.failure(f.requests[0]);
  f.workspace.retryListRequest();
  assert.equal(f.requests[1].url, f.requests[0].url);
  f.response(f.requests[1]);
  assert.equal(f.rendered.assignee, "agent-1");
  assert.equal(f.workspace.listRequestError, "");
  assert.equal(f.workspace.failedListNavigation, null);
});

test("rapid assignment and channel changes compose, and superseded responses cannot swap", () => {
  const f = fixture();
  f.workspace.applyAssignmentFilter("agent-1");
  f.workspace.navigateFilter({ channel_type: "whatsapp" });
  assert.equal(f.requests[0].xhr.aborted, true);
  const query = new URL(f.requests[1].url, f.location).searchParams;
  assert.equal(query.get("assigned_person_id"), "agent-1");
  assert.equal(query.get("channel_type"), "whatsapp");
  assert.equal(f.response(f.requests[0]).shouldSwap, false);
  assert.equal(f.workspace.filterLoading, true);
  f.response(f.requests[1]);
  assert.equal(f.pushes.length, 1);
  assert.equal(f.rendered.assignee, "agent-1");
});

test("rapid page navigation accepts only the newest page", () => {
  const f = fixture(); f.workspace.navigatePage("/admin/inbox?page=2"); f.workspace.navigatePage("/admin/inbox?page=3");
  f.response(f.requests[1]);
  assert.equal(f.response(f.requests[0]).shouldSwap, false);
  assert.equal(f.rendered.page, 3);
  assert.equal(f.pushes.length, 1);
});

test("Previous keeps the applied assignment and selected thread; draft is untouched", () => {
  const f = fixture("/admin/inbox?assigned_person_id=agent-1&page=3&c=thread-1", "thread-1");
  f.workspace.navigatePage("/admin/inbox?assigned_person_id=agent-1&page=2&conversation_id=thread-1");
  f.response(f.requests[0]);
  assert.equal(f.rendered.page, 2);
  assert.equal(f.location.searchParams.get("assigned_person_id"), "agent-1");
  assert.equal(f.location.searchParams.get("c"), "thread-1");
  assert.equal(f.rendered.draft, "Unsent reply");
  assert.equal(f.workspace.selectedId, "thread-1");
});

test("Back/Forward renders the requested history entry without creating extra entries", () => {
  const f = fixture(); f.workspace.navigatePage("/admin/inbox?page=2"); f.response(f.requests[0]);
  f.location.href = "https://inbox.example.test/admin/inbox?page=1";
  f.emit("popstate", {}, "window");
  assert.equal(f.rendered.page, 2);
  f.response(f.requests[1]);
  assert.equal(f.rendered.page, 1); assert.equal(f.pushes.length, 1);
  f.location.href = "https://inbox.example.test/admin/inbox?page=2";
  f.emit("popstate", {}, "window"); f.response(f.requests[2]);
  assert.equal(f.rendered.page, 2); assert.equal(f.pushes.length, 1);
});

test("failed history navigation restores the rendered URL and preserves thread selection", () => {
  const f = fixture("/admin/inbox?page=2&c=thread-1", "thread-1");
  f.location.href = "https://inbox.example.test/admin/inbox?page=1&c=thread-2";
  f.emit("popstate", {}, "window"); f.failure(f.requests[0]);
  assert.equal(f.location.searchParams.get("page"), "2");
  assert.equal(f.location.searchParams.get("c"), "thread-1");
  assert.equal(f.workspace.selectedId, "thread-1");
});

test("background polling yields to an operator request", () => {
  const f = fixture(); f.workspace.applyAssignmentFilter("agent-1"); f.workspace.refreshSidebar("poll");
  assert.equal(f.requests.length, 1); assert.equal(f.requests[0].xhr.aborted, false);
});

test("list AJAX uses a stable sidebar source separate from default thread transport", () => {
  const f = fixture(); f.workspace.navigatePage("/admin/inbox?page=2");
  assert.equal(f.requests[0].options.source, f.nodes.get("#inbox-sidebar-content"));
});

for (const extra of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) {
  test(`delegated links preserve modified/native clicks ${JSON.stringify(extra)}`, () => {
    const f = fixture(); const event = f.clickLink("/admin/inbox?view=queue", extra);
    assert.equal(event.defaultPrevented, false); assert.equal(f.requests.length, 0);
  });
}

test("ordinary delegated view links still work", () => {
  const f = fixture(); f.clickLink("/admin/inbox?view=queue"); assert.equal(f.requests.length, 1);
  f.response(f.requests[0]); assert.equal(f.location.searchParams.get("view"), "queue");
});

for (const status of [204, 403, 500]) {
  test(`HTTP ${status} cannot be mistaken for a new rendered inbox`, () => {
    const f = fixture(); f.workspace.applyAssignmentFilter("agent-1"); f.response(f.requests[0], status);
    assert.equal(f.pushes.length, 0); assert.equal(f.workspace.filterLoading, false);
    assert.equal(f.workspace.inboxRefreshState, "error");
  });
}

test("a login document with HTTP 200 is rejected rather than replacing the queue", () => {
  const f = fixture(); f.workspace.navigatePage("/admin/inbox?page=2");
  assert.equal(f.response(f.requests[0], 200, "<html><form>Login</form></html>").shouldSwap, false);
  assert.equal(f.pushes.length, 0); assert.equal(f.rendered.page, 1);
  assert.equal(f.workspace.inboxRefreshState, "error");
});

test("missing target and synchronous transport errors release the loader without changing URL", () => {
  const f = fixture(); f.nodes.delete("#inbox-conversation-queue");
  f.workspace.navigatePage("/admin/inbox?page=2");
  assert.equal(f.pushes.length, 0); assert.equal(f.workspace.filterLoading, false);
  assert.equal(f.workspace.inboxRefreshState, "error");
});

test("transport completion cannot mark a delayed swap as rendered", () => {
  const f = fixture(); f.workspace.applyAssignmentFilter("agent-1");
  f.requests[0].xhr.status = 200;
  f.emit("htmx:afterRequest", { ...f.requests[0].detail, successful: true });
  assert.equal(f.workspace.filterLoading, true); assert.equal(f.pushes.length, 0);
  f.response(f.requests[0]); assert.equal(f.workspace.filterLoading, false);
});

test("rejected promise is reported without an unhandled rejection", async () => {
  const f = fixture(); f.window.htmx.ajax = () => Promise.reject(new Error("Network error"));
  f.workspace.applyAssignmentFilter("agent-1"); await Promise.resolve();
  assert.equal(f.workspace.filterLoading, false); assert.equal(f.pushes.length, 0);
  assert.equal(f.workspace.inboxRefreshState, "error");
});

test("filtering from a conversation URL requests the canonical list endpoint", () => {
  const f = fixture("/admin/inbox/00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000001");
  f.workspace.applyAssignmentFilter("agent-1");
  assert.equal(new URL(f.requests[0].url, f.location).pathname, "/admin/inbox");
});

test("a late response after a terminal failure cannot change the displayed rows", () => {
  const f = fixture(); f.workspace.navigatePage("/admin/inbox?page=2"); f.failure(f.requests[0]);
  assert.equal(f.response(f.requests[0]).shouldSwap, false);
  assert.equal(f.rendered.page, 1); assert.equal(f.pushes.length, 0);
  assert.equal(f.workspace.inboxRefreshState, "error");
});

test("background refresh cannot erase a failed operator's retry intent", () => {
  const f = fixture(); f.workspace.applyAssignmentFilter("agent-1"); f.failure(f.requests[0]);
  for (const intent of ["poll", "read_state", "realtime"]) f.workspace.refreshSidebar(intent);
  assert.equal(f.requests.length, 1);
  assert.equal(f.workspace.inboxRefreshState, "error");
  assert.equal(f.workspace.failedListNavigation.intent, "operator_filter");
  f.workspace.retryListRequest(); f.response(f.requests[1]);
  f.workspace.refreshSidebar("poll"); assert.equal(f.requests.length, 3);
});

test("a failed background refresh may retry automatically", () => {
  const f = fixture(); f.workspace.refreshSidebar("poll"); f.failure(f.requests[0]);
  f.workspace.refreshSidebar("poll"); assert.equal(f.requests.length, 2);
  f.response(f.requests[1]); assert.equal(f.workspace.listRequestError, "");
});

test("retrying failed Back navigation updates both the rendered page and restored URL", () => {
  const f = fixture("/admin/inbox?page=2");
  f.location.href = "https://inbox.example.test/admin/inbox?page=1";
  f.emit("popstate", {}, "window"); f.failure(f.requests[0]);
  assert.equal(f.location.searchParams.get("page"), "2");
  f.workspace.retryListRequest(); f.response(f.requests[1]);
  assert.equal(f.location.searchParams.get("page"), "1"); assert.equal(f.rendered.page, 1);
  assert.equal(f.pushes.length, 0);
});

test("history of a direct thread URL requests the list endpoint and then restores the thread", () => {
  const id = "00000000-0000-0000-0000-000000000001";
  const f = fixture();
  const opened = [];
  f.workspace.refreshThread = (selected) => opened.push(selected);
  f.location.href = `https://inbox.example.test/admin/inbox/${id}`;
  f.emit("popstate", {}, "window");
  assert.equal(new URL(f.requests[0].url, f.location).pathname, "/admin/inbox");
  assert.equal(f.workspace.selectedId, "");
  f.response(f.requests[0]);
  assert.equal(f.workspace.selectedId, id); assert.deepEqual(opened, [id]);
  assert.equal(f.location.pathname, "/admin/inbox"); assert.equal(f.location.searchParams.get("c"), id);
  assert.equal(f.pushes.length, 0);
});

test("a refused current swap releases its own loader with actionable failure", () => {
  const f = fixture(); f.workspace.applyAssignmentFilter("agent-1");
  const record = f.requests[0]; record.xhr.status = 200;
  f.emit("htmx:beforeSwap", { ...record.detail, shouldSwap: false });
  f.emit("htmx:afterRequest", { ...record.detail, successful: true });
  assert.equal(f.workspace.filterLoading, false); assert.equal(f.pushes.length, 0);
  assert.equal(f.workspace.inboxRefreshState, "error");
});

for (const kind of ["swapError", "onLoadError"]) {
  test(`${kind} releases loading without making navigation look successful`, () => {
    const f = fixture(); f.workspace.navigatePage("/admin/inbox?page=2");
    const record = f.requests[0]; record.xhr.status = 200;
    f.emit(`htmx:${kind}`, record.detail);
    assert.equal(f.workspace.filterLoading, false); assert.equal(f.pushes.length, 0);
    assert.equal(f.workspace.inboxRefreshState, "error");
  });
}

test("changing threads while a list is loading preserves the newer selected thread", () => {
  const f = fixture("/admin/inbox?view=all&c=old-thread", "old-thread");
  f.workspace.applyAssignmentFilter("agent-1");
  f.workspace.selectedId = "new-thread";
  f.response(f.requests[0]);
  assert.equal(f.workspace.selectedId, "new-thread");
  const query = f.location.searchParams;
  assert.equal(query.get("conversation_id") || query.get("c"), "new-thread");
  assert.equal(f.rendered.draft, "Unsent reply");
});

test("search replaces the URL only after the matching rendered response", () => {
  const f = fixture("/admin/inbox?assigned_person_id=agent-1&page=3");
  f.workspace.searchConversations("first"); f.workspace.searchConversations("latest");
  assert.equal(f.replacements.length, 0); assert.equal(f.requests[0].xhr.aborted, true);
  f.response(f.requests[1]);
  assert.equal(f.location.searchParams.get("search"), "latest");
  assert.equal(f.location.searchParams.get("page"), null);
  assert.equal(f.location.searchParams.get("assigned_person_id"), "agent-1");
  assert.equal(f.pushes.length, 0); assert.equal(f.replacements.length, 1);
});

test("actual pagination markup owns all three link clicks before document delegation", () => {
  const template = fs.readFileSync(path.join(__dirname, "../../templates/admin/inbox/_queue_macros.html"), "utf8");
  const macro = template.slice(template.indexOf("{% macro inbox_pagination("));
  assert.equal((macro.match(/@click\.prevent='navigatePage\(/g) || []).length, 3);
  assert.equal(macro.includes("hx-get="), false);
});

for (const [callback, event] of [["onabort", "sendAbort"], ["ontimeout", "timeout"], ["onerror", "sendError"]]) {
  test(`transport double matches bundled HTMX ${callback} ordering`, () => {
    const htmx = fs.readFileSync(path.join(__dirname, "../../static/js/vendor/htmx.min.js"), "utf8");
    const start = htmx.indexOf(`.${callback}=function()`);
    assert.ok(start >= 0);
    const body = htmx.slice(start, htmx.indexOf("};", start));
    const completed = body.indexOf('"htmx:afterRequest"');
    const failed = body.indexOf(`"htmx:${event}"`);
    assert.ok(completed >= 0 && failed > completed);
    assert.equal(body.includes("successful="), false);
  });
}

for (const action of ["filter", "next", "previous"]) {
  test(`${action} never reloads a selected conversation or composer`, () => {
    const f = fixture("/admin/inbox?view=all&page=2&c=thread-1", "thread-1");
    f.workspace.refreshThread = () => assert.fail("List navigation must not reload the thread");
    if (action === "filter") f.workspace.applyAssignmentFilter("agent-1");
    else f.workspace.navigatePage(`/admin/inbox?view=all&page=${action === "next" ? 3 : 1}&c=thread-1`);
    f.response(f.requests[0]);
    assert.equal(f.requests.length, 1);
    assert.notEqual(f.requests[0].options.target, "#triage-detail");
    assert.equal(f.workspace.selectedId, "thread-1");
  });
}
