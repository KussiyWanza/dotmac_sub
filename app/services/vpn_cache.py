"""Redis caching layer for VPN configurations.

Provides caching for:
- Server configurations
- Peer configurations
- MikroTik scripts

Caching reduces database load and speeds up configuration downloads.

Uses the centralized Redis client with circuit breaker protection to prevent
retry storms during Redis outages.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar, cast

from app.models.domain_settings import SettingDomain
from app.services.redis_client import get_redis
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

# Configuration
CACHE_PREFIX = "wg:"
_DEFAULT_TTL = 900  # 15 minutes fallback


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _get_default_ttl() -> int:
    """Get the default VPN cache TTL from settings."""
    # In many call sites (and unit tests) we don't have a DB session available.
    # Settings resolution can require DB access, so keep this fail-safe.
    try:
        ttl = resolve_value(
            None, SettingDomain.network, "vpn_cache_default_ttl_seconds"
        )
    except Exception:
        return _DEFAULT_TTL
    if ttl is None:
        return _DEFAULT_TTL
    parsed = _coerce_int(ttl)
    return parsed if parsed is not None else _DEFAULT_TTL


# Cache TTL functions - these allow runtime configuration
def _get_server_config_ttl() -> int:
    return _get_default_ttl()


def _get_peer_config_ttl() -> int:
    return _get_default_ttl()


def _get_mikrotik_script_ttl() -> int:
    return _get_default_ttl()


def get_redis_client() -> Any | None:
    """Get Redis client for VPN caching.

    Uses the centralized Redis client with circuit breaker protection.
    Returns None if Redis is unavailable.
    """
    return get_redis()


def is_cache_available() -> bool:
    """Check if caching is available."""
    client = get_redis_client()
    if client is None:
        return False
    try:
        client.ping()
        return True
    except Exception:
        return False


def _make_key(prefix: str, *args: Any) -> str:
    """Generate a cache key from prefix and arguments."""
    parts = [str(arg) for arg in args if arg is not None]
    key_data = ":".join(parts)
    return f"{CACHE_PREFIX}{prefix}:{key_data}"


def get_cached(key: str) -> str | None:
    """Get a value from cache.

    Returns None if not found or cache unavailable.
    """
    client = get_redis_client()
    if client is None:
        return None

    try:
        return cast(str | None, client.get(key))
    except Exception:
        return None


def set_cached(key: str, value: str, ttl: int | None = None) -> bool:
    """Set a value in cache with TTL.

    Returns True if successful, False otherwise.
    """
    client = get_redis_client()
    if client is None:
        return False

    # Use configurable default if not provided
    if ttl is None:
        ttl = _get_default_ttl()

    try:
        client.setex(key, ttl, value)
        return True
    except Exception:
        return False


def delete_cached(key: str) -> bool:
    """Delete a value from cache.

    Returns True if deleted, False otherwise.
    """
    client = get_redis_client()
    if client is None:
        return False

    try:
        client.delete(key)
        return True
    except Exception:
        return False


def delete_pattern(pattern: str) -> int:
    """Delete all keys matching a pattern.

    Returns count of deleted keys.
    """
    client = get_redis_client()
    if client is None:
        return 0

    try:
        keys = list(client.scan_iter(f"{CACHE_PREFIX}{pattern}*"))
        if keys:
            return cast(int, client.delete(*keys))
        return 0
    except Exception:
        return 0


# ============== Server Cache ==============


def get_server_config(server_id: str) -> str | None:
    """Get cached server configuration."""
    key = _make_key("server_config", server_id)
    return get_cached(key)


def set_server_config(server_id: str, config: str) -> bool:
    """Cache server configuration."""
    key = _make_key("server_config", server_id)
    return set_cached(key, config, _get_server_config_ttl())


def invalidate_server(server_id: str) -> int:
    """Invalidate all cache entries for a server."""
    # Delete server config and all peer configs for this server
    count = 0
    if delete_cached(_make_key("server_config", server_id)):
        count += 1
    count += delete_pattern(f"peer_config:{server_id}:")
    count += delete_pattern(f"mikrotik_script:{server_id}:")
    return count


# ============== Peer Cache ==============


def get_peer_config(peer_id: str) -> str | None:
    """Get cached peer configuration."""
    key = _make_key("peer_config", peer_id)
    return get_cached(key)


def set_peer_config(peer_id: str, config: str, server_id: str) -> bool:
    """Cache peer configuration.

    The server_id is included in a secondary key for invalidation.
    """
    key = _make_key("peer_config", peer_id)
    # Also set a reverse lookup key
    reverse_key = _make_key("peer_config", server_id, peer_id)
    ttl = _get_peer_config_ttl()
    success = set_cached(key, config, ttl)
    if success:
        set_cached(reverse_key, peer_id, ttl)
    return success


def invalidate_peer(peer_id: str) -> bool:
    """Invalidate cache entries for a peer."""
    key = _make_key("peer_config", peer_id)
    script_key = _make_key("mikrotik_script", peer_id)
    delete_cached(key)
    delete_cached(script_key)
    return True


# ============== MikroTik Script Cache ==============


def get_mikrotik_script(peer_id: str) -> str | None:
    """Get cached MikroTik script."""
    key = _make_key("mikrotik_script", peer_id)
    return get_cached(key)


def set_mikrotik_script(peer_id: str, script: str, server_id: str) -> bool:
    """Cache MikroTik script."""
    key = _make_key("mikrotik_script", peer_id)
    return set_cached(key, script, _get_mikrotik_script_ttl())


# ============== Decorator for Caching ==============

T = TypeVar("T")


def cached(
    key_prefix: str,
    key_args: tuple[str, ...] = (),
    ttl: int | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for caching function results.

    Usage:
        @cached("my_function", ("arg1", "arg2"), ttl=300)
        def my_function(arg1: str, arg2: str) -> str:
            ...

    The function result must be JSON-serializable.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            # Build cache key from specified arguments
            key_parts = []
            for key_name in key_args:
                if key_name in kwargs:
                    key_parts.append(str(kwargs[key_name]))
                else:
                    # Try to get from positional args
                    import inspect

                    sig = inspect.signature(func)
                    params = list(sig.parameters.keys())
                    if key_name in params:
                        idx = params.index(key_name)
                        if idx < len(args):
                            key_parts.append(str(args[idx]))

            cache_key = _make_key(key_prefix, *key_parts)

            # Try to get from cache
            cached_value = get_cached(cache_key)
            if cached_value is not None:
                try:
                    return cast(T, json.loads(cached_value))
                except json.JSONDecodeError:
                    return cached_value  # type: ignore[return-value]

            # Call function and cache result
            result = func(*args, **kwargs)

            try:
                cached_str = json.dumps(result)
            except (TypeError, ValueError):
                cached_str = str(result)

            # Use configurable default if not provided
            effective_ttl = ttl if ttl is not None else _get_default_ttl()
            set_cached(cache_key, cached_str, effective_ttl)
            return result

        return wrapper

    return decorator


# ============== Cache Stats ==============


def get_cache_stats() -> dict[str, Any]:
    """Get cache statistics.

    Returns empty dict if cache unavailable.
    """
    client = get_redis_client()
    if client is None:
        return {"available": False}

    try:
        info = cast(dict[str, Any], client.info("memory"))
        keys = list(client.scan_iter(f"{CACHE_PREFIX}*"))

        return {
            "available": True,
            "total_keys": len(keys),
            "memory_used": info.get("used_memory_human", "unknown"),
            "server_configs": len([k for k in keys if "server_config" in k]),
            "peer_configs": len([k for k in keys if "peer_config" in k]),
            "mikrotik_scripts": len([k for k in keys if "mikrotik_script" in k]),
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


def flush_all_vpn_cache() -> int:
    """Flush all VPN-related cache entries.

    Returns count of deleted keys.
    """
    return delete_pattern("")
