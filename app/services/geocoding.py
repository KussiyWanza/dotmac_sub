from __future__ import annotations

import ipaddress
import logging
import threading
import time
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain

logger = logging.getLogger(__name__)
_LAST_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT: dict[str, float] = {}


def _secret_setting_value(db: Session, key: str) -> str | None:
    """A geocoding provider credential, read through the resolver that decrypts it.

    `_setting_value` reads `DomainSetting.value_text` straight off the row, and
    a secret setting's column now holds `enc:<key_id>:<token>`. Only the kernel
    resolver decrypts, so a direct row read would send the provider a
    ciphertext string and collect a 401 — which is exactly the failure this
    helper was added to fix, one storage format later.

    It was added in #2244 to dereference a `bao://` reference on this path,
    with a note saying the encrypt-at-rest slice would delete it. This is that
    slice, and the dereference is gone: `resolve_secret` remains only as
    transition tolerance for a row the conversion script has not reached, and
    passes plaintext through untouched.
    """

    from app.services.secrets import resolve_secret
    from app.services.settings_spec import resolve_value

    resolved = resolve_value(db, SettingDomain.geocoding, key)
    if not isinstance(resolved, str) or not resolved.strip():
        return None
    try:
        return resolve_secret(resolved)
    except Exception:
        # Never let a secret-store failure escape as a provider error, and
        # never log the reference. The callers below raise "not configured",
        # which is the truthful outcome: no usable credential.
        logger.warning("Could not resolve the %s geocoding credential", key)
        return None


