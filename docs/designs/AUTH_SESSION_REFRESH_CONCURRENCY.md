# Authentication Session Refresh Concurrency

Status: implemented

Owner: `app_sessions.refresh`

## Purpose

An admin page can start several background requests at the same time. When the
15-minute access token expires, those requests may all present the same refresh
token. Renewal must not mistake those requests for an attacker and log the
operator out.

## Server contract

- `sessions` is the authoritative record for the current and immediately
  previous refresh-token hashes, rotation time, client binding, status and
  expiry.
- `renew_authentication_session` is the only rotation owner.
- It locks the matching active session row before deciding what the supplied
  token means. Other renewals for that session wait for the lock.
- The first current-token request rotates the refresh token once.
- The immediately previous token may be replayed for five seconds only when its
  recorded browser binding still matches. That replay issues access through the
  adapter but does not rotate or return another refresh token.
- Previous-token reuse after five seconds, or from a different client binding,
  revokes the session. Unknown and expired tokens fail closed.
- Token values are never written to logs, audit metadata or events.
- The access token is minted before the owner transaction commits, using the
  effective access lifetime, algorithm and process-held signing key. The key is
  never returned by the owner or stored with the session.

For native clients, the stable device identifier is the binding. Browser
requests without a device identifier use the normalized user agent and
effective client address together. The effective address uses the same proxy
aware resolver used at login.

## Browser contract

The admin layout receives only the access-token expiry time; it never exposes a
token to JavaScript. One minute before expiry, the browser sends a CSRF-protected
background request to `POST /auth/session/refresh`.

Within one tab, all callers share one in-progress renewal promise. Across tabs,
the browser Web Locks API is preferred; the existing short-lived local-storage
lock and BroadcastChannel notification remain the fallback. HTMX requests that
arrive while renewal is due wait for that shared renewal before they are sent.

The endpoint returns no body. It replaces the HttpOnly access cookie, replaces
the refresh cookie only for the one real rotation, and returns the next access
expiry in a response header so the browser can schedule the following renewal.

## Failure behavior

- A `401` from background renewal sends the browser to login while preserving
  the current admin URL as the return target.
- A brief network failure does not invent a successful renewal.
- The normal five-second duplicate path remains active and keeps the current
  rotated refresh token unchanged.

## Verification

- `tests/test_auth_session_refresh.py` covers the overlap and fail-closed rules.
- `tests/integration/test_auth_session_refresh_concurrency.py` proves that two
  PostgreSQL workers rotate once and replay once under the row lock.
- `tests/js/session_refresh.test.js` covers same-tab and cross-tab sharing.
