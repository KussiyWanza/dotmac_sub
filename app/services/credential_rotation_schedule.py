"""Scheduled lifecycle for credential-at-rest encryption keys."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network_monitoring import AlertSeverity
from app.services.credential_crypto import (
    generate_encryption_key,
    get_encryption_key,
)
from app.services.credential_key_rotation import (
    CredentialIntegrityResult,
    publish_credential_integrity_snapshot,
    rotate_credential_encryption_material,
    scan_credential_encryption_integrity,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.observability import (
    Finding,
    record_finding,
    resolve_findings,
)
from app.services.secrets import (
    clear_cache,
    is_openbao_available,
    is_openbao_ref,
    read_secret_fields,
    write_secret,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

_ROTATION_LOCK_KEY = 0x43524544  # CRED
_CANONICAL_PATH = "settings/auth"
_LEGACY_PATH = "auth"
_CURRENT_FIELD = "credential_encryption_key"
_PREVIOUS_FIELD = "credential_encryption_previous_key"
_ROTATED_AT_FIELD = "credential_encryption_rotated_at"
_RETIRE_AFTER_FIELD = "credential_encryption_previous_retire_after"
_INTEGRITY_FINDING_PREFIX = "observability:credentials:"


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: object, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value)) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _managed_key_source() -> tuple[bool, str]:
    """Whether rotation may proceed: is the active key one OpenBao owns?

    Rotation writes a new key into OpenBao, so it is only safe when the key
    this process is actually using came from there. A key pinned to a literal
    in the environment must block: rotating the store would leave the process
    using the pinned value and every credential re-encrypted under a key it
    cannot read.
    """

    env_value = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if env_value:
        if is_openbao_ref(env_value):
            return True, "openbao_env_ref"
        return False, "static_environment_key"

    # The held set is what `get_encryption_key` now reads, so it is what
    # decides. The `auth/credential_encryption_key` ROW is deliberately no
    # longer consulted here: it stopped being a source of the key, and a gate
    # that answered "managed" from a row nothing reads would let rotation
    # proceed for a process that holds no key at all — which the
    # `managed_key_missing` check below would then report as the failure,
    # naming the wrong cause.
    from dotmac_kernel.secret_sources import secret_names

    if _CURRENT_FIELD in secret_names():
        return True, "openbao_held_secret"
    return False, "credential_key_is_not_held_from_openbao"


def _reload_key_material() -> None:
    """Re-read the key material this process is working from.

    Rotation writes the new key into OpenBao, and until this runs the process
    is still using the key it loaded at boot — `clear_cache()` alone only
    drops the OpenBao READ cache, which `get_encryption_key` no longer
    consults. Rotation is precisely the moment the kernel's held set is meant
    to be refreshed: `dotmac_kernel.secret_sources` reloads on an explicit act
    and never on a timer, so that a key changes when an operator says so.

    A deployment with no source installed (secrets configured through the
    environment, or a developer machine) has nothing to refresh, and that is
    not a rotation failure — the keyring write already succeeded.
    """

    clear_cache()
    from dotmac_kernel.secret_sources import (
        SecretSourceError,
        refresh_secrets,
        secret_source_installed,
    )

    if not secret_source_installed():
        return
    try:
        refresh_secrets()
    except SecretSourceError:
        # The keyring is already written; the process keeps the previous held
        # set (the kernel's documented behaviour) and the next boot picks the
        # new key up. Loud, because until then this process signs and decrypts
        # with a key the store no longer calls current.
        logger.exception("credential_rotation_secret_refresh_failed")


def _keyring_payload() -> dict[str, str]:
    canonical = read_secret_fields(_CANONICAL_PATH)
    if canonical.get(_CURRENT_FIELD):
        return dict(canonical)
    return dict(read_secret_fields(_LEGACY_PATH))


def _write_keyring(payload: dict[str, str]) -> bool:
    # Update the legacy path first. If the canonical write then fails, no data
    # rotation has started and both key locations still contain a usable keyring.
    legacy = dict(read_secret_fields(_LEGACY_PATH))
    legacy.update(payload)
    if not write_secret(_LEGACY_PATH, legacy):
        return False
    canonical = dict(read_secret_fields(_CANONICAL_PATH))
    canonical.update(payload)
    return write_secret(_CANONICAL_PATH, canonical)


def _retire_previous_key(payload: dict[str, str]) -> bool:
    retired = dict(payload)
    retired.pop(_PREVIOUS_FIELD, None)
    retired.pop(_RETIRE_AFTER_FIELD, None)

    legacy = dict(read_secret_fields(_LEGACY_PATH))
    legacy.pop(_PREVIOUS_FIELD, None)
    legacy.pop(_RETIRE_AFTER_FIELD, None)
    legacy.update(retired)
    if not write_secret(_LEGACY_PATH, legacy):
        return False

    canonical = dict(read_secret_fields(_CANONICAL_PATH))
    canonical.pop(_PREVIOUS_FIELD, None)
    canonical.pop(_RETIRE_AFTER_FIELD, None)
    canonical.update(retired)
    return write_secret(_CANONICAL_PATH, canonical)


def evaluate_scheduled_rotation(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Evaluate and, when due, safely rotate the managed credential key."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    enabled = _as_bool(
        resolve_value(db, SettingDomain.auth, "credential_rotation_enabled"),
        True,
    )
    if not enabled:
        return {"status": "disabled", "rotated": False}

    interval_days = _as_int(
        resolve_value(db, SettingDomain.auth, "credential_rotation_interval_days"),
        90,
        minimum=7,
        maximum=365,
    )
    grace_days = _as_int(
        resolve_value(db, SettingDomain.auth, "credential_rotation_grace_days"),
        7,
        minimum=1,
        maximum=30,
    )
    auto_apply = _as_bool(
        resolve_value(db, SettingDomain.auth, "credential_rotation_auto_apply"),
        True,
    )

    managed, source = _managed_key_source()
    if not managed:
        logger.error("credential_rotation_blocked: source=%s", source)
        return {
            "status": "blocked",
            "reason": source,
            "rotated": False,
        }
    if not is_openbao_available():
        return {
            "status": "blocked",
            "reason": "openbao_unavailable",
            "rotated": False,
        }

    payload = _keyring_payload()
    current_key = str(payload.get(_CURRENT_FIELD) or "")
    active_key = get_encryption_key()
    if not current_key or not active_key:
        return {
            "status": "blocked",
            "reason": "managed_key_missing",
            "rotated": False,
        }
    active_key_text = active_key.decode("ascii")
    if active_key_text != current_key:
        _reload_key_material()
        refreshed = get_encryption_key()
        if not refreshed or refreshed.decode("ascii") != current_key:
            return {
                "status": "blocked",
                "reason": "active_key_source_mismatch",
                "rotated": False,
            }

    previous_key = str(payload.get(_PREVIOUS_FIELD) or "")
    if previous_key:
        result = rotate_credential_encryption_material(
            db,
            old_key=previous_key,
            new_key=current_key,
            commit=True,
        )
        retire_after = _parse_datetime(payload.get(_RETIRE_AFTER_FIELD))
        retired = bool(retire_after and now >= retire_after)
        if retired and not _retire_previous_key(payload):
            raise RuntimeError("Failed to retire previous credential encryption key")
        _reload_key_material()
        return {
            "status": "previous_key_retired" if retired else "grace_period",
            "rotated": False,
            "updated_records": result.updated_records,
            "updated_values": result.updated_values,
            "retire_after": retire_after.isoformat() if retire_after else None,
        }

    rotated_at = _parse_datetime(payload.get(_ROTATED_AT_FIELD))
    if rotated_at is None:
        initialized = dict(payload)
        initialized[_ROTATED_AT_FIELD] = now.isoformat()
        if not _write_keyring(initialized):
            raise RuntimeError("Failed to initialize credential rotation metadata")
        return {
            "status": "initialized",
            "rotated": False,
            "next_rotation_at": (now + timedelta(days=interval_days)).isoformat(),
        }

    due_at = rotated_at + timedelta(days=interval_days)
    if now < due_at:
        return {
            "status": "not_due",
            "rotated": False,
            "next_rotation_at": due_at.isoformat(),
        }
    if not auto_apply:
        logger.warning("credential_rotation_due: automatic apply is disabled")
        return {
            "status": "due",
            "rotated": False,
            "next_rotation_at": due_at.isoformat(),
        }

    new_key = generate_encryption_key()
    staged = dict(payload)
    staged[_CURRENT_FIELD] = new_key
    staged[_PREVIOUS_FIELD] = current_key
    staged[_ROTATED_AT_FIELD] = now.isoformat()
    staged[_RETIRE_AFTER_FIELD] = (now + timedelta(days=grace_days)).isoformat()
    if not _write_keyring(staged):
        raise RuntimeError("Failed to stage credential encryption keyring")

    _reload_key_material()
    result = rotate_credential_encryption_material(
        db,
        old_key=current_key,
        new_key=new_key,
        commit=True,
    )
    logger.warning(
        "credential_rotation_completed: records=%d values=%d grace_days=%d",
        result.updated_records,
        result.updated_values,
        grace_days,
    )
    return {
        "status": "rotated",
        "rotated": True,
        "updated_records": result.updated_records,
        "updated_values": result.updated_values,
        "previous_key_retire_after": staged[_RETIRE_AFTER_FIELD],
    }


def _sync_integrity_findings(
    db: Session,
    integrity: CredentialIntegrityResult,
    rotation_result: dict[str, object],
) -> None:
    active: set[str] = set()
    totals = integrity.totals
    for state, severity, title in (
        (
            "undecryptable",
            AlertSeverity.critical,
            "Stored credentials cannot be decrypted",
        ),
        (
            "plaintext",
            AlertSeverity.warning,
            "Stored credentials are not encrypted",
        ),
    ):
        count = int(totals.get(state, 0))
        if count <= 0:
            continue
        fingerprint = f"{_INTEGRITY_FINDING_PREFIX}{state}"
        active.add(fingerprint)
        affected_scopes = sorted(
            scope
            for scope, states in integrity.counts.items()
            if int(states.get(state, 0)) > 0
        )
        record_finding(
            db,
            Finding(
                fingerprint=fingerprint,
                domain="security",
                source="credential_integrity",
                severity=severity,
                title=title,
                summary=f"{count} credential value(s) classified as {state}.",
                details={
                    "state": state,
                    "count": count,
                    "affected_scopes": affected_scopes,
                    "scanned_at": integrity.scanned_at.isoformat(),
                },
                target_url="/admin/system/secrets",
            ),
        )
    if rotation_result.get("status") == "blocked":
        fingerprint = f"{_INTEGRITY_FINDING_PREFIX}rotation-blocked"
        active.add(fingerprint)
        reason = str(rotation_result.get("reason") or "unknown")
        record_finding(
            db,
            Finding(
                fingerprint=fingerprint,
                domain="security",
                source="credential_rotation",
                severity=AlertSeverity.critical,
                title="Credential key rotation is blocked",
                summary=f"Credential key rotation is blocked: {reason}.",
                details={"reason": reason},
                target_url="/admin/system/secrets",
            ),
        )
    resolve_findings(
        db,
        managed_prefix=_INTEGRITY_FINDING_PREFIX,
        active_fingerprints=active,
    )


def _publish_integrity_state(
    db: Session,
    integrity: CredentialIntegrityResult,
    *,
    managed: bool,
    key_source: str,
    rotation_result: dict[str, object],
) -> None:
    extra_observations: list[tuple[str, str, float]] = [
        ("managed_key_source", key_source, 1.0 if managed else 0.0),
    ]
    rotation_status = str(rotation_result.get("status") or "error")
    due_at = _parse_datetime(rotation_result.get("next_rotation_at"))
    if due_at is not None:
        extra_observations.append(
            ("rotation_next_due_timestamp_seconds", "all", due_at.timestamp())
        )
    publish_credential_integrity_snapshot(
        integrity,
        operation="rotation",
        operation_status=rotation_status,
        extra_observations=tuple(extra_observations),
    )

    try:
        _sync_integrity_findings(db, integrity, rotation_result)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("credential_integrity_finding_sync_failed")


def _attach_integrity_counts(
    result: dict[str, object],
    integrity: CredentialIntegrityResult,
) -> dict[str, object]:
    return {
        **result,
        "integrity_values": integrity.values_scanned,
        "integrity_plaintext": integrity.totals["plaintext"],
        "integrity_undecryptable": integrity.totals["undecryptable"],
    }


def run_scheduled_credential_rotation() -> dict[str, object]:
    """Single daily entry point for integrity observation and key lifecycle."""
    with db_session_adapter.advisory_lock(
        _ROTATION_LOCK_KEY,
        timeout_ms=5000,
    ) as (db, acquired):
        if not acquired:
            return {"status": "already_running", "rotated": False}
        integrity = scan_credential_encryption_integrity(db)
        managed, key_source = _managed_key_source()
        if integrity.totals["undecryptable"] > 0:
            result: dict[str, object] = {
                "status": "blocked",
                "reason": "credential_integrity_failed",
                "rotated": False,
            }
        else:
            result = evaluate_scheduled_rotation(db)
            updated_values = result.get("updated_values")
            values_changed = (
                isinstance(updated_values, (int, float))
                and not isinstance(updated_values, bool)
                and updated_values > 0
            )
            if bool(result.get("rotated")) or values_changed:
                integrity = scan_credential_encryption_integrity(db)

        _publish_integrity_state(
            db,
            integrity,
            managed=managed,
            key_source=key_source,
            rotation_result=result,
        )
        return _attach_integrity_counts(result, integrity)
