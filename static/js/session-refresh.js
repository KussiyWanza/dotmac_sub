(function (root) {
    "use strict";

    const DEFAULT_INTERVAL_MS = 10 * 60 * 1000;
    const REFRESH_LEAD_MS = 60 * 1000;
    const LOCK_TTL_MS = 15 * 1000;
    const WAIT_TIMEOUT_MS = 12 * 1000;
    const POLL_MS = 100;
    const RECENT_RESULT_MS = 2 * 1000;
    const LOCK_KEY = "dotmac.sessionRefresh.lock.v1";
    const RESULT_KEY = "dotmac.sessionRefresh.result.v1";
    const CHANNEL_NAME = "dotmac.sessionRefresh.v1";

    function nowMs(win) {
        return win.Date.now();
    }

    function makeTabId(win) {
        const cryptoObj = win.crypto || {};
        if (typeof cryptoObj.randomUUID === "function") {
            return cryptoObj.randomUUID();
        }
        return `tab-${Math.random().toString(36).slice(2)}-${nowMs(win)}`;
    }

    function storageFor(win) {
        try {
            const storage = win.localStorage;
            const probe = `${LOCK_KEY}.probe`;
            storage.setItem(probe, "1");
            storage.removeItem(probe);
            return storage;
        } catch (err) {
            return null;
        }
    }

    function readJson(storage, key) {
        try {
            const value = storage.getItem(key);
            return value ? JSON.parse(value) : null;
        } catch (err) {
            return null;
        }
    }

    function writeJson(storage, key, value) {
        try {
            storage.setItem(key, JSON.stringify(value));
        } catch (err) {
            // A full or disabled storage area should not break auth refresh.
        }
    }

    function removeIfOwned(storage, ownerId) {
        const current = readJson(storage, LOCK_KEY);
        if (current && current.ownerId === ownerId) {
            storage.removeItem(LOCK_KEY);
        }
    }

    function tryAcquireRefreshLock(storage, ownerId, timestamp) {
        const current = readJson(storage, LOCK_KEY);
        if (
            current &&
            current.ownerId !== ownerId &&
            Number(current.expiresAt || 0) > timestamp
        ) {
            return false;
        }

        writeJson(storage, LOCK_KEY, {
            ownerId,
            expiresAt: timestamp + LOCK_TTL_MS,
        });

        const stored = readJson(storage, LOCK_KEY);
        return Boolean(stored && stored.ownerId === ownerId);
    }

    function loginRedirect(win, loginUrl) {
        const location = win.location || { pathname: "", search: "" };
        const next = `${location.pathname || ""}${location.search || ""}`;
        return `${loginUrl}?next=${encodeURIComponent(next)}`;
    }

    function responseReachedLogin(win, response, loginUrl) {
        if (!response || !response.redirected || !response.url) {
            return false;
        }
        try {
            const current = new win.URL(response.url, win.location.href);
            const login = new win.URL(loginUrl, win.location.href);
            return current.pathname === login.pathname;
        } catch (err) {
            return false;
        }
    }

    function shouldUseRecentResult(result, timestamp) {
        return Boolean(
            result &&
            Number(result.completedAt || 0) > 0 &&
            timestamp - Number(result.completedAt) < RECENT_RESULT_MS
        );
    }

    function openChannel(win) {
        if (typeof win.BroadcastChannel !== "function") {
            return null;
        }
        try {
            return new win.BroadcastChannel(CHANNEL_NAME);
        } catch (err) {
            return null;
        }
    }

    async function fetchRefresh(win, refreshUrl, loginUrl) {
        const csrfToken =
            typeof win.getCsrfToken === "function" ? win.getCsrfToken() : "";
        const response = await win.fetch(refreshUrl, {
            method: "POST",
            cache: "no-store",
            credentials: "same-origin",
            headers: {
                "X-Session-Refresh": "true",
                ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
            },
        });
        const expiresAtHeader =
            response.headers && typeof response.headers.get === "function"
                ? response.headers.get("X-Session-Expires-At")
                : null;
        const redirectTo =
            response.status === 401 || responseReachedLogin(win, response, loginUrl)
                ? loginRedirect(win, loginUrl)
                : null;
        return {
            completedAt: nowMs(win),
            status: response.status,
            redirectTo,
            expiresAt: Number(expiresAtHeader || 0),
        };
    }

    function publishResult(storage, channel, result) {
        writeJson(storage, RESULT_KEY, result);
        if (channel) {
            channel.postMessage({ type: "session-refresh-result", result });
        }
    }

    function waitForResult(win, storage, channel, startedAt) {
        return new Promise((resolve) => {
            let settled = false;
            let pollTimer = null;
            let timeoutTimer = null;

            function finish(result) {
                if (settled) {
                    return;
                }
                settled = true;
                if (pollTimer) {
                    win.clearInterval(pollTimer);
                }
                if (timeoutTimer) {
                    win.clearTimeout(timeoutTimer);
                }
                if (channel) {
                    channel.removeEventListener("message", onMessage);
                }
                win.removeEventListener("storage", onStorage);
                resolve(result || null);
            }

            function accept(result) {
                if (result && Number(result.completedAt || 0) >= startedAt) {
                    finish(result);
                }
            }

            function onMessage(event) {
                if (event.data && event.data.type === "session-refresh-result") {
                    accept(event.data.result);
                }
            }

            function onStorage(event) {
                if (event.key === RESULT_KEY && event.newValue) {
                    try {
                        accept(JSON.parse(event.newValue));
                    } catch (err) {
                        // Ignore malformed cross-tab notifications.
                    }
                }
            }

            if (channel) {
                channel.addEventListener("message", onMessage);
            }
            win.addEventListener("storage", onStorage);
            pollTimer = win.setInterval(() => {
                accept(readJson(storage, RESULT_KEY));
            }, POLL_MS);
            timeoutTimer = win.setTimeout(() => finish(null), WAIT_TIMEOUT_MS);
        });
    }

    function applyRefreshResult(win, result) {
        if (result && result.redirectTo) {
            win.location.href = result.redirectTo;
        }
    }

    function createSessionRefreshCoordinator(win, config) {
        const storage = storageFor(win);
        const channel = openChannel(win);
        const ownerId = makeTabId(win);
        let inFlight = null;
        let expiresAtMs = Number(config.expiresAt || 0) * 1000;

        function rememberResult(result) {
            const resultExpiry = Number((result && result.expiresAt) || 0);
            if (resultExpiry > 0) {
                expiresAtMs = resultExpiry * 1000;
            }
            return result;
        }

        async function refreshWithStorageLock() {
            if (!storage) {
                return rememberResult(
                    await fetchRefresh(win, config.refreshUrl, config.loginUrl),
                );
            }

            const startedAt = nowMs(win);
            const recent = readJson(storage, RESULT_KEY);
            if (shouldUseRecentResult(recent, startedAt)) {
                applyRefreshResult(win, recent);
                return rememberResult(recent);
            }

            if (!tryAcquireRefreshLock(storage, ownerId, startedAt)) {
                const result = await waitForResult(win, storage, channel, startedAt);
                if (result) {
                    applyRefreshResult(win, result);
                    return rememberResult(result);
                }
            }

            if (!tryAcquireRefreshLock(storage, ownerId, nowMs(win))) {
                return null;
            }

            try {
                const result = await fetchRefresh(win, config.refreshUrl, config.loginUrl);
                publishResult(storage, channel, result);
                applyRefreshResult(win, result);
                return rememberResult(result);
            } catch (err) {
                return null;
            } finally {
                removeIfOwned(storage, ownerId);
            }
        }

        async function coordinateRefresh() {
            const locks = win.navigator && win.navigator.locks;
            if (locks && typeof locks.request === "function") {
                return locks.request(LOCK_KEY, async () => {
                    const recent = storage ? readJson(storage, RESULT_KEY) : null;
                    if (shouldUseRecentResult(recent, nowMs(win))) {
                        applyRefreshResult(win, recent);
                        return rememberResult(recent);
                    }
                    const result = await fetchRefresh(
                        win,
                        config.refreshUrl,
                        config.loginUrl,
                    );
                    if (storage) {
                        publishResult(storage, channel, result);
                    }
                    applyRefreshResult(win, result);
                    return rememberResult(result);
                });
            }
            return refreshWithStorageLock();
        }

        function refreshSession() {
            if (inFlight) {
                return inFlight;
            }
            inFlight = coordinateRefresh()
                .catch(() => null)
                .finally(() => {
                    inFlight = null;
                });
            return inFlight;
        }

        return {
            refreshSession,
            isRefreshing() {
                return Boolean(inFlight);
            },
            needsRefresh(leadMs = REFRESH_LEAD_MS) {
                return expiresAtMs <= 0 || expiresAtMs - nowMs(win) <= leadMs;
            },
            expiresAtMs() {
                return expiresAtMs;
            },
            close() {
                if (channel) {
                    channel.close();
                }
            },
        };
    }

    /**
     * Keeps user sessions alive by periodically pinging a refresh endpoint.
     * Open tabs coordinate so only one tab refreshes at a time.
     *
     * @param {Object} config
     * @param {string} config.refreshUrl - Endpoint for session refresh
     * @param {string} config.loginUrl - Redirect URL on session expiry
     * @param {number} [config.intervalMs=600000] - Refresh interval
     */
    function pauseHtmxForRefresh(coordinator, event) {
        if (!coordinator.isRefreshing() && !coordinator.needsRefresh()) {
            return false;
        }
        event.preventDefault();
        coordinator.refreshSession().then((result) => {
            if (!result || (result.status !== 401 && !result.redirectTo)) {
                event.detail.issueRequest(true);
            }
        });
        return true;
    }

    function initSessionRefresh(config) {
        const intervalMs = config.intervalMs || DEFAULT_INTERVAL_MS;
        const coordinator = createSessionRefreshCoordinator(root, config);
        let refreshTimer = null;

        function scheduleRefresh() {
            if (refreshTimer) {
                root.clearTimeout(refreshTimer);
            }
            const expiry = coordinator.expiresAtMs();
            const delay = expiry > 0
                ? Math.max(0, expiry - nowMs(root) - REFRESH_LEAD_MS)
                : intervalMs;
            refreshTimer = root.setTimeout(async () => {
                const result = await coordinator.refreshSession();
                if (result && result.status >= 200 && result.status < 300) {
                    scheduleRefresh();
                } else if (!result || result.status !== 401) {
                    refreshTimer = root.setTimeout(scheduleRefresh, 30 * 1000);
                }
            }, delay);
        }

        function startRefresh() {
            scheduleRefresh();
        }

        if (root.document.readyState === "loading") {
            root.document.addEventListener("DOMContentLoaded", startRefresh);
        } else {
            startRefresh();
        }

        root.document.addEventListener("visibilitychange", () => {
            if (!root.document.hidden && coordinator.needsRefresh()) {
                coordinator.refreshSession();
            }
        });

        root.document.addEventListener("htmx:confirm", (event) => {
            pauseHtmxForRefresh(coordinator, event);
        });

        const closeCoordinator = coordinator.close.bind(coordinator);
        coordinator.close = function () {
            if (refreshTimer) {
                root.clearTimeout(refreshTimer);
            }
            closeCoordinator();
        };

        return coordinator;
    }

    root.initSessionRefresh = initSessionRefresh;
    root.createSessionRefreshCoordinator = createSessionRefreshCoordinator;

    if (typeof module !== "undefined" && module.exports) {
        module.exports = {
            createSessionRefreshCoordinator,
            initSessionRefresh,
            _internal: {
                loginRedirect,
                responseReachedLogin,
                shouldUseRecentResult,
                tryAcquireRefreshLock,
                pauseHtmxForRefresh,
            },
        };
    }
})(typeof window !== "undefined" ? window : globalThis);
