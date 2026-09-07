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
    return {
        Date,
        URL,
        crypto: { randomUUID: () => `tab-${Math.random()}` },
        localStorage: storage,
        BroadcastChannel: channelClass,
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
            addEventListener() {},
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
            url: 'https://oss.example.test/admin/session/refresh',
        };
    };

    const first = sessionRefresh.createSessionRefreshCoordinator(
        fakeWindow({ storage, channelClass, fetch }),
        { refreshUrl: '/admin/session/refresh', loginUrl: '/auth/login' },
    );
    const second = sessionRefresh.createSessionRefreshCoordinator(
        fakeWindow({ storage, channelClass, fetch }),
        { refreshUrl: '/admin/session/refresh', loginUrl: '/auth/login' },
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
        { refreshUrl: '/admin/session/refresh', loginUrl: '/auth/login' },
    );
    const second = sessionRefresh.createSessionRefreshCoordinator(
        secondWindow,
        { refreshUrl: '/admin/session/refresh', loginUrl: '/auth/login' },
    );

    await Promise.all([first.refreshSession(), second.refreshSession()]);

    assert.equal(fetchCount, 1);
    assert.match(firstWindow.location.href, /\/auth\/login\?next=/);
    assert.match(secondWindow.location.href, /\/auth\/login\?next=/);
    first.close();
    second.close();
});
