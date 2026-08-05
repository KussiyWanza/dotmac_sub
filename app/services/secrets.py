"""Centralized secrets management via OpenBao (Vault-compatible).

All secret resolution goes through this module. Supports:
- ``bao://mount/path#field`` references (resolved from OpenBao KV v2)
- ``openbao://`` and ``vault://`` as aliases
- Plaintext passthrough (for local dev without OpenBao)
- ``get_secret(path, field)`` convenience helper for direct lookups
"""

import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import NewType
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Secrets are cached per-process to avoid repeated HTTP calls. A time bucket in
# the cache key expires entries even when rotation happens in another process.
_CACHE_SIZE = 128
_DEFAULT_CACHE_TTL_SECONDS = 60
_MAX_SECRET_PATH_DEPTH = 16
_MAX_SECRET_PATHS = 2048

SecretPath = NewType("SecretPath", str)


def _read_openbao_token() -> str | None:
    token_file = os.getenv("OPENBAO_TOKEN_FILE") or os.getenv("VAULT_TOKEN_FILE")
    if token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("OpenBao token file is unreadable: %s", token_file)
        else:
            if token:
                return token
    return os.getenv("OPENBAO_TOKEN") or os.getenv("VAULT_TOKEN")


def _cache_bucket() -> int:
    raw_ttl = os.getenv("OPENBAO_CACHE_TTL_SECONDS", "")
    try:
        ttl = int(raw_ttl) if raw_ttl else _DEFAULT_CACHE_TTL_SECONDS
    except ValueError:
        ttl = _DEFAULT_CACHE_TTL_SECONDS
    ttl = max(1, min(ttl, 3600))
    return int(time.monotonic() // ttl)


def is_secret_ref(value: str | None) -> bool:
    """Check if a value is a secret URI reference (OpenBao or env)."""
    if not value:
        return False
    return value.startswith(("bao://", "openbao://", "vault://", "env://"))


def is_openbao_ref(value: str | None) -> bool:
    """Check if a value is an OpenBao URI reference."""
    if not value:
        return False
    return value.startswith(("bao://", "openbao://", "vault://"))


def _openbao_config() -> tuple[str, str, str | None, str]:
    """Return (addr, token, namespace, kv_version) from env."""
    addr = os.getenv("OPENBAO_ADDR") or os.getenv("VAULT_ADDR")
    token = _read_openbao_token()
    namespace = os.getenv("OPENBAO_NAMESPACE") or os.getenv("VAULT_NAMESPACE")
    kv_version = os.getenv("OPENBAO_KV_VERSION", "2")
    if not addr:
        raise HTTPException(status_code=500, detail="OpenBao address not configured")
    if not token:
        raise HTTPException(status_code=500, detail="OpenBao token not configured")
    return addr.rstrip("/"), token, namespace, kv_version


def is_openbao_available() -> bool:
    """Check if OpenBao is configured and reachable (non-throwing)."""
    try:
        addr = os.getenv("OPENBAO_ADDR") or os.getenv("VAULT_ADDR")
        token = _read_openbao_token()
        if not addr or not token:
            return False
        resp = httpx.get(
            f"{addr.rstrip('/')}/v1/sys/health",
            headers={"X-Vault-Token": token},
            timeout=3.0,
        )
        return resp.status_code in (200, 429, 472, 473)
    except Exception:
        return False


def _parse_ref(reference: str) -> tuple[str, str, str]:
    """Parse ``bao://mount/path#field`` into (mount, path, field)."""
    parsed = urlparse(reference)
    mount = parsed.netloc
    path = parsed.path.lstrip("/")
    field = parsed.fragment or "value"
    if not mount or not path:
        raise HTTPException(status_code=500, detail="Invalid OpenBao reference")
    return mount, path, field


@lru_cache(maxsize=_CACHE_SIZE)
def _fetch_secret_data(
    url: str,
    token: str,
    namespace: str | None,
    http_get_identity: int,
    cache_bucket: int,
) -> dict:
    """Fetch and cache a secret payload from OpenBao."""
    headers: dict[str, str] = {"X-Vault-Token": token}
    if namespace:
        headers["X-Vault-Namespace"] = namespace
    try:
        response = httpx.get(url, headers=headers, timeout=5.0)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 404:
            logger.debug("OpenBao secret missing at %s", url)
            raise HTTPException(
                status_code=404, detail="OpenBao secret not found"
            ) from exc
        logger.error("OpenBao request failed for %s: %s", url, exc)
        raise HTTPException(status_code=500, detail="OpenBao request failed") from exc
    except httpx.HTTPError as exc:
        logger.error("OpenBao request failed for %s: %s", url, exc)
        raise HTTPException(status_code=500, detail="OpenBao request failed") from exc
    payload = response.json()
    return payload.get("data", {})


def resolve_openbao_ref(reference: str) -> str:
    """Resolve a ``bao://mount/path#field`` reference to its secret value."""
    addr, token, namespace, kv_version = _openbao_config()
    mount, path, field = _parse_ref(reference)
    if str(kv_version) == "1":
        url = f"{addr}/v1/{mount}/{path}"
    else:
        url = f"{addr}/v1/{mount}/data/{path}"
    raw_data = _fetch_secret_data(
        url,
        token,
        namespace,
        id(httpx.get),
        _cache_bucket(),
    )
    if str(kv_version) == "1":
        secret_data = raw_data
    else:
        secret_data = raw_data.get("data", {})
    if field not in secret_data:
        raise HTTPException(
            status_code=500,
            detail=f"OpenBao secret field '{field}' not found at {mount}/{path}",
        )
    return str(secret_data[field])


def _resolve_env_ref(reference: str) -> str | None:
    """Resolve an ``env://VARIABLE_NAME`` reference to its environment value."""
    var_name = reference[6:]  # strip "env://"
    if not var_name:
        return None
    return os.environ.get(var_name)


def resolve_secret(value: str | None) -> str | None:
    """Resolve a value that may be a secret URI reference or plaintext.

    Supported schemes:
    - ``bao://``, ``openbao://``, ``vault://`` — resolved from OpenBao
    - ``env://VARIABLE_NAME`` — resolved from environment variable
    - Anything else — returned as-is
    """
    if not value:
        return value
    if value.startswith("env://"):
        return _resolve_env_ref(value)
    if is_openbao_ref(value):
        return resolve_openbao_ref(value)
    return value


def get_secret(path: str, field: str, *, default: str = "") -> str:
    """Convenience: fetch a secret directly from OpenBao KV v2.

    Args:
        path: Secret path (e.g. ``paystack``, ``auth``, ``database``).
        field: Field name within the secret (e.g. ``secret_key``).
        default: Fallback value if OpenBao is unavailable.

    Returns:
        The secret value, or default if resolution fails.
    """
    try:
        return resolve_openbao_ref(f"bao://secret/{path}#{field}")
    except Exception:
        logger.debug(
            "OpenBao lookup failed for secret/%s#%s, using default", path, field
        )
        return default


def get_env_or_secret(
    env_var: str,
    bao_path: str,
    bao_field: str,
    *,
    default: str = "",
) -> str:
    """Resolve a secret with fallback chain: OpenBao → env var → default.

    This is the recommended function for all secret resolution in the app.
    """
    # Try OpenBao first
    try:
        val = get_secret(bao_path, bao_field)
        if val:
            return val
    except Exception:
        logger.debug(
            "OpenBao secret lookup failed for %s#%s",
            bao_path,
            bao_field,
            exc_info=True,
        )
    # Fall back to env var
    env_val = os.getenv(env_var, "")
    if env_val:
        return env_val
    return default


def clear_cache() -> None:
    """Clear the in-process secret cache (e.g., after rotation)."""
    _fetch_secret_data.cache_clear()


# ── OpenBao KV management (for admin UI) ────────────────────────────


def _metadata_list_url(addr: str, prefix: str) -> str:
    normalized_prefix = prefix.strip("/")
    suffix = f"/{normalized_prefix}" if normalized_prefix else "/"
    return f"{addr}/v1/secret/metadata{suffix}?list=true"


def _normalized_metadata_child(prefix: str, key: object) -> tuple[str, bool] | None:
    if not isinstance(key, str):
        return None
    is_directory = key.endswith("/")
    name = key.rstrip("/")
    if not name or "/" in name or name in {".", ".."}:
        return None
    child = f"{prefix}/{name}" if prefix else name
    return child, is_directory


def list_secret_paths() -> tuple[SecretPath, ...]:
    """List leaf secret paths under the ``secret/`` mount.

    OpenBao KV v2 returns folders with a trailing slash and requires another
    LIST request for their contents. The admin inventory needs the leaf paths
    so a nested secret such as ``integrations/meta_social`` remains editable.
    Subtrees the application token cannot list are omitted without converting
    an otherwise valid inventory into an unavailable state.
    """
    try:
        addr, token, namespace, _kv_version = _openbao_config()
        headers: dict[str, str] = {"X-Vault-Token": token}
        if namespace:
            headers["X-Vault-Namespace"] = namespace

        pending: list[tuple[str, int]] = [("", 0)]
        visited: set[str] = set()
        leaves: set[SecretPath] = set()
        while (
            pending
            and len(visited) < _MAX_SECRET_PATHS
            and len(leaves) < _MAX_SECRET_PATHS
        ):
            prefix, depth = pending.pop()
            if prefix in visited:
                continue
            visited.add(prefix)
            response = httpx.get(
                _metadata_list_url(addr, prefix),
                headers=headers,
                timeout=5.0,
            )
            if response.status_code in (403, 404):
                if prefix:
                    logger.debug("OpenBao metadata subtree is not listable: %s", prefix)
                    continue
                return ()
            response.raise_for_status()
            raw_keys = response.json().get("data", {}).get("keys", [])
            if not isinstance(raw_keys, list):
                logger.warning(
                    "OpenBao metadata LIST returned invalid keys for %s", prefix
                )
                continue
            for raw_key in raw_keys:
                normalized = _normalized_metadata_child(prefix, raw_key)
                if normalized is None:
                    logger.warning(
                        "OpenBao metadata LIST returned an invalid child key"
                    )
                    continue
                child, is_directory = normalized
                if is_directory:
                    if depth >= _MAX_SECRET_PATH_DEPTH:
                        logger.warning(
                            "OpenBao metadata path depth limit reached at %s", child
                        )
                        continue
                    pending.append((child, depth + 1))
                else:
                    leaves.add(SecretPath(child))
                if len(leaves) >= _MAX_SECRET_PATHS:
                    logger.warning("OpenBao metadata path limit reached")
                    break
        if pending:
            logger.warning("OpenBao metadata inventory limit reached")
        return tuple(sorted(leaves, key=str))
    except Exception as exc:
        logger.warning("Failed to list OpenBao paths: %s", exc)
        return ()


def read_secret_metadata(path: str) -> dict:
    """Read metadata (version, dates) for a secret path."""
    try:
        addr, token, namespace, _kv = _openbao_config()
        url = f"{addr}/v1/secret/metadata/{path}"
        headers: dict[str, str] = {"X-Vault-Token": token}
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        resp = httpx.get(url, headers=headers, timeout=5.0)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json().get("data", {})
    except Exception as exc:
        logger.warning("Failed to read OpenBao metadata for %s: %s", path, exc)
        return {}


def read_secret_fields(path: str) -> dict[str, str]:
    """Read secret values for internal update and resolution workflows."""
    try:
        addr, token, namespace, kv_version = _openbao_config()
        url = f"{addr}/v1/secret/data/{path}"
        headers: dict[str, str] = {"X-Vault-Token": token}
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        resp = httpx.get(url, headers=headers, timeout=5.0)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("data", {})
        return {k: str(v) for k, v in data.items()}
    except Exception as exc:
        logger.warning("Failed to read OpenBao fields for %s: %s", path, exc)
        return {}


def list_secret_field_names(path: str) -> list[str]:
    """Return field names without exposing values to callers."""
    return sorted(read_secret_fields(path).keys())


def write_secret(path: str, data: dict[str, str]) -> bool:
    """Write/update a secret at the given path."""
    try:
        addr, token, namespace, _kv = _openbao_config()
        url = f"{addr}/v1/secret/data/{path}"
        headers: dict[str, str] = {
            "X-Vault-Token": token,
            "Content-Type": "application/json",
        }
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        resp = httpx.post(url, json={"data": data}, headers=headers, timeout=5.0)
        resp.raise_for_status()
        clear_cache()
        return True
    except Exception as exc:
        logger.error("Failed to write OpenBao secret %s: %s", path, exc)
        return False


def delete_secret(path: str) -> bool:
    """Delete a secret at the given path (metadata delete)."""
    try:
        addr, token, namespace, _kv = _openbao_config()
        url = f"{addr}/v1/secret/metadata/{path}"
        headers: dict[str, str] = {"X-Vault-Token": token}
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        resp = httpx.delete(url, headers=headers, timeout=5.0)
        resp.raise_for_status()
        clear_cache()
        return True
    except Exception as exc:
        logger.error("Failed to delete OpenBao secret %s: %s", path, exc)
        return False
