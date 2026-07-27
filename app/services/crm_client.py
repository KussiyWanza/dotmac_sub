"""CRM API client for DotMac Omni CRM integration.

Provides authenticated HTTP access to tickets, work orders, and subscriber
data in the external CRM system.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar, cast

import httpx
from dotmac_integration import IntegrationHttpClient
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.observability import get_request_id
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


# Response cache TTLs (seconds). Portal list/detail pages are read-heavy and
# tolerate brief staleness; caching them stops every page load from fanning
# out live HTTP calls per subscriber account.
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


_CACHE_LIST_TTL = _env_int("CRM_CACHE_LIST_SECONDS", 60)
_CACHE_DETAIL_TTL = _env_int("CRM_CACHE_DETAIL_SECONDS", 30)

# Bounded retry for transient rate-limit / unavailable responses. A 429/503
# means the request was rejected (not processed), so retrying — including for
# POSTs — cannot double-apply. Honour the server's Retry-After when present,
# else back off exponentially, each delay capped so total wait stays well
# inside the Celery task time limits.
_RETRY_STATUSES = frozenset({429, 503})
_RETRY_MAX_ATTEMPTS = _env_int("CRM_RETRY_MAX_ATTEMPTS", 2)  # extra tries
_RETRY_MAX_SLEEP = float(_env_int("CRM_RETRY_MAX_SLEEP_SECONDS", 8))


def _resolve_scheduler_int(
    db: Session | None,
    key: str,
    env_name: str,
    default: int,
) -> int:
    value: object | None = None
    if db is not None:
        try:
            value = resolve_value(db, SettingDomain.scheduler, key)
        except Exception:
            value = None
    if value is None:
        return _env_int(env_name, default)
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return _env_int(env_name, default)


def _retry_after_seconds(resp: httpx.Response, *, max_sleep: float) -> float | None:
    """Parsed numeric ``Retry-After`` seconds, clamped to ``max_sleep``.

    ``None`` when the header is absent or non-numeric (the HTTP-date form is
    rare here), so callers fall back to exponential backoff.
    """
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return min(max_sleep, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return None


def _retry_delay(
    resp: httpx.Response,
    attempt: int,
    *,
    max_sleep: float | None = None,
) -> float:
    """Seconds to wait before the next retry of a 429/503 response.

    Prefers a numeric ``Retry-After`` header; falls back to exponential
    backoff (0.5s, 1s, 2s, …). Always clamped to ``_RETRY_MAX_SLEEP``.
    """
    sleep_cap = _RETRY_MAX_SLEEP if max_sleep is None else max(0.0, max_sleep)
    parsed = _retry_after_seconds(resp, max_sleep=sleep_cap)
    if parsed is not None:
        return parsed
    return min(sleep_cap, 0.5 * (2**attempt))


class CRMClientError(Exception):
    """Base exception for CRM client errors."""


class _CRMReachabilityCircuit:
    """Short-cooldown breaker tripped by connection/timeout failures.

    A slow or unreachable CRM otherwise makes every per-subscriber request wait
    out the full HTTP timeout; the reseller dashboard and work-order pages fan
    out across N accounts, turning one outage into N × timeout. The first
    connection failure trips this breaker so the remaining requests in the same
    window fast-fail instead of each blocking for the full timeout. A single
    successful call closes it again.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._open_until = 0.0

    def is_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def trip(self, cooldown_seconds: float | None = None) -> None:
        with self._lock:
            cooldown = (
                cooldown_seconds
                if cooldown_seconds is not None
                else float(_env_int("CRM_REACHABILITY_CIRCUIT_SECONDS", 30))
            )
            self._open_until = time.monotonic() + max(cooldown, 1.0)

    def reset(self) -> None:
        with self._lock:
            self._open_until = 0.0


_REACHABILITY_CIRCUIT = _CRMReachabilityCircuit()


# Pooled outbound HTTP clients, keyed by timeout (the only per-instance
# transport knob). Replaces the old per-request ``httpx.Client`` construction
# so connections are reused across calls and retry attempts.
_HTTP_CLIENT_LOCK = threading.Lock()
_HTTP_CLIENTS: dict[float, httpx.Client] = {}


