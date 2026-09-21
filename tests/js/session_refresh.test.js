'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const sessionRefresh = require('../../static/js/session-refresh.js');

function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function sharedStorage() {
    const values = new Map();
    return {
        getItem(key) {
            return values.has(key) ? values.get(key) : null;
        },
        setItem(key, value) {
            values.set(key, String(value));
        },
        removeItem(key) {
            values.delete(key);
        },
    };
}

function broadcastChannelClass() {
    const channels = new Map();
    return class FakeBroadcastChannel {
        constructor(name) {
            this.name = name;
            this.listeners = new Set();
            if (!channels.has(name)) {
                channels.set(name, new Set());
            }
            channels.get(name).add(this);
        }

        addEventListener(type, listener) {
            if (type === 'message') {
                this.listeners.add(listener);
            }
        }

        removeEventListener(type, listener) {
            if (type === 'message') {
                this.listeners.delete(listener);
            }
        }

        postMessage(data) {
            for (const channel of channels.get(this.name) || []) {
                if (channel === this) {
                    continue;
                }
                setTimeout(() => {
                    for (const listener of channel.listeners) {
                        listener({ data });
                    }
                }, 0);
            }
        }

        close() {
            channels.get(this.name).delete(this);
            this.listeners.clear();
        }
    };
}

function fakeWindow({ storage, channelClass, fetch }) {
    const listeners = new Map();
    const documentListeners = new Map();
    return {
        Date,
        URL,
        crypto: { randomUUID: () => `tab-${Math.random()}` },
        localStorage: storage,
        BroadcastChannel: channelClass,
        navigator: {},
        fetch,
        location: {
            href: 'https://oss.example.test/admin/dashboard',
            pathname: '/admin/dashboard',
            search: '',
        },
        addEventListener(type, listener) {
            if (!listeners.has(type)) {
                listeners.set(type, new Set());
            }
            listeners.get(type).add(listener);
        },
        removeEventListener(type, listener) {
            if (listeners.has(type)) {
                listeners.get(type).delete(listener);
            }
        },
        setInterval,
        clearInterval,
        setTimeout,
        clearTimeout,
        document: {
            readyState: 'complete',
            hidden: false,
            addEventListener(type, listener) {
                if (!documentListeners.has(type)) {
                    documentListeners.set(type, new Set());
                }
                documentListeners.get(type).add(listener);
            },
            dispatchEvent(event) {
                for (const listener of documentListeners.get(event.type) || []) {
                    listener(event);
                }
            },
        },
    };
}

test('session refresh is shared by tabs instead of duplicated', async () => {
    const storage = sharedStorage();
    const channelClass = broadcastChannelClass();
    let fetchCount = 0;
    const fetch = async () => {
        fetchCount += 1;
        await delay(25);
        return {
            status: 204,
            redirected: false,
            url: 'https://oss.example.test/auth/session/refresh',
        };
    };

    const first = sessionRefresh.createSessionRefreshCoordinator(
        fakeWindow({ storage, channelClass, fetch }),
        { refreshUrl: '/auth/session/refresh', loginUrl: '/auth/login' },
    );
    const second = sessionRefresh.createSessionRefreshCoordinator(
        fakeWindow({ storage, channelClass, fetch }),
        { refreshUrl: '/auth/session/refresh', loginUrl: '/auth/login' },
    );

    const results = await Promise.all([
        first.refreshSession(),
        second.refreshSession(),
    ]);

    assert.equal(fetchCount, 1);
    assert.equal(results[0].status, 204);
    assert.equal(results[1].status, 204);
    first.close();
    second.close();
});

test('a login redirect is shared across waiting tabs', async () => {
    const storage = sharedStorage();
    const channelClass = broadcastChannelClass();
    let fetchCount = 0;
    const fetch = async () => {
        fetchCount += 1;
        await delay(25);
        return {
            status: 200,
            redirected: true,
            url: 'https://oss.example.test/auth/login',
        };
    };

    const firstWindow = fakeWindow({ storage, channelClass, fetch });
    const secondWindow = fakeWindow({ storage, channelClass, fetch });
    const first = sessionRefresh.createSessionRefreshCoordinator(
        firstWindow,
        { refreshUrl: '/auth/session/refresh', loginUrl: '/auth/login' },
    );
    const second = sessionRefresh.createSessionRefreshCoordinator(
        secondWindow,
        { refreshUrl: '/auth/session/refresh', loginUrl: '/auth/login' },
    );

    await Promise.all([first.refreshSession(), second.refreshSession()]);

    assert.equal(fetchCount, 1);
    assert.match(firstWindow.location.href, /\/auth\/login\?next=/);
    assert.match(secondWindow.location.href, /\/auth\/login\?next=/);
    first.close();
    second.close();
});

test('several requests in one tab share one renewal promise', async () => {
    const storage = sharedStorage();
    let fetchCount = 0;
    const fetch = async (_url, options) => {
        fetchCount += 1;
        assert.equal(options.method, 'POST');
        await delay(25);
        return {
            status: 204,
            redirected: false,
            url: 'https://oss.example.test/auth/session/refresh',
            headers: { get: () => '2000000000' },
        };
    };
    const coordinator = sessionRefresh.createSessionRefreshCoordinator(
        fakeWindow({ storage, channelClass: broadcastChannelClass(), fetch }),
        { refreshUrl: '/auth/session/refresh', loginUrl: '/auth/login' },
    );

    const results = await Promise.all([
        coordinator.refreshSession(),
        coordinator.refreshSession(),
        coordinator.refreshSession(),
        coordinator.refreshSession(),
        coordinator.refreshSession(),
    ]);

    assert.equal(fetchCount, 1);
    assert.equal(results.length, 5);
    assert.equal(coordinator.expiresAtMs(), 2000000000 * 1000);
    coordinator.close();
});

test('an HTMX request waits when renewal is due', async () => {
    const storage = sharedStorage();
    let fetchCount = 0;
    const fetch = async () => {
        fetchCount += 1;
        await delay(20);
        return {
            status: 204,
            redirected: false,
            url: 'https://oss.example.test/auth/session/refresh',
            headers: { get: () => String(Math.floor(Date.now() / 1000) + 900) },
        };
    };
    const win = fakeWindow({
        storage,
        channelClass: broadcastChannelClass(),
        fetch,
    });
    const browserCoordinator = sessionRefresh.createSessionRefreshCoordinator(
        win,
        { refreshUrl: '/auth/session/refresh', loginUrl: '/auth/login', expiresAt: 0 },
    );
    let prevented = false;
    let issued = false;
    const event = {
        preventDefault() {
            prevented = true;
        },
        detail: {
            issueRequest(skipConfirmation) {
                assert.equal(skipConfirmation, true);
                issued = true;
            },
        },
    };

    const paused = sessionRefresh._internal.pauseHtmxForRefresh(
        browserCoordinator,
        event,
    );
    assert.equal(paused, true);
    assert.equal(prevented, true);
    assert.equal(issued, false);
    await new Promise((resolve) => {
        const timer = setInterval(() => {
            if (issued) {
                clearInterval(timer);
                resolve();
            }
        }, 5);
    });
    assert.equal(fetchCount, 1);
    assert.equal(issued, true);
    browserCoordinator.close();
});
