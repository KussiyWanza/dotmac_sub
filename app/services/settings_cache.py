"""Redis-based settings cache for domain settings.

This module provides a centralized cache for domain settings using Redis,
eliminating race conditions with in-memory caches in multi-worker environments.

Uses the centralized Redis client with circuit breaker protection to prevent
retry storms during Redis outages.
"""

import json
import logging
from typing import Any, cast

import redis

from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)


def get_settings_redis() -> redis.Redis | None:
    """Get Redis client for settings cache.

    Uses the centralized Redis client with circuit breaker protection.
    Returns None if Redis is unavailable (circuit breaker open or connection failed).
    """
    return get_redis()


class SettingsCache:
    """RETIRED as the settings cache. No `app/` module uses it.

    `dotmac_kernel.settings_cache` owns settings caching — the key, the scope
    segment, the TTL policy, what is never cached and what a write invalidates
    — over the transport in `app/services/kernel_settings_cache_store.py`.
    `tests/architecture/test_kernel_owned_facts_have_one_expression.py` holds
    the "no `app/` user" line.

    Its key is `settings:{domain}:{key}`, with NO SCOPE SEGMENT. That is the
    cross-tenant leak `dotmac_kernel.settings_cache` cites `dotmac_erp` for:
    Redis is shared across workers, so one tenant is served the entry another
    populated. Dormant here because Sub provisions one tenant, and the reason
    this class must not acquire a new caller.

    It survives only because tests still call `invalidate` after writing a
    setting, to make their next read fresh. Those calls now clear a cache
    nobody reads — they are no-ops against the unit suite's refusing Redis, and
    misleading either way. Removing them, and then this class, is a tidy-up
    with no production effect.
    """

    PREFIX = "settings:"
    TTL = 30  # seconds

    @staticmethod
    def _cache_key(domain: str, key: str) -> str:
        """Build the Redis cache key."""
        return f"{SettingsCache.PREFIX}{domain}:{key}"

    @staticmethod
    def get(domain: str, key: str) -> Any | None:
        """Get a setting value from cache.

        Args:
            domain: The setting domain (e.g., "billing", "collections")
            key: The setting key

        Returns:
            The cached value, or None if not cached or on error
        """
        try:
            r = get_settings_redis()
            if r is None:
                return None
            cache_key = SettingsCache._cache_key(domain, key)
            value = cast(str | None, r.get(cache_key))
            if value is not None:
                return json.loads(value)
        except redis.RedisError as exc:
            logger.debug("Settings cache read failed: %s", exc)
        except json.JSONDecodeError as exc:
            logger.warning("Settings cache JSON decode failed: %s", exc)
        return None

    @staticmethod
    def set(domain: str, key: str, value: Any) -> bool:
        """Set a setting value in cache.

        Args:
            domain: The setting domain
            key: The setting key
            value: The value to cache (must be JSON serializable)

        Returns:
            True if cached successfully, False on error
        """
        try:
            r = get_settings_redis()
            if r is None:
                return False
            cache_key = SettingsCache._cache_key(domain, key)
            r.setex(cache_key, SettingsCache.TTL, json.dumps(value))
            return True
        except redis.RedisError as exc:
            logger.debug("Settings cache write failed: %s", exc)
        except (TypeError, ValueError) as exc:
            logger.warning("Settings cache JSON encode failed: %s", exc)
        return False

    @staticmethod
    def invalidate(domain: str, key: str) -> bool:
        """Invalidate a specific setting in cache.

        Args:
            domain: The setting domain
            key: The setting key

        Returns:
            True if invalidated successfully, False on error
        """
        try:
            r = get_settings_redis()
            if r is None:
                return False
            cache_key = SettingsCache._cache_key(domain, key)
            r.delete(cache_key)
            return True
        except redis.RedisError as exc:
            logger.debug("Settings cache invalidate failed: %s", exc)
        return False

    @staticmethod
    def invalidate_domain(domain: str) -> int:
        """Invalidate all settings for a domain.

        Args:
            domain: The setting domain

        Returns:
            Number of keys invalidated, or -1 on error
        """
        try:
            r = get_settings_redis()
            if r is None:
                return -1
            pattern = f"{SettingsCache.PREFIX}{domain}:*"
            count = 0
            for key in r.scan_iter(pattern):
                r.delete(key)
                count += 1
            return count
        except redis.RedisError as exc:
            logger.debug("Settings cache domain invalidate failed: %s", exc)
        return -1

    @staticmethod
    def get_multi(domain: str, keys: list[str]) -> dict[str, Any]:
        """Get multiple setting values from cache atomically.

        Args:
            domain: The setting domain
            keys: List of setting keys

        Returns:
            Dict mapping keys to their cached values (missing keys are omitted)
        """
        result: dict[str, Any] = {}
        try:
            r = get_settings_redis()
            if r is None:
                return result
            cache_keys = [SettingsCache._cache_key(domain, k) for k in keys]
            values = cast(list[str | None], r.mget(cache_keys))
            for key, value in zip(keys, values):
                if value is not None:
                    try:
                        result[key] = json.loads(value)
                    except json.JSONDecodeError:
                        pass
        except redis.RedisError as exc:
            logger.debug("Settings cache multi-get failed: %s", exc)
        return result

    @staticmethod
    def set_multi(domain: str, values: dict[str, Any]) -> bool:
        """Set multiple setting values in cache atomically.

        Args:
            domain: The setting domain
            values: Dict mapping keys to values

        Returns:
            True if all values cached successfully, False on error
        """
        try:
            r = get_settings_redis()
            if r is None:
                return False
            pipe = r.pipeline()
            for key, value in values.items():
                cache_key = SettingsCache._cache_key(domain, key)
                pipe.setex(cache_key, SettingsCache.TTL, json.dumps(value))
            pipe.execute()
            return True
        except redis.RedisError as exc:
            logger.debug("Settings cache multi-set failed: %s", exc)
        except (TypeError, ValueError) as exc:
            logger.warning("Settings cache JSON encode failed: %s", exc)
        return False