def _setting_value(db: Session, key: str) -> str | None:
    setting = db.scalars(
        select(DomainSetting)
        .where(DomainSetting.domain == SettingDomain.geocoding)
        .where(DomainSetting.key == key)
        .where(DomainSetting.is_active.is_(True))
    ).first()
    if not setting:
        return None
    if setting.value_text is not None:
        return setting.value_text
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _setting_bool(db: Session, key: str, default: bool) -> bool:
    raw = _setting_value(db, key)
    if raw is None:
        return default
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _setting_int(db: Session, key: str, default: int) -> int:
    raw = _setting_value(db, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _is_self_hosted_url(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return False
    return parsed.is_private or parsed.is_loopback


def _throttle_geocoding_request(db: Session, *, provider: str, base_url: str) -> None:
    min_interval_ms = max(_setting_int(db, "min_interval_ms", 0), 0)
    if min_interval_ms <= 0 or _is_self_hosted_url(base_url):
        return
    key = f"{provider}:{base_url.rstrip('/')}"
    min_interval_seconds = min_interval_ms / 1000.0
    with _LAST_REQUEST_LOCK:
        now = time.monotonic()
        last_request_at = _LAST_REQUEST_AT.get(key)
        if last_request_at is not None:
            wait_seconds = min_interval_seconds - (now - last_request_at)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
                now = time.monotonic()
        _LAST_REQUEST_AT[key] = now


def _compose_address(data: dict) -> str | None:
    parts = [
        data.get("address_line1"),
        data.get("address_line2"),
        data.get("city"),
        data.get("region"),
        data.get("postal_code"),
        data.get("country_code"),
    ]
    value = ", ".join([part for part in parts if part])
    return value.strip() if value else None


def _nominatim_search(db: Session, query: str, limit: int) -> list[dict]:
    base_url = _setting_value(db, "base_url") or "https://nominatim.openstreetmap.org"
    user_agent = _setting_value(db, "user_agent") or "dotmac_sm"
    timeout_sec = _setting_int(db, "timeout_sec", 5)
    email = _setting_value(db, "email")
    country_codes = _setting_value(db, "country_codes")
    params: dict[str, Any] = {
        "q": query,
        "format": "json",
        "limit": max(limit, 1),
        "addressdetails": 1,
    }
    if country_codes:
        params["countrycodes"] = ",".join(
            part.strip().lower() for part in country_codes.split(",") if part.strip()
        )
    if email:
        params["email"] = email
    try:
        _throttle_geocoding_request(db, provider="nominatim", base_url=base_url)
        response = httpx.get(
            f"{base_url.rstrip('/')}/search",
            params=params,
            headers={"User-Agent": user_agent},
            timeout=float(timeout_sec),
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Geocoding request failed") from exc
    results = response.json()
    if not isinstance(results, list):
        raise HTTPException(status_code=502, detail="Invalid geocoding response")
    return results


def _nominatim_reverse(db: Session, latitude: float, longitude: float) -> dict | None:
    base_url = _setting_value(db, "base_url") or "https://nominatim.openstreetmap.org"
    user_agent = _setting_value(db, "user_agent") or "dotmac_sm"
    timeout_sec = _setting_int(db, "timeout_sec", 5)
    email = _setting_value(db, "email")
    params: dict[str, Any] = {
        "lat": latitude,
        "lon": longitude,
        "format": "json",
        "addressdetails": 1,
        "zoom": 18,
    }
    if email:
        params["email"] = email
    try:
        _throttle_geocoding_request(db, provider="nominatim", base_url=base_url)
        response = httpx.get(
            f"{base_url.rstrip('/')}/reverse",
            params=params,
            headers={"User-Agent": user_agent},
            timeout=float(timeout_sec),
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Geocoding request failed") from exc
    result = response.json()
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Invalid geocoding response")
    # Nominatim signals "nothing here" with an error payload, not a 404.
    if result.get("error"):
        return None
    return result


def reverse_geocode(db: Session, latitude: float, longitude: float) -> dict | None:
    """Resolve coordinates to the nearest known address (Nominatim only).

    Returns a dict with display_name/latitude/longitude/address, or None when
    geocoding is disabled or nothing is known at that point.
    """
    if not _setting_bool(db, "enabled", True):
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        raise HTTPException(status_code=400, detail="Invalid coordinates")
    result = _nominatim_reverse(db, latitude, longitude)
    if not result:
        return None
    try:
        return {
            "display_name": result.get("display_name"),
            "latitude": float(str(result.get("lat") or "")),
            "longitude": float(str(result.get("lon") or "")),
            "address": result.get("address") or {},
        }
    except (TypeError, ValueError):
        return None


def _google_search(db: Session, query: str, limit: int) -> list[dict]:
    api_key = _secret_setting_value(db, "google_api_key")
    if not api_key:
        raise HTTPException(
            status_code=400, detail="Google geocoding key is not configured"
        )
    timeout_sec = _setting_int(db, "timeout_sec", 5)
    base_url = "https://maps.googleapis.com"
    try:
        _throttle_geocoding_request(db, provider="google", base_url=base_url)
        response = httpx.get(
            f"{base_url}/maps/api/geocode/json",
            params={"address": query, "key": api_key},
            timeout=float(timeout_sec),
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Geocoding request failed") from exc
    body = response.json()
    if body.get("status") not in {"OK", "ZERO_RESULTS"}:
        raise HTTPException(status_code=502, detail="Invalid geocoding response")
    results = body.get("results") or []
    if not isinstance(results, list):
        raise HTTPException(status_code=502, detail="Invalid geocoding response")
    normalized: list[dict] = []
    for item in results[: max(1, limit)]:
        geometry = item.get("geometry") or {}
        location = geometry.get("location") or {}
        lat = location.get("lat")
        lon = location.get("lng")
        if lat is None or lon is None:
            continue
        normalized.append(
            {
                "display_name": item.get("formatted_address"),
                "lat": lat,
                "lon": lon,
                "class": "google",
                "type": "geocode",
                "importance": 1.0,
            }
        )
    return normalized


def _mapbox_search(db: Session, query: str, limit: int) -> list[dict]:
    token = _secret_setting_value(db, "mapbox_api_key")
    if not token:
        raise HTTPException(
            status_code=400, detail="Mapbox geocoding token is not configured"
        )
    timeout_sec = _setting_int(db, "timeout_sec", 5)
    base_url = "https://api.mapbox.com"
    try:
        _throttle_geocoding_request(db, provider="mapbox", base_url=base_url)
        response = httpx.get(
            f"{base_url}/geocoding/v5/mapbox.places/{quote(query)}.json",
            params={"access_token": token, "limit": max(limit, 1)},
            timeout=float(timeout_sec),
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Geocoding request failed") from exc
    body = response.json()
    features = body.get("features") or []
    if not isinstance(features, list):
        raise HTTPException(status_code=502, detail="Invalid geocoding response")
    normalized: list[dict] = []
    for item in features[: max(1, limit)]:
        center = item.get("center") or []
        if not isinstance(center, list) or len(center) < 2:
            continue
        normalized.append(
            {
                "display_name": item.get("place_name"),
                "lat": center[1],
                "lon": center[0],
                "class": "mapbox",
                "type": item.get("place_type", ["geocode"])[0]
                if isinstance(item.get("place_type"), list) and item.get("place_type")
                else "geocode",
                "importance": item.get("relevance"),
            }
        )
    return normalized


def _provider_search(db: Session, query: str, limit: int) -> list[dict]:
    provider = (_setting_value(db, "provider") or "nominatim").strip().lower()
    if provider == "google":
        return _google_search(db, query, limit)
    if provider == "mapbox":
        return _mapbox_search(db, query, limit)
    return _nominatim_search(db, query, limit)


def geocode_address(db: Session, data: dict) -> dict:
    if data.get("latitude") is not None and data.get("longitude") is not None:
        return data
    if not _setting_bool(db, "enabled", True):
        return data
    address = _compose_address(data)
    if not address:
        return data
    results = _provider_search(db, address, 1)
    if not results:
        return data
    first = results[0]
    try:
        data["latitude"] = float(str(first.get("lat") or ""))
        data["longitude"] = float(str(first.get("lon") or ""))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail="Invalid geocoding response"
        ) from exc
    return data


def geocode_preview(db: Session, data: dict, limit: int = 3) -> list[dict]:
    if not _setting_bool(db, "enabled", True):
        return []
    address = _compose_address(data)
    if not address:
        raise HTTPException(status_code=400, detail="Address fields required")
    results = _provider_search(db, address, limit)
    preview: list[dict] = []
    for item in results:
        try:
            preview.append(
                {
                    "display_name": item.get("display_name"),
                    "latitude": float(str(item.get("lat") or "")),
                    "longitude": float(str(item.get("lon") or "")),
                    "class": item.get("class"),
                    "type": item.get("type"),
                    "importance": item.get("importance"),
                }
            )
        except (TypeError, ValueError):
            continue
    return preview


def geocode_preview_from_request(db: Session, payload) -> list[dict]:
    data = payload.model_dump(exclude={"limit"})
    return geocode_preview(db, data, limit=payload.limit or 3)
