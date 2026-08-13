"""Sub's Redis as a `CacheStore` for the kernel's settings cache.

The kernel owns settings caching: the key, the scope segment, the TTL policy,
what is never cached, and what a write invalidates. This module is the
TRANSPORT it runs on and nothing else — three methods, no key building, no
decisions. That split is the point, and it is the same one the Dotmac
source-of-truth standard makes everywhere else: an external system is a
transport, not a decision system.

## Why this replaces `SettingsCache`

`app/services/settings_cache.py` cached resolved settings under
``settings:{domain}:{key}``. That key has NO SCOPE SEGMENT, which is verbatim
the defect `dotmac_kernel.settings_cache` cites `dotmac_erp` for: with Redis
shared across workers, one tenant is served the entry another populated,
deployment-wide, and nothing raises. Sub provisions one tenant (ADR-0009), so
it is dormant rather than harmless — it becomes live the day that changes,
which is precisely when nobody is looking for it.

Building a scope segment here would fix that instance and leave the shape:
a second implementation of a key model, drifting from the kernel's. So the key
is not built here at all. `CacheStore` is deliberately key-agnostic, and a
store that cannot construct a key cannot lose a segment from one.

## Degradation

Redis being down must never fail a settings read. Every method swallows
`RedisError` and reports the miss: `get` returns None (the kernel treats that
as a miss and resolves from the database), `set` drops the write, and
`delete_prefix` returns 0.

`delete_prefix` returning 0 on an error is the one that deserves a second
look, because a failed INVALIDATION is not a failed read — it leaves a stale
entry behind. That is what `SETTINGS_CACHE_TTL_SECONDS` is for: invalidation
is the mechanism, and the TTL is the bound on how wrong a missed one can leave
us.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import redis

from app.services.settings_cache import get_settings_redis

logger = logging.getLogger(__name__)


def _ttl_seconds() -> int:
    """The staleness ceiling, from configuration.

    It was a module constant, which made an operational, deployment-specific
    value a code change — against this repository's "everything by config" rule,
    and past a `ttl_seconds` parameter the kernel's `CacheStore` already
    accepts. Read per call rather than at import so a test can move it without
    reloading the module.
    """

    from app.config import settings

    return int(settings.settings_cache_ttl_seconds)


#: Batch size for prefix deletion, so invalidating a platform write does not
#: issue one round trip per key.
_SCAN_BATCH = 500


class RedisSettingsCacheStore:
    """A `dotmac_kernel.cache.CacheStore` over Sub's shared Redis.

    Structural conformance only — the kernel declares a `Protocol`, so there is
    nothing to subclass and nothing to import from it here.
    """

    def get(self, key: str) -> object | None:
        client = get_settings_redis()
        if client is None:
            return None
        try:
            raw = client.get(key)
        except redis.RedisError as exc:
            logger.debug("settings cache get failed: %s", exc)
            return None
        if raw is None:
            return None
        try:
            # `cast`: the shared client is typed as possibly-async, and this
            # module is the sync path.
            loaded: Any = json.loads(cast("str | bytes", raw))
        except ValueError:
            # An entry written by an older encoding, or a corrupted one. Treat
            # it as a miss: resolution rebuilds it, and the write below
            # overwrites it. A cache that cannot be re-read is a cache that
            # must not raise.
            return None
        # The kernel caches a `(value, source)` PAIR. JSON has no tuple, so a
        # round trip returns a list; the resolver unpacks either, but handing
        # back the shape that was stored keeps `cached()`'s contract honest for
        # anything that inspects it.
        if isinstance(loaded, list) and len(loaded) == 2:
            return (loaded[0], loaded[1])
        return loaded

    def set(self, key: str, value: object, *, ttl_seconds: int | None = None) -> None:
        client = get_settings_redis()
        if client is None:
            return
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError) as exc:
            # A value type whose resolved form is not JSON-serialisable. Not
            # cacheable through this transport, and NOT an error the read path
            # should see — the setting still resolves, just uncached.
            logger.debug("settings cache set skipped, not serialisable: %s", exc)
            return
        try:
            client.setex(key, ttl_seconds or _ttl_seconds(), payload)
        except redis.RedisError as exc:
            logger.debug("settings cache set failed: %s", exc)

    def delete_prefix(self, prefix: str) -> int:
        client = get_settings_redis()
        if client is None:
            return 0
        deleted = 0
        try:
            batch: list[str] = []
            # `scan_iter`, never `keys`: this runs on a write path against a
            # Redis shared with sessions and Celery, and `KEYS` blocks the
            # server for the length of the keyspace.
            for found in client.scan_iter(match=f"{prefix}*", count=_SCAN_BATCH):
                batch.append(found)
                if len(batch) >= _SCAN_BATCH:
                    deleted += cast("int", client.delete(*batch))
                    batch = []
            if batch:
                deleted += cast("int", client.delete(*batch))
        except redis.RedisError as exc:
            logger.debug("settings cache invalidation failed: %s", exc)
            return deleted
        return deleted


def install() -> None:
    """Install the store as the process-active settings cache.

    Called at import of `app.services.settings_spec`, beside the spec
    registration and for the same reason: a resolution must never happen
    before the thing it depends on exists. An uninstalled store is not an
    error — the kernel's cache is inert without one — so a missed install
    would show up as a performance regression rather than a failure, which is
    the kind of thing that survives for months.
    """

    from dotmac_kernel.settings_cache import install_settings_cache

    install_settings_cache(RedisSettingsCacheStore())
