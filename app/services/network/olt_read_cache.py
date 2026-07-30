"""Redis-based caching layer for OLT read operations.

Caches frequently accessed OLT data to reduce device round trips:
- Service ports: 60s TTL (changes infrequently)
- OLT profiles: 120s TTL (rarely changes)
- Running config: 300s TTL (manual changes only)

Usage:
    from app.services.network.olt_read_cache import olt_cache

    # Manual cache operations
    olt_cache.set_service_ports(olt_id, fsp, ports, ttl=60)
    ports = olt_cache.get_service_ports(olt_id, fsp)

    # Invalidate on write
    olt_cache.invalidate(olt_id, "service_ports")
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from uuid import UUID

    from redis import Redis

logger = logging.getLogger(__name__)

# Type for decorated functions
F = TypeVar("F", bound=Callable[..., Any])

# Default TTLs for different data types (seconds)
DEFAULT_TTLS = {
    "service_ports": 60,
    "profiles": 120,
    "running_config": 300,
    "ont_info": 30,
    "health": 15,
}


class OltReadCache:
    """Redis-backed cache for OLT read operations.

    Provides caching for expensive device operations with automatic
    serialization, TTL management, and cache invalidation.

    Thread Safety:
        Redis operations are atomic. Multiple processes can share the cache.

    Cache Key Format:
        olt:<olt_id>:<operation>:<params_hash>

    Example:
        olt:550e8400-e29b-41d4-a716-446655440000:service_ports:0/2/1
    """

    def __init__(self, redis_url: str | None = None):
        """Initialize cache with Redis connection.

        Args:
            redis_url: Redis connection URL. If None, uses REDIS_URL from config.
        """
        self._redis: Redis | None = None
        self._redis_url = redis_url
        self._enabled = True
        self._stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "invalidations": 0,
            "errors": 0,
        }

    @property
    def redis(self):
        """Lazy initialization of Redis client."""
        if self._redis is None:
            try:
                import redis

                if self._redis_url is None:
                    from app.config import settings

                    self._redis_url = settings.REDIS_URL

                self._redis = redis.from_url(
                    self._redis_url,
                    decode_responses=True,
                    socket_timeout=2.0,
                    socket_connect_timeout=2.0,
                )
                # Test connection
                self._redis.ping()
            except Exception as e:
                logger.warning("Redis cache not available: %s", e)
                self._enabled = False
                return None

        return self._redis

    def _cache_key(self, olt_id: str, operation: str, params: str = "") -> str:
        """Build cache key for an operation."""
        if params:
            # Hash long params to keep keys short
            if len(params) > 50:
                params = hashlib.md5(
                    params.encode(), usedforsecurity=False
                ).hexdigest()[:12]
            return f"olt:{olt_id}:{operation}:{params}"
        return f"olt:{olt_id}:{operation}"

    def get(
        self,
        olt_id: str | UUID,
        operation: str,
        params: str = "",
    ) -> Any | None:
        """Get cached value for an operation.

        Args:
            olt_id: OLT device UUID.
            operation: Operation name (e.g., "autofind", "service_ports").
            params: Additional parameters (e.g., FSP for service ports).

        Returns:
            Cached value or None if not found/expired.
        """
        if not self._enabled:
            return None

        try:
            redis = self.redis
            if redis is None:
                return None

            key = self._cache_key(str(olt_id), operation, params)
            data = redis.get(key)

            if data is None:
                self._stats["misses"] += 1
                return None

            self._stats["hits"] += 1
            result = json.loads(data)
            logger.debug("Cache hit: %s", key)
            return result.get("value")

        except Exception as e:
            self._stats["errors"] += 1
            logger.warning("Cache get error for %s/%s: %s", olt_id, operation, e)
            return None

    def set(
        self,
        olt_id: str | UUID,
        operation: str,
        value: Any,
        params: str = "",
        ttl: int | None = None,
    ) -> bool:
        """Cache a value for an operation.

        Args:
            olt_id: OLT device UUID.
            operation: Operation name.
            value: Value to cache (must be JSON-serializable).
            params: Additional parameters.
            ttl: Time-to-live in seconds (uses default for operation if None).

        Returns:
            True if cached successfully, False otherwise.
        """
        if not self._enabled:
            return False

        try:
            redis = self.redis
            if redis is None:
                return False

            key = self._cache_key(str(olt_id), operation, params)
            ttl = ttl or DEFAULT_TTLS.get(operation, 60)

            data = json.dumps(
                {
                    "value": value,
                    "cached_at": datetime.now(UTC).isoformat(),
                    "operation": operation,
                }
            )

            redis.setex(key, ttl, data)
            self._stats["sets"] += 1
            logger.debug("Cache set: %s (TTL=%ds)", key, ttl)
            return True

        except Exception as e:
            self._stats["errors"] += 1
            logger.warning("Cache set error for %s/%s: %s", olt_id, operation, e)
            return False

    def invalidate(
        self,
        olt_id: str | UUID,
        operation: str | None = None,
    ) -> int:
        """Invalidate cached data for an OLT.

        Args:
            olt_id: OLT device UUID.
            operation: Specific operation to invalidate, or None for all.

        Returns:
            Number of keys deleted.
        """
        if not self._enabled:
            return 0

        try:
            redis = self.redis
            if redis is None:
                return 0

            if operation:
                # Delete specific operation
                pattern = f"olt:{olt_id}:{operation}*"
            else:
                # Delete all for OLT
                pattern = f"olt:{olt_id}:*"

            deleted = 0
            cursor = 0
            while True:
                cursor, keys = redis.scan(cursor, match=pattern, count=100)
                if keys:
                    deleted += redis.delete(*keys)
                if cursor == 0:
                    break

            self._stats["invalidations"] += deleted
            if deleted > 0:
                logger.debug("Cache invalidated %d keys for %s", deleted, pattern)
            return deleted

        except Exception as e:
            self._stats["errors"] += 1
            logger.warning("Cache invalidate error for %s: %s", olt_id, e)
            return 0

    # Convenience methods for common operations

    def get_service_ports(self, olt_id: str, fsp: str) -> list[dict] | None:
        """Get cached service ports for a PON port."""
        return self.get(olt_id, "service_ports", fsp)

    def set_service_ports(
        self, olt_id: str, fsp: str, ports: list[dict], ttl: int = 60
    ) -> bool:
        """Cache service ports for a PON port."""
        return self.set(olt_id, "service_ports", ports, fsp, ttl)

    def get_profiles(self, olt_id: str, profile_type: str) -> list[dict] | None:
        """Get cached OLT profiles."""
        return self.get(olt_id, "profiles", profile_type)

    def set_profiles(
        self, olt_id: str, profile_type: str, profiles: list[dict], ttl: int = 120
    ) -> bool:
        """Cache OLT profiles."""
        return self.set(olt_id, "profiles", profiles, profile_type, ttl)

    def cached(
        self,
        operation: str,
        ttl: int | None = None,
        param_extractor: Callable[..., str] | None = None,
    ) -> Callable[[F], F]:
        """Decorator for caching function results.

        The decorated function must have olt_id or olt as first positional arg.

        Args:
            operation: Cache operation name.
            ttl: Cache TTL in seconds.
            param_extractor: Function to extract cache params from args/kwargs.

        Example:
            @olt_cache.cached("service_ports", ttl=60, param_extractor=lambda olt, fsp: fsp)
            def get_service_ports(olt: OLTDevice, fsp: str) -> list[dict]:
                # ... expensive operation ...
        """

        def decorator(func: F) -> F:
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                # Extract OLT ID from first argument
                first_arg = args[0] if args else None
                olt_id = None

                if first_arg is not None and hasattr(first_arg, "id"):
                    olt_id = str(first_arg.id)
                elif isinstance(first_arg, str):
                    olt_id = first_arg

                if not olt_id:
                    # Can't cache without OLT ID
                    return func(*args, **kwargs)

                # Extract params
                params = ""
                if param_extractor:
                    try:
                        params = param_extractor(*args, **kwargs)
                    except Exception:
                        pass

                # Try cache first
                cached_value = self.get(olt_id, operation, params)
                if cached_value is not None:
                    return cached_value

                # Call function and cache result
                result = func(*args, **kwargs)

                if result is not None:
                    self.set(olt_id, operation, result, params, ttl)

                return result

            return wrapper  # type: ignore[return-value]

        return decorator

    def get_stats(self) -> dict:
        """Get cache statistics."""
        hit_rate = 0.0
        total = self._stats["hits"] + self._stats["misses"]
        if total > 0:
            hit_rate = self._stats["hits"] / total * 100

        return {
            **self._stats,
            "hit_rate_percent": round(hit_rate, 1),
            "enabled": self._enabled,
        }


# Global cache instance
olt_cache = OltReadCache()