def _pooled_client(timeout: float) -> httpx.Client:
    with _HTTP_CLIENT_LOCK:
        client = _HTTP_CLIENTS.get(timeout)
        if client is None:
            client = httpx.Client(timeout=timeout)
            _HTTP_CLIENTS[timeout] = client
        return client


def _outbound_request_id() -> str | None:
    """Propagate the inbound x-request-id onto outbound CRM calls.

    ``None`` outside a request (Celery workers), so the header is omitted.
    """
    return get_request_id() or None


class _RetryableStatusError(CRMClientError):
    """Internal: a 429/503 response, distinguishable so the shared retry
    engine retries it.

    Never escapes ``CRMClient`` — exhausted retries surface exactly like the
    old hand-rolled loop (``CRM API error: {status}`` from ``_request``; the
    final raw response returned from ``_raw_request``).
    """

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"CRM retryable status: {response.status_code}")
        self.response = response


class _CRMTransport:
    """Engine-facing client adapter: pooled ``httpx.Client`` + this edge's
    transport policy.

    Connection/timeout failures are deliberately NOT retried on this edge
    (unchanged from the old loop): the portal/reseller pages fan out across N
    accounts, so one outage must cost one timeout, not attempts × timeout.
    The first ``httpx.RequestError`` trips the reachability breaker and
    surfaces immediately as ``CRMClientError`` — before the shared engine's
    transport-retry path can see it.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        circuit_seconds: float,
        path: str,
        log_label: str = "request",
        content: bytes | None = None,
    ) -> None:
        self._client = client
        self._circuit_seconds = circuit_seconds
        self._path = path
        self._log_label = log_label
        self._content = content

    def request(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            if self._content is not None:
                return self._client.request(
                    method, url, content=self._content, headers=headers or {}
                )
            return self._client.request(
                method, url, params=params, json=json, headers=headers
            )
        except httpx.RequestError as exc:
            # Connection/timeout — CRM is unreachable, trip the breaker so the
            # rest of this request's fan-out fast-fails.
            _REACHABILITY_CIRCUIT.trip(self._circuit_seconds)
            logger.error(
                "CRM %s error %s %s: %s", self._log_label, method, self._path, exc
            )
            raise CRMClientError(f"CRM connection error: {exc}") from exc


class CRMClient:
    """HTTP client for the DotMac Omni CRM API.

    Handles JWT authentication with automatic token refresh,
    and provides typed methods for each CRM resource.
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout: float = 15.0,
        settings_db: Session | None = None,
        service_token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Static service ApiKey — the ONLY service credential (auth
        # unification S1): the staff username/password->JWT fallback is
        # retired. The parameters remain (ignored) for one release so call
        # sites migrate without a lockstep deploy.
        if username or password:
            logger.warning(
                "CRMClient staff credentials are retired and ignored; "
                "configure the integration installation service credential"
            )
        self.service_token = (service_token or "").strip()
        self.timeout = timeout
        self.settings_db = settings_db
        # Cache of minted read-union portal tokens, keyed by (subscriber, actor),
        # so the four mirror reconcilers reuse one token per subscriber per cycle
        # instead of minting four (P4). Value: (token, epoch_expiry).
        self._portal_read_tokens: dict[tuple[str, str], tuple[str, float]] = {}
        self._portal_token_lock = threading.RLock()

    @property
    def cache_list_ttl(self) -> int:
        return _resolve_scheduler_int(
            self.settings_db,
            "crm_cache_list_seconds",
            "CRM_CACHE_LIST_SECONDS",
            _CACHE_LIST_TTL,
        )

    @property
    def cache_detail_ttl(self) -> int:
        return _resolve_scheduler_int(
            self.settings_db,
            "crm_cache_detail_seconds",
            "CRM_CACHE_DETAIL_SECONDS",
            _CACHE_DETAIL_TTL,
        )

    @property
    def retry_max_attempts(self) -> int:
        return max(
            0,
            _resolve_scheduler_int(
                self.settings_db,
                "crm_retry_max_attempts",
                "CRM_RETRY_MAX_ATTEMPTS",
                _RETRY_MAX_ATTEMPTS,
            ),
        )

    @property
    def retry_max_sleep(self) -> float:
        return float(
            max(
                0,
                _resolve_scheduler_int(
                    self.settings_db,
                    "crm_retry_max_sleep_seconds",
                    "CRM_RETRY_MAX_SLEEP_SECONDS",
                    int(_RETRY_MAX_SLEEP),
                ),
            )
        )

    @property
    def circuit_seconds(self) -> float:
        return float(
            max(
                1,
                _resolve_scheduler_int(
                    self.settings_db,
                    "crm_reachability_circuit_seconds",
                    "CRM_REACHABILITY_CIRCUIT_SECONDS",
                    30,
                ),
            )
        )

    def _auth_headers(self) -> dict[str, str]:
        """Auth headers for a service-to-service CRM request.

        The static service ApiKey is the only service credential (auth
        unification S1, mirroring erp->sub): the staff username/password->JWT
        fallback is retired, so a missing key fails loudly instead of quietly
        borrowing a person's credentials.
        """
        if not self.base_url:
            raise CRMClientError("CRM is not configured")
        if not self.service_token:
            raise CRMClientError(
                "CRM service credential is not configured on the installation; "
                "the staff-credential login fallback has been retired"
            )
        return {"X-API-Key": self.service_token}

    def _build_engine(
        self,
        *,
        method: str,
        path: str,
        transport: _CRMTransport,
        handler: Callable[..., Any],
        pending: dict[str, httpx.Response],
        auth_headers: dict[str, str] | None = None,
        retry_log_fmt: str = "CRM %s %s -> %d, retrying in %.1fs (attempt %d/%d)",
    ) -> IntegrationHttpClient:
        """One shared-engine instance for one logical CRM request.

        The engine (``dotmac-integration-client``) owns the retry loop, header
        merging (per-request headers override the service key, exactly like the
        old ``{**auth, **headers}``), and x-request-id propagation. This
        builder keeps the edge's policy verbatim: retry statuses {429, 503}
        ONLY, ``CRM_RETRY_MAX_ATTEMPTS`` extra tries, numeric ``Retry-After``
        honoured and capped at ``CRM_RETRY_MAX_SLEEP_SECONDS``, and no
        transport retries (see ``_CRMTransport``).
        """
        retry_max_attempts = self.retry_max_attempts
        retry_max_sleep = self.retry_max_sleep

        def _backoff(attempt: int) -> float:
            resp = pending.pop("response", None)
            status = resp.status_code if resp is not None else 0
            delay: float | None = None
            if resp is not None:
                delay = _retry_after_seconds(resp, max_sleep=retry_max_sleep)
            if delay is None:
                # Same base/cap as the old loop; jitter added so concurrent
                # callers decorrelate (the shared engine's backoff shape).
                jitter = random.uniform(0.0, 0.25)  # noqa: S311  # nosec B311 - retry jitter
                delay = min(retry_max_sleep, 0.5 * (2**attempt)) + jitter
            logger.warning(
                retry_log_fmt,
                method,
                path,
                status,
                delay,
                attempt + 1,
                retry_max_attempts,
            )
            # The engine performs the single ``time.sleep(delay)`` per retry.
            # Tests that patch ``app.services.crm_client.time.sleep`` still
            # neutralise it: that target is the shared ``time`` module object,
            # the same one the engine calls.
            return delay

        return IntegrationHttpClient(
            client_factory=lambda: transport,
            response_handler=handler,
            backoff=_backoff,
            max_attempts=retry_max_attempts + 1,
            retryable_excs=(_RetryableStatusError,),
            non_retryable_excs=(CRMClientError, httpx.HTTPStatusError),
            auth_headers=auth_headers,
            edge="sub->crm",
            request_id_provider=_outbound_request_id,
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Make an authenticated request to the CRM API.

        Returns:
            Parsed JSON response.

        Raises:
            CRMClientError: On any request failure.
        """
        if _REACHABILITY_CIRCUIT.is_open():
            raise CRMClientError("CRM temporarily unavailable (circuit open)")

        auth_headers = self._auth_headers()
        url = f"{self.base_url}{path}"
        pending: dict[str, httpx.Response] = {}

        def _handle(resp: httpx.Response) -> Any:
            if resp.status_code in _RETRY_STATUSES:
                pending["response"] = resp
                raise _RetryableStatusError(resp)
            resp.raise_for_status()
            _REACHABILITY_CIRCUIT.reset()
            if not resp.text:
                return {}
            return resp.json()

        engine = self._build_engine(
            method=method,
            path=path,
            transport=_CRMTransport(
                _pooled_client(self.timeout),
                circuit_seconds=self.circuit_seconds,
                path=path,
            ),
            handler=_handle,
            pending=pending,
            auth_headers=auth_headers,
        )
        try:
            return engine.request(
                method, url, params=params, json_data=json_data, headers=headers
            )
        except _RetryableStatusError as e:
            # Retries exhausted: same error surface as the old loop's final
            # ``raise_for_status()``.
            logger.error(
                "CRM API error %s %s: %d %s",
                method,
                path,
                e.response.status_code,
                e.response.text[:200],
            )
            raise CRMClientError(f"CRM API error: {e.response.status_code}") from e
        except httpx.HTTPStatusError as e:
            logger.error(
                "CRM API error %s %s: %d %s",
                method,
                path,
                e.response.status_code,
                e.response.text[:200],
            )
            raise CRMClientError(f"CRM API error: {e.response.status_code}") from e

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        content: bytes,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send an unauthenticated raw request with CRM retry/circuit handling."""
        if _REACHABILITY_CIRCUIT.is_open():
            raise CRMClientError("CRM temporarily unavailable (circuit open)")
        if not self.base_url:
            raise CRMClientError("CRM is not configured")

        url = f"{self.base_url}{path}"
        pending: dict[str, httpx.Response] = {}

        def _handle(resp: httpx.Response) -> httpx.Response:
            if resp.status_code in _RETRY_STATUSES:
                pending["response"] = resp
                raise _RetryableStatusError(resp)
            _REACHABILITY_CIRCUIT.reset()
            return resp

        engine = self._build_engine(
            method=method,
            path=path,
            transport=_CRMTransport(
                _pooled_client(self.timeout),
                circuit_seconds=self.circuit_seconds,
                path=path,
                log_label="raw request",
                content=content,
            ),
            handler=_handle,
            pending=pending,
            retry_log_fmt="CRM raw %s %s -> %d, retrying in %.1fs (attempt %d/%d)",
        )
        try:
            return engine.request(method, url, headers=headers or {})
        except _RetryableStatusError as e:
            # Retries exhausted: the old loop returned the final 429/503
            # response to the caller rather than raising.
            _REACHABILITY_CIRCUIT.reset()
            return e.response

    def _cached_get(
        self,
        path: str,
        params: dict[str, Any] | None,
        ttl: int,
    ) -> Any:
        """GET with a short-lived Redis response cache.

        On cache hit, returns the stored JSON without touching the CRM. On miss,
        performs the request and caches a successful result for ``ttl`` seconds.
        Redis being unavailable degrades transparently to a live request.
        """
        from app.services.session_store import get_session_redis

        redis = get_session_redis()
        cache_key: str | None = None
        if redis is not None and ttl > 0:
            raw_key = json.dumps([path, params or {}], sort_keys=True)
            digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            cache_key = f"crm:resp:{digest}"
            try:
                cached = redis.get(cache_key)
                if cached is not None:
                    return json.loads(cast("str | bytes | bytearray", cached))
            except Exception as exc:  # noqa: BLE001
                logger.warning("CRM cache get failed for %s: %s", path, exc)

        data = self._request("GET", path, params=params)

        if redis is not None and cache_key is not None:
            try:
                redis.setex(cache_key, ttl, json.dumps(data))
            except Exception as exc:  # noqa: BLE001
                logger.warning("CRM cache set failed for %s: %s", path, exc)
        return data

    # ── Subscriber resolution ────────────────────────────────────────────

    def resolve_subscriber_id(self, splynx_customer_id: int) -> str | None:
        """Look up CRM subscriber UUID by legacy external_id.

        Returns:
            CRM subscriber UUID string, or None if not found.
        """
        try:
            data = self._request(
                "GET",
                "/api/v1/subscribers",
                params={
                    "search": str(splynx_customer_id),
                    "external_system": "splynx",
                    "per_page": 10,
                },
            )
            items = data if isinstance(data, list) else data.get("items", [])
            for item in items:
                if str(item.get("external_id") or "") == str(splynx_customer_id):
                    return str(item["id"])
        except CRMClientError:
            logger.warning(
                "CRM subscriber lookup failed for splynx_id=%s", splynx_customer_id
            )
        return None

    def get_subscriber(self, subscriber_id: str) -> dict[str, Any]:
        """Get a CRM subscriber by UUID."""
        return self._cached_get(
            f"/api/v1/subscribers/{subscriber_id}", None, self.cache_detail_ttl
        )

    def list_subscribers(
        self,
        *,
        external_system: str | None = None,
        page: int = 1,
        per_page: int = 100,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        """List CRM subscribers with CRM's page/per_page pagination."""
        params: dict[str, Any] = {
            "page": max(page, 1),
            "per_page": min(max(per_page, 1), 100),
        }
        if external_system:
            params["external_system"] = external_system
        data = (
            self._cached_get("/api/v1/subscribers", params, self.cache_list_ttl)
            if use_cache
            else self._request("GET", "/api/v1/subscribers", params=params)
        )
        return data if isinstance(data, list) else data.get("items", [])

    # ── Tickets ──────────────────────────────────────────────────────────

    def list_tickets(
        self,
        subscriber_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "created_at",
        order_dir: str = "desc",
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        """List tickets, optionally filtered by CRM subscriber."""
        params: dict[str, Any] = {
            "limit": min(max(limit, 1), 200),
            "offset": max(offset, 0),
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if subscriber_id:
            params["subscriber_id"] = subscriber_id
        data = (
            self._cached_get("/api/v1/tickets", params, self.cache_list_ttl)
            if use_cache
            else self._request("GET", "/api/v1/tickets", params=params)
        )
        return data if isinstance(data, list) else data.get("items", [])

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Get a single ticket by ID."""
        return self._cached_get(
            f"/api/v1/tickets/{ticket_id}", None, self.cache_detail_ttl
        )

    def list_ticket_comments(
        self, ticket_id: str, *, use_cache: bool = True
    ) -> list[dict[str, Any]]:
        """List comments for a ticket."""
        params = {"ticket_id": ticket_id, "limit": 200}
        data = (
            self._cached_get(
                "/api/v1/ticket-comments",
                params,
                self.cache_detail_ttl,
            )
            if use_cache
            else self._request("GET", "/api/v1/ticket-comments", params=params)
        )
        return data if isinstance(data, list) else data.get("items", [])

    def create_portal_session(
        self,
        *,
        crm_subscriber_id: str,
        actor: str = "subscriber",
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Mint a customer Portal API token (server-to-server, RFC #73).

        The CRM trusts this service JWT to assert the subject, so the client
        never authenticates to the CRM directly — it presents the returned
        short-lived, scoped token. Returns {portal_token, expires_at, api_base}.
        """
        payload: dict[str, Any] = {
            "crm_subscriber_id": crm_subscriber_id,
            "actor": actor,
            "scopes": list(scopes or []),
        }
        data = self._request(
            "POST", "/api/v1/portal/internal/session", json_data=payload
        )
        return data if isinstance(data, dict) else {}

    def create_widget_session(
        self,
        *,
        config_id: str,
        email: str,
        name: str | None,
        crm_subscriber_id: str | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Mint an identified CRM widget session for an authenticated portal."""
        payload: dict[str, Any] = {
            "config_id": config_id,
            "email": email,
            "metadata": metadata,
        }
        if name:
            payload["name"] = name
        if crm_subscriber_id:
            payload["crm_subscriber_id"] = crm_subscriber_id
        data = self._request(
            "POST", "/api/v1/widget/internal/session", json_data=payload
        )
        return data if isinstance(data, dict) else {}

    def _portal_token(
        self, crm_subscriber_id: str, scopes: list[str], actor: str = "subscriber"
    ) -> str:
        minted = self.create_portal_session(
            crm_subscriber_id=crm_subscriber_id, actor=actor, scopes=scopes
        )
        token = str(minted.get("portal_token") or "")
        if not token:
            raise CRMClientError("portal token mint returned an empty token")
        return token

    # External referrals and quotes still share one scoped read token. Projects
    # and work orders are native Sub domains and are intentionally absent.
    _PORTAL_READ_SCOPES: ClassVar[list[str]] = [
        "referrals:read",
        "quotes:read",
    ]

    @staticmethod
    def _portal_token_ttl_expiry(minted: dict, now: float) -> float:
        """Epoch time until which a minted portal token may be cached.

        Uses the token's own ``expires_at`` (epoch or ISO-8601) minus a 30s skew
        buffer; falls back to a conservative 60s when it can't be parsed.
        """
        raw = minted.get("expires_at")
        exp: float | None = None
        if isinstance(raw, (int, float)):
            exp = float(raw)
        elif isinstance(raw, str) and raw:
            with contextlib.suppress(ValueError):
                exp = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        if exp is None:
            return now + 60.0
        return exp - 30.0

    def _portal_read_token(
        self, crm_subscriber_id: str, actor: str = "subscriber"
    ) -> str:
        """A cached read-union portal token for a subscriber (P4).

        Reused across the four mirror reconcilers so one subscriber costs one
        mint per cycle, not four. Thread-safe; re-mints once the cached token
        nears expiry.
        """
        key = (crm_subscriber_id, actor)
        now = time.time()
        with self._portal_token_lock:
            cached = self._portal_read_tokens.get(key)
            if cached and cached[1] > now:
                return cached[0]

        minted = self.create_portal_session(
            crm_subscriber_id=crm_subscriber_id,
            actor=actor,
            scopes=self._PORTAL_READ_SCOPES,
        )
        token = str(minted.get("portal_token") or "")
        if not token:
            raise CRMClientError("portal token mint returned an empty token")
        with self._portal_token_lock:
            self._portal_read_tokens[key] = (
                token,
                self._portal_token_ttl_expiry(minted, now),
            )
        return token

    def get_portal_referrals(self, crm_subscriber_id: str) -> dict[str, Any]:
        """Read a subscriber's referrals from the CRM Portal API (server-side).

        Mints a scoped portal token then calls the portal API with it (the
        per-request ``Authorization`` overrides the service token). Used by the
        local-mirror reconcile, not the customer's own request path.
        """
        token = self._portal_read_token(crm_subscriber_id)
        data = self._request(
            "GET",
            "/api/v1/portal/referrals",
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}

    def get_portal_quotes(self, crm_subscriber_id: str) -> dict[str, Any]:
        """Read a subscriber's self-serve quotes (feasibility, estimate, deposit,
        status) from the CRM Portal API (server-side). Used by the mirror reconcile."""
        token = self._portal_read_token(crm_subscriber_id)
        data = self._request(
            "GET",
            "/api/v1/portal/quotes",
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}

    def request_portal_quote(
        self,
        crm_subscriber_id: str,
        *,
        latitude: float,
        longitude: float,
        address: str | None = None,
        region: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Request a map-pinned installation quote (write-through to the CRM
        Portal API). Returns the created quote payload (feasibility + estimate)."""
        token = self._portal_token(crm_subscriber_id, ["quotes:write"])
        payload: dict[str, Any] = {"latitude": latitude, "longitude": longitude}
        if address:
            payload["address"] = address
        if region:
            payload["region"] = region
        if note:
            payload["note"] = note
        data = self._request(
            "POST",
            "/api/v1/portal/quote-request",
            json_data=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}

    def accept_portal_quote(
        self,
        crm_subscriber_id: str,
        quote_id: str,
        *,
        deposit_reference: str,
        deposit_amount: str,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Accept a quote after the deposit is verified (write-through). The CRM
        records the deposit and triggers the sales-order + install-project."""
        token = self._portal_token(crm_subscriber_id, ["quotes:write"])
        payload: dict[str, Any] = {
            "deposit_reference": deposit_reference,
            "deposit_amount": deposit_amount,
        }
        if provider:
            payload["provider"] = provider
        data = self._request(
            "POST",
            f"/api/v1/portal/quotes/{quote_id}/accept",
            json_data=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}


# ── Singleton ────────────────────────────────────────────────────────────
