"""Enforcement helpers for sessions, throttling, and blocking."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pyrad.client import Client, Timeout
from pyrad.dictionary import Dictionary
from pyrad.packet import CoARequest, DisconnectACK, DisconnectNAK, DisconnectRequest
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.catalog import (
    AccessCredential,
    NasDevice,
    NasVendor,
    OfferRadiusProfile,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.models.radius import RadiusClient
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services import control_registry, settings_spec
from app.services.common import coerce_uuid
from app.services.credential_crypto import decrypt_credential
from app.services.nas import DeviceProvisioner
from app.services.radius_address_lists import suspended_address_list
from app.services.secrets import resolve_secret

logger = logging.getLogger(__name__)

# Characters that could break RouterOS CLI quoting or inject commands
_ROUTEROS_UNSAFE_RE = re.compile(r'[";\\{}\n\r]')


class CoADisconnectDisposition(StrEnum):
    """Transport-neutral evidence from one RADIUS Disconnect request."""

    disconnected = "disconnected"
    session_not_found = "session_not_found"
    disabled = "disabled"
    temporarily_suppressed = "temporarily_suppressed"
    missing_nas_host = "missing_nas_host"
    missing_dictionary = "missing_dictionary"
    invalid_dictionary = "invalid_dictionary"
    missing_secret = "missing_secret"
    rejected = "rejected"
    timeout = "timeout"
    transport_error = "transport_error"
    unexpected_response = "unexpected_response"


@dataclass(frozen=True)
class CoADisconnectOutcome:
    disposition: CoADisconnectDisposition

    @property
    def disconnected(self) -> bool:
        return self.disposition is CoADisconnectDisposition.disconnected

    @property
    def session_absent(self) -> bool:
        return self.disposition is CoADisconnectDisposition.session_not_found

    @property
    def terminal(self) -> bool:
        return self.disconnected or self.session_absent


def _disconnect_error_cause(response: object) -> int | None:
    """Return RFC 5176 Error-Cause without exposing response payloads."""
    try:
        values = response["Error-Cause"]  # type: ignore[index]
        raw = values[0] if isinstance(values, (list, tuple)) else values
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="ignore")
        return int(raw)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _sanitize_routeros_value(value: str) -> str:
    """Remove characters that could break RouterOS CLI quoting."""
    return _ROUTEROS_UNSAFE_RE.sub("", value)


def _setting_bool(db: Session, domain: SettingDomain, key: str, default: bool) -> bool:
    value = settings_spec.resolve_value(db, domain, key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _radius_dictionary_path(db: Session) -> str | None:
    value = settings_spec.resolve_value(db, SettingDomain.radius, "coa_dictionary_path")
    if value is None:
        value = settings_spec.resolve_value(
            db, SettingDomain.radius, "auth_dictionary_path"
        )
    return None if value is None else str(value)


def _radius_timeout_sec(db: Session) -> float:
    timeout = settings_spec.resolve_value(db, SettingDomain.radius, "coa_timeout_sec")
    try:
        return float(str(timeout)) if timeout is not None else 3.0
    except (TypeError, ValueError):
        return 3.0


def _coa_enabled(db: Session) -> bool:
    return control_registry.is_enabled(db, "access.radius_coa")


def _coa_retries(db: Session) -> int:
    retries = settings_spec.resolve_value(db, SettingDomain.radius, "coa_retries")
    try:
        return int(str(retries)) if retries is not None else 1
    except (TypeError, ValueError):
        return 1


# Per-NAS negative cache for CoA support. After a CoA times out we skip
# the attempt for ``_COA_NEG_TTL`` to avoid paying ~6s (timeout * retries)
# per customer on NASes where CoA isn't enabled. Cleared on first
# successful CoA. Reset manually via ``reset_coa_cache()``.
# Fallback default; the live value comes from
# SettingDomain.network/coa_negative_cache_ttl_minutes via _coa_neg_ttl().
_COA_NEG_TTL = timedelta(minutes=15)
_COA_NEG_CACHE: dict[Any, datetime] = {}
_COA_CACHE_LOCK = threading.Lock()


def _coa_neg_ttl() -> timedelta:
    """CoA negative-cache TTL from settings, falling back to the default.
    Best-effort — a settings/DB hiccup must not break enforcement."""
    try:
        from app.db import SessionLocal
        from app.services import settings_spec

        with SessionLocal() as session:
            minutes = settings_spec.resolve_value(
                session, SettingDomain.network, "coa_negative_cache_ttl_minutes"
            )
        if minutes is not None:
            return timedelta(minutes=int(minutes))
    except Exception:
        logger.debug("CoA neg-cache TTL: using default", exc_info=True)
    return _COA_NEG_TTL


def _coa_disabled_for_nas(nas_id: Any) -> bool:
    if nas_id is None:
        return False
    with _COA_CACHE_LOCK:
        expires_at = _COA_NEG_CACHE.get(nas_id)
        if expires_at is None:
            return False
        if expires_at <= datetime.now(UTC):
            _COA_NEG_CACHE.pop(nas_id, None)
            return False
        return True


def _mark_coa_unsupported(nas_id: Any) -> None:
    if nas_id is None:
        return
    with _COA_CACHE_LOCK:
        _COA_NEG_CACHE[nas_id] = datetime.now(UTC) + _coa_neg_ttl()


def _mark_coa_supported(nas_id: Any) -> None:
    if nas_id is None:
        return
    with _COA_CACHE_LOCK:
        _COA_NEG_CACHE.pop(nas_id, None)


def reset_coa_cache(nas_id: Any = None) -> None:
    """Clear the CoA negative cache. Pass a nas_id to clear one entry,
    or call with no args to wipe everything (e.g., after a NAS config
    change re-enables CoA)."""
    with _COA_CACHE_LOCK:
        if nas_id is None:
            _COA_NEG_CACHE.clear()
        else:
            _COA_NEG_CACHE.pop(nas_id, None)


def _mikrotik_kill_enabled(db: Session) -> bool:
    return control_registry.is_enabled(db, "access.mikrotik_session_kill")


def _mikrotik_api_session_kick_enabled(db: Session) -> bool:
    return control_registry.is_enabled(db, "access.mikrotik_api_session_kick")


def _address_list_block_enabled(db: Session) -> bool:
    return control_registry.is_enabled(db, "access.address_list_block")


def _default_address_list(db: Session) -> str | None:
    value = settings_spec.resolve_value(
        db, SettingDomain.network, "default_mikrotik_address_list"
    )
    return None if value is None else str(value)


def _resolve_effective_profile(
    db: Session,
    subscription: Subscription,
    credential: AccessCredential | None = None,
) -> RadiusProfile | None:
    """Resolve the effective RADIUS profile for a subscription.

    Resolution priority:
    1. Credential-level override (if credential provided)
    2. Subscription-level override
    3. Offer's default profile (via OfferRadiusProfile)

    Args:
        db: Database session
        subscription: The subscription to resolve profile for
        credential: Optional credential to check for override

    Returns:
        RadiusProfile or None if no profile is configured
    """
    # 1. Check credential-level override first
    if credential and credential.radius_profile_id:
        profile = db.get(RadiusProfile, credential.radius_profile_id)
        if profile:
            return profile

    # 2. Check subscription-level override
    if subscription.radius_profile_id:
        profile = db.get(RadiusProfile, subscription.radius_profile_id)
        if profile:
            return profile

    # 3. Fall back to offer's default profile
    offer_profile = (
        db.query(OfferRadiusProfile)
        .filter(OfferRadiusProfile.offer_id == subscription.offer_id)
        .first()
    )
    if offer_profile:
        return db.get(RadiusProfile, offer_profile.profile_id)
    return None


def _resolve_nas_device(
    db: Session, session: RadiusAccountingSession
) -> NasDevice | None:
    if session.nas_device_id:
        return db.get(NasDevice, session.nas_device_id)
    if session.radius_client_id:
        client = db.get(RadiusClient, session.radius_client_id)
        if client and client.nas_device_id:
            return db.get(NasDevice, client.nas_device_id)
    return None


def _nas_with_api_creds(db: Session, nas_device: NasDevice) -> NasDevice | None:
    """Return a NAS row with usable RouterOS API creds for this device.

    ``nas_devices`` has duplicate rows per BNG IP and only some carry API
    credentials, so the row resolved from a session may be a credential-less
    duplicate. Prefer the device itself; otherwise a sibling row on the same IP
    that has API creds.
    """
    if nas_device.api_username and nas_device.api_password:
        return nas_device
    ip = nas_device.nas_ip or nas_device.ip_address
    if not ip:
        return None
    return (
        db.query(NasDevice)
        .filter((NasDevice.nas_ip == ip) | (NasDevice.ip_address == ip))
        .filter(NasDevice.api_username.isnot(None))
        .filter(NasDevice.api_password.isnot(None))
        .filter(NasDevice.vendor == NasVendor.mikrotik)
        .first()
    )


# Per-process cache of CoA secrets resolved from the external FreeRADIUS
# ``nas`` table, keyed by NAS device id. Secrets effectively never change
# without a NAS re-sync (which restarts workers on deploy), so no TTL.
_COA_SECRET_CACHE: dict[str, str] = {}
_COA_SECRET_CACHE_LOCK = threading.Lock()


def _resolve_coa_secret(db: Session, nas_device: NasDevice) -> str | None:
    """Resolve the RADIUS shared secret to use for CoA against a NAS.

    Prefers the secret stored on the NAS device record. Falls back to the
    external FreeRADIUS ``nas`` table (matched by the device's RADIUS client
    IP) — that secret is authoritative by construction: FreeRADIUS is
    actively authenticating the NAS with it. Several migrated NAS records
    have no local shared_secret, which used to make CoA structurally
    impossible for their subscribers.
    """
    if nas_device.shared_secret:
        try:
            decrypted = decrypt_credential(nas_device.shared_secret)
            if decrypted:
                resolved = resolve_secret(decrypted)
                if resolved:
                    return resolved
        except Exception as exc:
            # An undecryptable/unresolvable local secret must not abort the
            # enforcement loop — fall through to the external lookup.
            logger.warning(
                "Local CoA secret unusable for NAS %s (%s): %s — trying "
                "external nas table.",
                nas_device.name,
                nas_device.id,
                exc,
            )

    cache_key = str(nas_device.id)
    with _COA_SECRET_CACHE_LOCK:
        cached = _COA_SECRET_CACHE.get(cache_key)
    if cached:
        return cached

    from sqlalchemy import Column, String
    from sqlalchemy import select as sa_select

    from app.services.radius import (
        _active_external_sync_configs,
        _external_radius_table,
        _get_external_engine,
        _radius_client_ip_for_nas,
    )

    client_ip = _radius_client_ip_for_nas(nas_device)
    if not client_ip:
        return None
    for config in _active_external_sync_configs(db):
        try:
            engine = _get_external_engine(config["db_url"])
            nas_table = _external_radius_table(
                config.get("nas_table", "nas"),
                Column("nasname", String),
                Column("secret", String),
            )
            with engine.connect() as conn:
                secret = conn.execute(
                    sa_select(nas_table.c.secret).where(
                        nas_table.c.nasname == client_ip
                    )
                ).scalar()
            if secret:
                with _COA_SECRET_CACHE_LOCK:
                    _COA_SECRET_CACHE[cache_key] = secret
                logger.info(
                    "CoA secret for NAS %s (%s) resolved from external "
                    "RADIUS nas table (no local shared_secret).",
                    nas_device.name,
                    nas_device.id,
                )
                return secret
        except Exception as exc:
            logger.warning(
                "External CoA secret lookup failed for NAS %s (%s): %s",
                nas_device.name,
                nas_device.id,
                exc,
            )
    return None


def _send_coa_disconnect(
    db: Session,
    nas_device: NasDevice,
    username: str | None,
    framed_ip: str | None,
    session_id: str | None,
) -> CoADisconnectOutcome:
    if not _coa_enabled(db):
        return CoADisconnectOutcome(CoADisconnectDisposition.disabled)
    if _coa_disabled_for_nas(nas_device.id):
        logger.debug(
            "Skipping CoA disconnect for NAS %s (negative-cached).", nas_device.id
        )
        return CoADisconnectOutcome(CoADisconnectDisposition.temporarily_suppressed)
    host = nas_device.nas_ip or nas_device.management_ip or nas_device.ip_address
    if not host:
        logger.warning("Missing NAS host for CoA disconnect.")
        return CoADisconnectOutcome(CoADisconnectDisposition.missing_nas_host)
    dict_path = _radius_dictionary_path(db)
    if not dict_path:
        logger.warning("Missing RADIUS dictionary path for CoA disconnect.")
        return CoADisconnectOutcome(CoADisconnectDisposition.missing_dictionary)
    try:
        dictionary = Dictionary(dict_path)
    except Exception as exc:
        logger.warning("Failed to load RADIUS dictionary: %s", exc)
        return CoADisconnectOutcome(CoADisconnectDisposition.invalid_dictionary)
    decrypted_secret = _resolve_coa_secret(db, nas_device)
    if not decrypted_secret:
        logger.warning(
            "No usable shared secret for CoA disconnect to NAS %s "
            "(nas_devices decrypt failed and no radius.nas row).",
            host,
        )
        return CoADisconnectOutcome(CoADisconnectDisposition.missing_secret)
    client = Client(
        server=host,
        secret=decrypted_secret.encode("utf-8"),
        dict=dictionary,
        coaport=int(nas_device.coa_port or 3799),
    )
    client.retries = _coa_retries(db)
    client.timeout = _radius_timeout_sec(db)
    req = client.CreateCoAPacket(code=DisconnectRequest)
    if username:
        req["User-Name"] = username
    if framed_ip:
        req["Framed-IP-Address"] = framed_ip
    if session_id:
        req["Acct-Session-Id"] = session_id
    try:
        response = client.SendPacket(req)
        _mark_coa_supported(nas_device.id)
        response_code = getattr(response, "code", None)
        if response_code == DisconnectACK:
            return CoADisconnectOutcome(CoADisconnectDisposition.disconnected)
        if response_code == DisconnectNAK:
            error_cause = _disconnect_error_cause(response)
            if error_cause == 503:
                logger.info(
                    "Disconnect-NAK session-not-found from NAS %s for session %s.",
                    nas_device.id,
                    session_id,
                )
                return CoADisconnectOutcome(CoADisconnectDisposition.session_not_found)
            logger.warning(
                "Disconnect-NAK from NAS %s for session %s — disconnect was "
                "not confirmed; falling back to another transport.",
                nas_device.id,
                session_id,
            )
            return CoADisconnectOutcome(CoADisconnectDisposition.rejected)
        logger.warning(
            "Unexpected CoA response from NAS %s for session %s.",
            nas_device.id,
            session_id,
        )
        return CoADisconnectOutcome(CoADisconnectDisposition.unexpected_response)
    except Timeout:
        _mark_coa_unsupported(nas_device.id)
        logger.warning(
            "CoA disconnect timed out for NAS %s — marking unsupported for %s.",
            nas_device.id,
            _COA_NEG_TTL,
        )
        return CoADisconnectOutcome(CoADisconnectDisposition.timeout)
    except Exception as exc:
        logger.warning("CoA disconnect failed for NAS %s: %s", nas_device.id, exc)
        return CoADisconnectOutcome(CoADisconnectDisposition.transport_error)


def _build_mikrotik_rate_limit(profile: RadiusProfile) -> str | None:
    """Build MikroTik-Rate-Limit attribute string from profile speeds.

    Format: rx/tx (download is rx from NAS perspective, upload is tx).
    Supports burst: rx/tx burst-rx/burst-tx threshold-rx/threshold-tx time
    """
    if profile.mikrotik_rate_limit:
        return profile.mikrotik_rate_limit
    if not profile.download_speed and not profile.upload_speed:
        return None
    dl = f"{profile.download_speed}k" if profile.download_speed else "0"
    ul = f"{profile.upload_speed}k" if profile.upload_speed else "0"
    rate = f"{dl}/{ul}"
    if profile.burst_download or profile.burst_upload:
        bdl = f"{profile.burst_download}k" if profile.burst_download else dl
        bul = f"{profile.burst_upload}k" if profile.burst_upload else ul
        threshold = f"{profile.burst_threshold}k" if profile.burst_threshold else dl
        threshold_ul = f"{profile.burst_threshold}k" if profile.burst_threshold else ul
        btime = str(profile.burst_time or 10)
        rate = f"{dl}/{ul} {bdl}/{bul} {threshold}/{threshold_ul} {btime}/{btime}"
    return rate


def _send_coa_update(
    db: Session,
    nas_device: NasDevice,
    username: str | None,
    framed_ip: str | None,
    session_id: str | None,
    profile: RadiusProfile,
) -> bool:
    """Send a RADIUS CoA-Update to change session attributes in-place.

    This sends a CoA-Request (code 43) with updated bandwidth/profile
    attributes, allowing mid-session speed changes without disconnecting
    the subscriber.
    """
    if not _coa_enabled(db):
        return False
    if _coa_disabled_for_nas(nas_device.id):
        logger.debug("Skipping CoA update for NAS %s (negative-cached).", nas_device.id)
        return False
    host = nas_device.nas_ip or nas_device.management_ip or nas_device.ip_address
    if not host:
        logger.warning("Missing NAS host for CoA update.")
        return False
    dict_path = _radius_dictionary_path(db)
    if not dict_path:
        logger.warning("Missing RADIUS dictionary path for CoA update.")
        return False
    try:
        dictionary = Dictionary(dict_path)
    except Exception as exc:
        logger.warning("Failed to load RADIUS dictionary: %s", exc)
        return False
    # Same secret resolution as the disconnect path: device record first,
    # external radius nas table second (decrypt-drift devices have no
    # usable local secret — bare decrypt_credential left profile-change
    # CoA broken on those NAS).
    decrypted_secret = _resolve_coa_secret(db, nas_device)
    if decrypted_secret is None:
        logger.warning(
            "No usable shared secret for CoA update (NAS %s / %s).",
            nas_device.name,
            nas_device.id,
        )
        return False
    client = Client(
        server=host,
        secret=decrypted_secret.encode("utf-8"),
        dict=dictionary,
        coaport=int(nas_device.coa_port or 3799),
    )
    client.retries = _coa_retries(db)
    client.timeout = _radius_timeout_sec(db)
    req = client.CreateCoAPacket(code=CoARequest)
    if username:
        req["User-Name"] = username
    if framed_ip:
        req["Framed-IP-Address"] = framed_ip
    if session_id:
        req["Acct-Session-Id"] = session_id
    # Apply profile bandwidth attributes
    rate_limit = _build_mikrotik_rate_limit(profile)
    if rate_limit:
        try:
            req["Mikrotik-Rate-Limit"] = rate_limit
        except KeyError:
            logger.debug("Mikrotik-Rate-Limit attribute not in dictionary, skipping.")
    if profile.download_speed:
        try:
            req["Filter-Id"] = profile.name or profile.code or ""
        except KeyError:
            pass
    try:
        client.SendPacket(req)
        _mark_coa_supported(nas_device.id)
        logger.info(
            "CoA update sent for user=%s on NAS %s (profile=%s).",
            username,
            nas_device.id,
            profile.name,
        )
        return True
    except Timeout:
        _mark_coa_unsupported(nas_device.id)
        logger.warning(
            "CoA update timed out for NAS %s — marking unsupported for %s.",
            nas_device.id,
            _COA_NEG_TTL,
        )
        return False
    except Exception as exc:
        logger.warning("CoA update failed for NAS %s: %s", nas_device.id, exc)
        return False


def update_subscription_sessions(
    db: Session, subscription_id: str, reason: str | None = None
) -> int:
    """Send CoA-Update to active sessions to apply new profile in-place.

    Falls back to disconnect+reconnect if CoA-Update is not supported
    or fails. Uses credential-level profile override if available.
    """
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sessions = (
        db.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.subscription_id == subscription.id)
        .filter(RadiusAccountingSession.session_end.is_(None))
        .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
        .all()
    )
    if not sessions:
        return 0

    count = 0
    for session in sessions:
        credential = db.get(AccessCredential, session.access_credential_id)
        # Resolve profile with credential-level override support
        profile = _resolve_effective_profile(db, subscription, credential)
        if not profile:
            logger.warning(
                "No profile found for session %s, skipping CoA update.",
                session.id,
            )
            continue

        nas_device = _resolve_nas_device(db, session)
        username = credential.username if credential else None
        framed_ip = subscription.ipv4_address
        session_id = session.session_id
        if nas_device:
            if _send_coa_update(
                db, nas_device, username, framed_ip, session_id, profile
            ):
                count += 1
            else:
                # Fall back to disconnect — subscriber reconnects with new
                # profile. CoA → RouterOS API (read-back verified) → SSH, the
                # same fallback chain the suspend/cancel disconnect path uses,
                # so API-only NAS (no SSH creds) are still refreshed.
                disconnect = _send_coa_disconnect(
                    db, nas_device, username, framed_ip, session_id
                )
                if disconnect.terminal:
                    count += 1
                elif _api_kick_session(db, nas_device, username):
                    count += 1
                elif _disconnect_mikrotik_session(db, nas_device, username):
                    count += 1
    if count:
        logger.info(
            "Updated %s active sessions for subscription %s (%s).",
            count,
            subscription_id,
            reason or "profile_change",
        )
    return count


def _run_ssh(nas_device: NasDevice, command: str, ssh=None) -> str:
    """Run command on the supplied open session, or open a one-shot one."""
    if ssh is not None:
        return ssh.execute(command)
    return DeviceProvisioner._execute_ssh(nas_device, command)


def _disconnect_mikrotik_session(
    db: Session, nas_device: NasDevice, username: str | None, ssh=None
) -> bool:
    if not username:
        return False
    if nas_device.vendor != NasVendor.mikrotik:
        return False
    if not _mikrotik_kill_enabled(db):
        return False
    try:
        safe_user = _sanitize_routeros_value(username)
        _run_ssh(
            nas_device,
            f'/ppp active remove [find where name="{safe_user}"]',
            ssh=ssh,
        )
        return True
    except Exception as exc:
        logger.warning("MikroTik PPP session disconnect failed: %s", exc)
        return False


def _disconnect_mikrotik_hotspot_session(
    db: Session, nas_device: NasDevice, username: str | None, ssh=None
) -> bool:
    """Disconnect an active MikroTik hotspot session."""
    if not username:
        return False
    if nas_device.vendor != NasVendor.mikrotik:
        return False
    if not _mikrotik_kill_enabled(db):
        return False
    try:
        safe_user = _sanitize_routeros_value(username)
        _run_ssh(
            nas_device,
            f'/ip hotspot active remove [find user="{safe_user}"]',
            ssh=ssh,
        )
        return True
    except Exception as exc:
        logger.warning("MikroTik hotspot session disconnect failed: %s", exc)
        return False


def _apply_mikrotik_address_list(
    nas_device: NasDevice, list_name: str, address: str, ssh=None
) -> bool:
    if nas_device.vendor != NasVendor.mikrotik:
        return False
    try:
        safe_list = _sanitize_routeros_value(list_name)
        safe_addr = _sanitize_routeros_value(address)
        # Conditional add — re-blocking the same customer (e.g., duplicate
        # event delivery) is a no-op instead of an error/duplicate entry.
        _run_ssh(
            nas_device,
            (
                f":if ([:len [/ip firewall address-list find "
                f'list="{safe_list}" address="{safe_addr}"]] = 0) '
                f"do={{/ip firewall address-list add "
                f'list="{safe_list}" address="{safe_addr}"}}'
            ),
            ssh=ssh,
        )
        return True
    except Exception as exc:
        logger.warning("MikroTik address-list update failed: %s", exc)
        return False


def _remove_mikrotik_address_list(
    nas_device: NasDevice, list_name: str, address: str, ssh=None
) -> bool:
    if nas_device.vendor != NasVendor.mikrotik:
        return False
    try:
        safe_list = _sanitize_routeros_value(list_name)
        safe_addr = _sanitize_routeros_value(address)
        _run_ssh(
            nas_device,
            f'/ip firewall address-list remove [find list="{safe_list}" address="{safe_addr}"]',
            ssh=ssh,
        )
        return True
    except Exception as exc:
        logger.warning("MikroTik address-list removal failed: %s", exc)
        return False


def _api_kick_session(db: Session, nas_device: NasDevice, username: str | None) -> bool:
    """Disconnect one PPPoE session via the RouterOS API (read-back verified).

    The profile-change refresh path historically fell straight from CoA to SSH;
    on API-only MikroTik NAS (no SSH creds — the common case) that left the
    session live on the old profile. This mirrors the API tier of the
    suspend/cancel disconnect path.
    """
    if not username:
        return False
    if nas_device.vendor != NasVendor.mikrotik:
        return False
    api_dev = _nas_with_api_creds(db, nas_device)
    if api_dev is None:
        return False
    try:
        from app.services.nas._mikrotik import disconnect_mikrotik_pppoe_bulk

        return bool(disconnect_mikrotik_pppoe_bulk(api_dev, {username}))
    except Exception as exc:
        logger.warning(
            "API kick (profile refresh) failed on %s: %s",
            getattr(api_dev, "name", "?"),
            exc,
        )
        return False


def _enforce_address_list_on_nas(
    db: Session,
    nas_device: NasDevice,
    list_name: str,
    address: str,
    *,
    add: bool,
) -> bool:
    """Add (``add=True``) or remove an address-list block on one NAS.

    Tries SSH first (unchanged behaviour for SSH-credentialed devices), then
    falls back to the RouterOS API for API-only MikroTik NAS. Both paths are
    idempotent.
    """
    action = "add" if add else "remove"
    try:
        with DeviceProvisioner.ssh_session(nas_device) as ssh:
            if add:
                ok = _apply_mikrotik_address_list(
                    nas_device, list_name, address, ssh=ssh
                )
            else:
                ok = _remove_mikrotik_address_list(
                    nas_device, list_name, address, ssh=ssh
                )
        if ok:
            return True
    except Exception as exc:
        logger.warning(
            "Address-list %s: SSH path failed for %s: %s — trying API.",
            action,
            getattr(nas_device, "name", "?"),
            exc,
        )

    api_dev = _nas_with_api_creds(db, nas_device)
    if api_dev is None:
        return False
    try:
        from app.services.nas._mikrotik import (
            apply_mikrotik_address_list_via_api,
            remove_mikrotik_address_list_via_api,
        )

        if add:
            return apply_mikrotik_address_list_via_api(api_dev, list_name, address)
        return remove_mikrotik_address_list_via_api(api_dev, list_name, address)
    except Exception as exc:
        logger.warning(
            "Address-list %s: API fallback failed for %s: %s",
            action,
            getattr(api_dev, "name", "?"),
            exc,
        )
        return False


def _open_radacct_sessions_for_username(db: Session, username: str) -> list[dict]:
    """Open sessions straight from radacct (the source of truth).

    The imported RadiusAccountingSession rows lag behind radacct and their
    subscription_id linkage is unreliable for multi-subscription
    subscribers, so live sessions were silently missed on suspend/disable
    (incident 2026-06-11).
    """
    if not username:
        return []
    try:
        from sqlalchemy import Column, DateTime, String, func, select

        from app.services.external_radius_targets import (
            authoritative_accounting_target,
            external_radius_table,
            get_external_engine,
        )

        target = authoritative_accounting_target(db)
        if not target:
            return []
        engine = get_external_engine(target["db_url"])
        radacct = external_radius_table(
            target["radacct_table"],
            Column("username", String),
            Column("acctsessionid", String),
            Column("nasipaddress", String),
            Column("framedipaddress", String),
            Column("acctstoptime", DateTime),
        )
        nas_ip = (
            func.host(radacct.c.nasipaddress)
            if engine.dialect.name == "postgresql"
            else radacct.c.nasipaddress
        )
        framed_ip = (
            func.host(radacct.c.framedipaddress)
            if engine.dialect.name == "postgresql"
            else radacct.c.framedipaddress
        )
        with engine.connect() as conn:
            rows = conn.execute(
                select(radacct.c.acctsessionid, nas_ip, framed_ip).where(
                    radacct.c.username == username,
                    radacct.c.acctstoptime.is_(None),
                )
            ).all()
        return [
            {"session_id": r[0], "nas_ip": r[1], "framed_ip": r[2] or None}
            for r in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("radacct open-session lookup failed for %s: %s", username, exc)
        return []


def _nas_device_by_ip(db: Session, nas_ip: str) -> NasDevice | None:
    if not nas_ip:
        return None
    from sqlalchemy import or_

    return (
        db.query(NasDevice)
        .filter(
            or_(
                NasDevice.nas_ip == nas_ip,
                NasDevice.management_ip == nas_ip,
                NasDevice.ip_address == nas_ip,
            )
        )
        .filter(NasDevice.is_active.is_(True))
        .first()
    )


def disconnect_subscription_sessions(
    db: Session,
    subscription_id: str,
    reason: str | None = None,
    *,
    framed_ip_address: str | None = None,
    require_terminal: bool = False,
) -> int:
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    login = (subscription.login or "").strip()
    expected_framed_ip = str(framed_ip_address or "").strip()

    # Primary source: open radacct sessions by login. Keyed by session_id
    # to dedupe against the legacy app-side rows added below.
    targets: dict[str, tuple[Any, str | None, str, str | None]] = {}
    found = 0
    for sess in _open_radacct_sessions_for_username(db, login):
        observed_framed_ip = str(sess["framed_ip"] or "").strip()
        if expected_framed_ip and observed_framed_ip != expected_framed_ip:
            continue
        found += 1
        nas_device = _nas_device_by_ip(db, sess["nas_ip"])
        if not nas_device:
            logger.warning(
                "No active NasDevice matches NAS IP %s (session %s, user %s)",
                sess["nas_ip"],
                sess["session_id"],
                login,
            )
            continue
        targets[sess["session_id"]] = (
            nas_device,
            login,
            sess["session_id"],
            sess["framed_ip"],
        )

    # Secondary source: app-side imported sessions (covers sessions whose
    # login changed after they started).
    legacy_sessions = (
        db.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.subscription_id == subscription.id)
        .filter(RadiusAccountingSession.session_end.is_(None))
        .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
        .all()
    )
    for session in legacy_sessions:
        if session.session_id in targets:
            continue
        observed_framed_ip = str(session.framed_ip_address or "").strip()
        if expected_framed_ip and observed_framed_ip != expected_framed_ip:
            continue
        found += 1
        nas_device = _resolve_nas_device(db, session)
        if not nas_device:
            logger.warning(
                "Open session %s for subscription %s has no NAS device — "
                "cannot target a disconnect.",
                session.session_id,
                subscription_id,
            )
            continue
        credential = db.get(AccessCredential, session.access_credential_id)
        username = (credential.username if credential else None) or login or None
        targets[session.session_id] = (
            nas_device,
            username,
            session.session_id,
            session.framed_ip_address,
        )

    if not targets:
        if found:
            logger.warning(
                "Failed to disconnect any of %s open sessions for "
                "subscription %s — no NAS device resolvable.",
                found,
                subscription_id,
            )
            if require_terminal:
                raise RuntimeError(
                    "Targeted RADIUS sessions have no resolvable NAS device."
                )
        return 0

    # Group sessions by NAS so we open at most one SSH connection per device
    # for the SSH-kick fallback, instead of one per session.
    by_nas: dict[Any, list[tuple[Any, str | None, str, str | None]]] = {}
    for entry in targets.values():
        by_nas.setdefault(entry[0].id, []).append(entry)

    count = 0
    for entries in by_nas.values():
        nas_device = entries[0][0]
        # Try CoA for every session first (UDP, no connection cost worth sharing).
        coa_ok: set[int] = set()
        needs_ssh_kick = False
        for idx, (_, username, session_id, framed_ip) in enumerate(entries):
            disconnect = _send_coa_disconnect(
                db, nas_device, username, framed_ip, session_id
            )
            if disconnect.terminal:
                coa_ok.add(idx)
                count += 1
            else:
                needs_ssh_kick = True

        if not needs_ssh_kick:
            continue

        # CoA could not confirm these — disconnect via the RouterOS API. The API
        # is reachable and credentialed fleet-wide (SSH creds are absent on
        # nearly all NAS), and its read-back VERIFIES the drop, which CoA cannot
        # (a lost CoA reply is indistinguishable from a real failure).
        fallback = {
            username
            for idx, (_, username, _sid, _fip) in enumerate(entries)
            if idx not in coa_ok and username
        }
        api_confirmed: set[str] = set()
        api_dev = (
            _nas_with_api_creds(db, nas_device)
            if _mikrotik_api_session_kick_enabled(db)
            else None
        )
        if api_dev is not None and fallback:
            try:
                from app.services.nas._mikrotik import (
                    disconnect_mikrotik_pppoe_bulk,
                )

                api_confirmed = disconnect_mikrotik_pppoe_bulk(api_dev, fallback)
                count += len(api_confirmed)
                logger.info(
                    "API-kicked %s/%s sessions on %s (verified via read-back).",
                    len(api_confirmed),
                    len(fallback),
                    getattr(api_dev, "name", "?"),
                )
            except Exception as exc:
                logger.warning(
                    "API kick failed on %s: %s — trying SSH.",
                    getattr(nas_device, "name", "?"),
                    exc,
                )

        # SSH last resort, only for sessions the API did not confirm gone.
        remaining = fallback - api_confirmed
        if not remaining:
            continue
        try:
            with DeviceProvisioner.ssh_session(nas_device) as ssh:
                for idx, (_, username, _session_id, _framed_ip) in enumerate(entries):
                    if idx in coa_ok or username not in remaining:
                        continue
                    if _disconnect_mikrotik_session(db, nas_device, username, ssh=ssh):
                        count += 1
                    elif _disconnect_mikrotik_hotspot_session(
                        db, nas_device, username, ssh=ssh
                    ):
                        count += 1
        except Exception as exc:
            logger.warning(
                "Failed to open SSH session for kick on %s: %s",
                getattr(nas_device, "name", "?"),
                exc,
            )

    if count:
        logger.info(
            "Disconnected %s active sessions for subscription %s (%s).",
            count,
            subscription_id,
            reason or "no_reason",
        )
    else:
        logger.warning(
            "Failed to disconnect any of %s open sessions for subscription %s.",
            found,
            subscription_id,
        )
    if require_terminal:
        remaining_sessions = [
            session
            for session in _open_radacct_sessions_for_username(db, login)
            if not expected_framed_ip
            or str(session["framed_ip"] or "").strip() == expected_framed_ip
        ]
        if remaining_sessions:
            raise RuntimeError(
                "One or more targeted RADIUS sessions remain active after disconnect."
            )
    return count


def network_identity_signature(db: Session, subscription: Subscription) -> tuple:
    """The RADIUS-effective network identity of a subscription: login, served
    IPv4, served IPv6 prefix, RADIUS profile, and the sorted set of active routed
    blocks. Used to decide whether an edit actually changed something the BNG must
    re-learn — so a non-effective edit doesn't trigger a needless session kick.
    """
    from app.models.network import SubscriberAdditionalRoute

    routes: tuple[str, ...] = ()
    if subscription.subscriber_id is not None:
        routes = tuple(
            sorted(
                str(cidr)
                for (cidr,) in db.query(SubscriberAdditionalRoute.cidr)
                .filter(
                    SubscriberAdditionalRoute.subscriber_id
                    == subscription.subscriber_id
                )
                .filter(SubscriberAdditionalRoute.is_active.is_(True))
                .all()
            )
        )
    return (
        (subscription.login or "").strip(),
        (subscription.ipv4_address or "").strip(),
        (subscription.ipv6_address or "").strip(),
        str(subscription.radius_profile_id or ""),
        routes,
    )


def reauth_subscription_on_identity_change(
    db: Session,
    subscription_id: str,
    *,
    before: tuple,
    reason: str = "network_identity_change",
) -> dict:
    """Reconcile RADIUS then kick live sessions when an ACTIVE subscription's
    network identity changed.

    Call AFTER the edit is committed (never mid-transaction). Compares the current
    signature to ``before``; if unchanged — or the subscription is not active —
    it's a no-op (avoids needless kicks). Otherwise it reconciles this
    subscriber's RADIUS state *first* (so a re-auth lands on the new
    Framed-IP/routes/profile) and *then* disconnects open sessions so the BNG
    re-learns them.
    """
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription or subscription.status != SubscriptionStatus.active:
        return {"changed": False, "reason": "not_active"}
    after = network_identity_signature(db, subscription)
    if after == before:
        return {"changed": False, "reason": "no_effective_change"}

    # RADIUS first (synchronous, per-subscriber), then kick.
    try:
        from app.services.radius import reconcile_subscription_connectivity

        reconcile_subscription_connectivity(db, subscription_id).require_projected()
    except Exception as exc:  # pragma: no cover - operational guard
        logger.warning(
            "reauth: RADIUS reconcile failed for sub=%s: %s; session CoA deferred",
            subscription_id,
            exc,
        )
        return {
            "changed": True,
            "disconnected": 0,
            "reason": "projection_incomplete",
        }

    disconnected = 0
    try:
        disconnected = disconnect_subscription_sessions(
            db, subscription_id, reason=reason
        )
    except Exception as exc:  # pragma: no cover - kick is best-effort
        logger.warning(
            "reauth: session disconnect failed for sub=%s: %s", subscription_id, exc
        )
    logger.info(
        "reauth on identity change sub=%s reason=%s disconnected=%d",
        subscription_id,
        reason,
        disconnected,
    )
    return {"changed": True, "disconnected": disconnected}


def disconnect_account_sessions(
    db: Session, account_id: str, reason: str | None = None
) -> int:
    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == coerce_uuid(account_id))
        .all()
    )
    count = 0
    for sub in subscriptions:
        count += disconnect_subscription_sessions(db, str(sub.id), reason=reason)
    return count


def apply_radius_profile_to_account(
    db: Session, account_id: str, profile_id: str
) -> int:
    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == coerce_uuid(account_id))
        .filter(AccessCredential.is_active.is_(True))
        .all()
    )
    if not credentials:
        return 0
    profile = db.get(RadiusProfile, coerce_uuid(profile_id))
    if not profile or not profile.is_active:
        raise HTTPException(status_code=404, detail="RADIUS profile not found")
    updated = 0
    for cred in credentials:
        if cred.radius_profile_id != profile.id:
            cred.radius_profile_id = profile.id
            updated += 1
    db.commit()
    from app.services.radius_population import reconcile_usernames

    reconcile_usernames(
        {credential.username for credential in credentials},
        dry_run=False,
        source_db=db,
    )
    return updated


def apply_subscription_address_list_block(db: Session, subscription_id: str) -> int:
    if not _address_list_block_enabled(db):
        return 0
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        return 0
    list_name = suspended_address_list(db)
    if not subscription.ipv4_address:
        return 0
    sessions = (
        db.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.subscription_id == subscription.id)
        .filter(RadiusAccountingSession.session_end.is_(None))
        .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
        .all()
    )
    count = 0
    targets: dict[Any, NasDevice] = {}
    for session in sessions:
        nas_device = _resolve_nas_device(db, session)
        if nas_device:
            targets.setdefault(nas_device.id, nas_device)
    if not targets and subscription.provisioning_nas_device_id:
        nas_device = db.get(NasDevice, subscription.provisioning_nas_device_id)
        if nas_device:
            targets[nas_device.id] = nas_device
    for nas_device in targets.values():
        if _enforce_address_list_on_nas(
            db, nas_device, list_name, subscription.ipv4_address, add=True
        ):
            count += 1
    return count


def remove_subscription_address_list_block(db: Session, subscription_id: str) -> int:
    if not _address_list_block_enabled(db):
        return 0
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        return 0
    list_name = suspended_address_list(db)
    if not subscription.ipv4_address:
        return 0
    sessions = (
        db.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.subscription_id == subscription.id)
        .filter(RadiusAccountingSession.session_end.is_(None))
        .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
        .all()
    )
    count = 0
    targets: dict[Any, NasDevice] = {}
    for session in sessions:
        nas_device = _resolve_nas_device(db, session)
        if nas_device:
            targets.setdefault(nas_device.id, nas_device)
    if not targets and subscription.provisioning_nas_device_id:
        nas_device = db.get(NasDevice, subscription.provisioning_nas_device_id)
        if nas_device:
            targets[nas_device.id] = nas_device
    for nas_device in targets.values():
        if _enforce_address_list_on_nas(
            db, nas_device, list_name, subscription.ipv4_address, add=False
        ):
            count += 1
    return count


def lift_fup_enforcement(
    db: Session,
    subscription_id: str,
    *,
    evaluated_at: datetime,
) -> dict[str, Any]:
    """Undo whatever FUP enforcement is active, then clear the FUP state row.

    Called at the quota period boundary (and on a qualifying top-up). The old
    reset path only nulled the ``FupState`` row, leaving the throttle profile /
    address-list block / suspension in place on the wire — so a subscriber
    stayed throttled or blocked indefinitely. This reverses the action recorded
    in the state *before* clearing it:

    - ``throttled`` → re-apply the captured original (or offer-effective)
      RADIUS profile and re-sync to RADIUS.
    - ``blocked``   → remove the address-list block AND resume any FUP
      suspension (both idempotent; ``blocked`` covers both apply paths).
    """
    from app.models.fup_state import FupActionStatus
    from app.services.fup_state import ClearFupRuntimeState, fup_state

    state = fup_state.get(db, subscription_id)
    if not state:
        return {"lifted": False, "reason": "no_state", "actions": []}

    prior = state.action_status
    actions: list[str] = []
    subscription = db.get(Subscription, coerce_uuid(subscription_id))

    if prior == FupActionStatus.throttled:
        target = str(state.original_profile_id) if state.original_profile_id else None
        if not target and subscription is not None:
            prof = _resolve_effective_profile(db, subscription)
            target = str(prof.id) if prof else None
        if target and subscription is not None:
            try:
                apply_radius_profile_to_account(
                    db, str(subscription.subscriber_id), target
                )
                actions.append("restore_profile")
            except Exception as exc:
                logger.warning(
                    "FUP lift: profile restore failed for %s: %s",
                    subscription_id,
                    exc,
                )
    elif prior == FupActionStatus.blocked:
        try:
            remove_subscription_address_list_block(db, subscription_id)
            actions.append("remove_block")
        except Exception as exc:
            logger.warning(
                "FUP lift: address-list unblock failed for %s: %s",
                subscription_id,
                exc,
            )
        # Resume only if a FUP suspension actually flipped the status.
        from app.services.account_lifecycle import SUSPENDED_EQUIVALENT

        if subscription is not None and subscription.status in SUSPENDED_EQUIVALENT:
            try:
                from app.models.enforcement_lock import EnforcementReason
                from app.services.account_lifecycle import restore_subscription

                restored = restore_subscription(
                    db,
                    subscription_id,
                    trigger="cap_reset",
                    resolved_by="fup_cap_reset",
                    reason=EnforcementReason.fup,
                )
                if restored:
                    actions.append("resume")
            except Exception as exc:
                logger.warning(
                    "FUP lift: resume failed for %s: %s", subscription_id, exc
                )

    fup_state.clear(
        db,
        ClearFupRuntimeState(
            subscription_id=UUID(subscription_id),
            evaluated_at=evaluated_at,
        ),
    )
    db.flush()
    return {"lifted": True, "prior": prior.value, "actions": actions}


# ---------------------------------------------------------------------------
# Subscription cancellation cleanup
# ---------------------------------------------------------------------------


def cleanup_subscription_on_cancel(db: Session, subscription_id: str) -> dict[str, int]:
    """Full cleanup when a subscription is canceled.

    1. Disconnect all active RADIUS sessions
    2. Deactivate RADIUS credentials
    3. Remove credentials from external RADIUS DB
    4. Release IP assignments
    5. Remove NAS user entries
    6. Clean up address list entries

    Returns:
        Dict with counts of each cleanup action
    """
    from app.models.radius import RadiusUser

    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        return {"error": 1}

    stats: dict[str, int] = {
        "sessions_disconnected": 0,
        "credentials_deactivated": 0,
        "radius_users_removed": 0,
        "ip_assignments_released": 0,
        "nas_commands_sent": 0,
    }

    # Pre-change backup BEFORE the destructive cancel mutations (credential
    # deactivation + the IP null at the bottom, the R2 "only copy" risk).
    # Best-effort: capture never raises into the cancel path.
    from app.services.connectivity_backup import capture_connectivity_state

    capture_connectivity_state(db, subscription.subscriber_id, reason="cancel")

    # 1. Deactivate source credentials and project before any CoA consequence.
    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscription.subscriber_id)
        .filter(AccessCredential.is_active.is_(True))
        .all()
    )
    # Only deactivate if no other active subscriptions for this subscriber
    other_active = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscription.subscriber_id)
        .filter(Subscription.id != subscription.id)
        .filter(Subscription.status.in_(["active", "pending"]))
        .first()
    )
    if not other_active:
        for cred in credentials:
            cred.is_active = False
            stats["credentials_deactivated"] += 1
        # 3. Remove from external RADIUS DB
        _remove_credentials_from_external_radius(db, credentials)
        # Remove internal RadiusUser records
        for cred in credentials:
            radius_users = (
                db.query(RadiusUser)
                .filter(RadiusUser.access_credential_id == cred.id)
                .all()
            )
            for ru in radius_users:
                ru.is_active = False
                stats["radius_users_removed"] += 1

    # 2. Disconnect only after every configured projection target committed.
    try:
        stats["sessions_disconnected"] = disconnect_subscription_sessions(
            db,
            subscription_id,
            reason="canceled",
        )
    except Exception as exc:
        logger.warning("Session disconnect on cancel failed: %s", exc)

    # 4. Clear IP from subscription (IP assignments are now subscriber-level)
    # IP assignments are managed independently of subscriptions
    subscription.ipv4_address = None
    subscription.ipv6_address = None

    # 5. Remove NAS user entries
    if subscription.provisioning_nas_device_id:
        nas_device = db.get(NasDevice, subscription.provisioning_nas_device_id)
        if nas_device:
            try:
                from app.services.connection_type_provisioning import (
                    build_nas_provisioning_commands,
                )

                profile = _resolve_effective_profile(db, subscription)
                commands = build_nas_provisioning_commands(
                    db,
                    subscription,
                    nas_device,
                    profile=profile,
                    action="delete",
                )
                # A PPPoE cancellation yields no commands by design: the NAS
                # holds no per-customer record, so there is nothing to clean up
                # and no reason to open an SSH session to every NAS on every
                # cancel. Pre-existing shadowing secrets are retired per NAS
                # through network.nas_local_secret_boundary's reviewed cleanup,
                # not implicitly here.
                if commands:
                    with DeviceProvisioner.ssh_session(nas_device) as ssh:
                        for cmd in commands:
                            try:
                                ssh.execute(cmd)
                                stats["nas_commands_sent"] += 1
                            except Exception as cmd_exc:
                                logger.warning(
                                    "NAS cleanup command failed: %s", cmd_exc
                                )
            except Exception as exc:
                logger.warning(
                    "NAS cleanup failed for subscription %s: %s", subscription_id, exc
                )

    # 6. Remove address list entries
    try:
        remove_subscription_address_list_block(db, subscription_id)
    except Exception as exc:
        logger.warning("Address list cleanup failed: %s", exc)

    db.flush()
    logger.info(
        "Subscription %s cancellation cleanup: %s",
        subscription_id,
        stats,
    )
    return stats


def cleanup_subscription_on_suspend(
    db: Session, subscription_id: str
) -> dict[str, int]:
    """Cleanup when a subscription is suspended.

    Unlike cancellation, suspension is reversible so we:
    1. Disconnect all active RADIUS sessions
    2. Deactivate RadiusUser records (prevents new auth)
    3. Remove credentials from external RADIUS DB
    4. Optionally add to blocked address list

    Credentials themselves remain active for when the subscription
    is restored.

    Returns:
        Dict with counts of each cleanup action
    """
    from app.models.radius import RadiusUser

    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        return {"error": 1}

    stats: dict[str, int] = {
        "sessions_disconnected": 0,
        "radius_users_deactivated": 0,
        "external_radius_removed": 0,
        "address_list_blocked": 0,
    }

    # Pre-change backup BEFORE the suspend mutations (RadiusUser deactivation +
    # external RADIUS removal). Best-effort: never raises into the suspend path.
    from app.services.connectivity_backup import capture_connectivity_state

    capture_connectivity_state(db, subscription.subscriber_id, reason="suspend")

    # 1. Deactivate RadiusUser source state for this subscription.
    # This prevents new authentication attempts while suspended
    radius_users = (
        db.query(RadiusUser)
        .filter(RadiusUser.subscription_id == subscription.id)
        .filter(RadiusUser.is_active.is_(True))
        .all()
    )
    for ru in radius_users:
        ru.is_active = False
        stats["radius_users_deactivated"] += 1

    # 3. Remove THIS SUBSCRIPTION's credentials from the external RADIUS DB.
    #
    # This used to query by subscriber_id and hard-DELETE radcheck/radreply/
    # radusergroup for every credential the subscriber owned — so suspending one
    # service (for any reason, including a correct prepaid suspension) took the
    # customer's OTHER PAID SERVICES offline too, until the next populate() sweep
    # rebuilt them. Step 2 above already scopes to subscription.id; step 3 did not.
    #
    # Legacy credentials predate the subscription link and carry a NULL
    # subscription_id. Those still have to be removed, or suspension would stop
    # working for the customers who have them — but only when this is the
    # subscriber's ONLY live subscription, so an unlinked credential can never be
    # attributed to a sibling service that is still paid for.
    # A sibling that should still be ON. A suspended sibling needs no protection —
    # its own credentials are meant to be gone.
    other_serving_subscription = (
        db.query(Subscription.id)
        .filter(Subscription.subscriber_id == subscription.subscriber_id)
        .filter(Subscription.id != subscription.id)
        .filter(
            Subscription.status.in_(
                (SubscriptionStatus.active, SubscriptionStatus.pending)
            )
        )
        .first()
    )
    credential_scope = [AccessCredential.subscription_id == subscription.id]
    if other_serving_subscription is None:
        credential_scope.append(AccessCredential.subscription_id.is_(None))

    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscription.subscriber_id)
        .filter(or_(*credential_scope))
        .filter(AccessCredential.is_active.is_(True))
        .all()
    )
    projection_ready = True
    if credentials:
        try:
            _remove_credentials_from_external_radius(db, credentials)
            stats["external_radius_removed"] = len(credentials)
        except Exception as exc:
            projection_ready = False
            logger.warning("External RADIUS removal on suspend failed: %s", exc)

    # 2. CoA only follows a complete multi-target projection.
    if projection_ready:
        try:
            stats["sessions_disconnected"] = disconnect_subscription_sessions(
                db,
                subscription_id,
                reason="suspended",
            )
        except Exception as exc:
            logger.warning("Session disconnect on suspend failed: %s", exc)
    else:
        logger.error(
            "Session disconnect deferred for subscription %s: external RADIUS "
            "projection is incomplete",
            subscription_id,
        )

    # 4. Apply address list block if configured
    try:
        stats["address_list_blocked"] = apply_subscription_address_list_block(
            db, subscription_id
        )
    except Exception as exc:
        logger.warning("Address list block on suspend failed: %s", exc)

    db.flush()
    logger.info(
        "Subscription %s suspension cleanup: %s",
        subscription_id,
        stats,
    )
    return stats


def restore_subscription_connectivity(
    db: Session, subscription_id: str
) -> dict[str, int]:
    """Restore RADIUS connectivity when a subscription is resumed.

    Reverses the suspension cleanup:
    1. Reactivate RadiusUser records
    2. Sync credentials back to external RADIUS DB
    3. Remove address list blocks

    Returns:
        Dict with counts of each restore action
    """
    from app.models.radius import RadiusUser
    from app.services.radius import reconcile_subscription_connectivity

    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        return {"error": 1}

    stats: dict[str, int] = {
        "radius_users_reactivated": 0,
        "external_radius_synced": 0,
        "address_list_unblocked": 0,
    }

    # Pre-change backup BEFORE the restore mutations (RadiusUser reactivation +
    # external re-sync). Best-effort: never raises into the restore path.
    from app.services.connectivity_backup import capture_connectivity_state

    capture_connectivity_state(db, subscription.subscriber_id, reason="restore")

    # 1. Reactivate RadiusUser records
    radius_users = (
        db.query(RadiusUser)
        .filter(RadiusUser.subscription_id == subscription.id)
        .filter(RadiusUser.is_active.is_(False))
        .all()
    )
    for ru in radius_users:
        ru.is_active = True
        stats["radius_users_reactivated"] += 1

    # 2. Sync credentials back to external RADIUS
    result = reconcile_subscription_connectivity(db, subscription_id)
    result.require_projected()
    stats["external_radius_synced"] = result.external_credentials_synced

    # 3. Remove address list blocks
    stats["address_list_unblocked"] = remove_subscription_address_list_block(
        db, subscription_id
    )

    db.flush()
    logger.info(
        "Subscription %s restore cleanup: %s",
        subscription_id,
        stats,
    )
    return stats


def _remove_credentials_from_external_radius(
    db: Session, credentials: list[AccessCredential]
) -> None:
    """Request convergence from the sole external RADIUS projection owner."""
    if not credentials:
        return
    from app.services.radius_population import reconcile_usernames

    reconcile_usernames(
        {credential.username for credential in credentials if credential.username},
        dry_run=False,
        source_db=db,
    )
