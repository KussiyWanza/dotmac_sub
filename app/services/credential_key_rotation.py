"""Rotate credential-at-rest Fernet material without losing stored secrets."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, load_only

from app.models.billing import BankAccount, PaymentMethod
from app.models.catalog import AccessCredential, NasDevice
from app.models.domain_settings import DomainSetting
from app.models.network import OLTDevice, OntProfileWanService, OntUnit
from app.models.network_monitoring import NetworkDevice
from app.models.router_management import JumpHost, Router
from app.models.system_user import SystemUser
from app.models.tr069 import Tr069AcsServer
from app.services.access_credential_secret import (
    is_one_way_access_credential_secret,
)
from app.services.credential_crypto import (
    ENCRYPTED_MODEL_FIELDS,
    decrypt_credential_with_key,
    encrypt_credential_with_key,
    get_encryption_key,
    get_previous_encryption_key,
)
from app.services.network.ont_desired_config import (
    desired_config_column,
    desired_config_values_for_paths,
    rotate_desired_config_credentials,
)
from app.services.secrets import is_secret_ref

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CredentialKeyRotationResult:
    updated_records: int
    updated_values: int


@dataclass(frozen=True)
class CredentialRemediationResult:
    status: str
    execute: bool
    values_scanned: int
    plaintext_before: int
    plaintext_after: int
    undecryptable: int
    updated_records: int = 0
    updated_values: int = 0
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "execute": self.execute,
            "values_scanned": self.values_scanned,
            "plaintext_before": self.plaintext_before,
            "plaintext_after": self.plaintext_after,
            "undecryptable": self.undecryptable,
            "updated_records": self.updated_records,
            "updated_values": self.updated_values,
            "reason": self.reason,
        }


_INTEGRITY_STATES = (
    "encrypted",
    "plaintext",
    "one_way",
    "undecryptable",
    "reference",
    "empty",
)


@dataclass(frozen=True)
class CredentialIntegrityResult:
    """Redacted credential inventory suitable for observability publication."""

    counts: dict[str, dict[str, int]]
    totals: dict[str, int]
    scanned_at: datetime

    @property
    def values_scanned(self) -> int:
        return sum(self.totals.values())

    def observations(self) -> list[tuple[str, str, float]]:
        values: list[tuple[str, str, float]] = []
        for scope, states in sorted(self.counts.items()):
            for state in _INTEGRITY_STATES:
                values.append((state, scope, float(states.get(state, 0))))
        for state in _INTEGRITY_STATES:
            values.append((state, "all", float(self.totals.get(state, 0))))
        return values


_MODEL_BY_NAME: dict[str, type[Any]] = {
    "NasDevice": NasDevice,
    "NetworkDevice": NetworkDevice,
    "AccessCredential": AccessCredential,
    "OLTDevice": OLTDevice,
    "OntUnit": OntUnit,
    "OntProfileWanService": OntProfileWanService,
    "Tr069AcsServer": Tr069AcsServer,
    "PaymentMethod": PaymentMethod,
    "BankAccount": BankAccount,
    "SystemUser": SystemUser,
    "Router": Router,
    "JumpHost": JumpHost,
}

_MODEL_FIELDS: tuple[tuple[type[Any], tuple[str, ...]], ...] = tuple(
    (model, ENCRYPTED_MODEL_FIELDS[model_name])
    for model_name, model in _MODEL_BY_NAME.items()
)

_ONT_DESIRED_CONFIG_CREDENTIAL_PATHS: tuple[tuple[str, ...], ...] = (
    ("wifi", "password"),
)

_RAW_ENCRYPTED_COLUMNS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "Tr069Job.secure_payload",
        "secure_payload",
        "SELECT secure_payload FROM tr069_jobs WHERE secure_payload IS NOT NULL",
        "SELECT id, secure_payload FROM tr069_jobs WHERE secure_payload IS NOT NULL",
        "UPDATE tr069_jobs SET secure_payload = :value WHERE id = :id",
    ),
    (
        "ConnectorConfig.headers",
        "headers",
        "SELECT headers FROM connector_configs WHERE headers IS NOT NULL",
        "SELECT id, headers FROM connector_configs WHERE headers IS NOT NULL",
        "UPDATE connector_configs SET headers = :value WHERE id = :id",
    ),
    (
        "OAuthToken.access_token",
        "access_token",
        "SELECT access_token FROM oauth_tokens WHERE access_token IS NOT NULL",
        "SELECT id, access_token FROM oauth_tokens WHERE access_token IS NOT NULL",
        "UPDATE oauth_tokens SET access_token = :value WHERE id = :id",
    ),
    (
        "OAuthToken.refresh_token",
        "refresh_token",
        "SELECT refresh_token FROM oauth_tokens WHERE refresh_token IS NOT NULL",
        "SELECT id, refresh_token FROM oauth_tokens WHERE refresh_token IS NOT NULL",
        "UPDATE oauth_tokens SET refresh_token = :value WHERE id = :id",
    ),
)


def _empty_integrity_counts() -> dict[str, int]:
    return dict.fromkeys(_INTEGRITY_STATES, 0)


def classify_credential_value_state(
    value: Any,
    keys: tuple[bytes, ...],
    *,
    preserve_one_way: bool = False,
) -> str:
    if value is None or value == "":
        return "empty"
    text_value = str(value)
    if preserve_one_way and is_one_way_access_credential_secret(text_value):
        return "one_way"
    if is_secret_ref(text_value):
        return "reference"
    if text_value.startswith("plain:") or not text_value.startswith("enc:"):
        return "plaintext"
    for key in keys:
        try:
            decrypt_credential_with_key(text_value, key)
            return "encrypted"
        except ValueError:
            continue
    return "undecryptable"


def scan_credential_encryption_integrity(
    db: Session,
    *,
    now: datetime | None = None,
) -> CredentialIntegrityResult:
    """Classify every rotation-owned value without exposing record identities."""
    _validate_model_fields()
    keys = tuple(
        key
        for key in (get_encryption_key(), get_previous_encryption_key())
        if key is not None
    )
    counts: dict[str, dict[str, int]] = {}

    def register(scope: str) -> None:
        counts.setdefault(scope, _empty_integrity_counts())

    def observe(scope: str, value: Any, *, preserve_one_way: bool = False) -> None:
        register(scope)
        counts[scope][
            classify_credential_value_state(
                value, keys, preserve_one_way=preserve_one_way
            )
        ] += 1

    for model, fields in _MODEL_FIELDS:
        for field in fields:
            register(f"{model.__name__}.{field}")
        property_fields: set[str] = set()
        if model is OntUnit and "pppoe_password" in fields:
            property_fields.add("pppoe_password")
            for _path, value in desired_config_values_for_paths(
                db, (("wan", "pppoe_password"),)
            ):
                observe("OntUnit.pppoe_password", value)
        column_fields = tuple(field for field in fields if field not in property_fields)
        columns = tuple(getattr(model, field) for field in column_fields)
        if not columns:
            continue
        for values_row in db.execute(select(*columns)).all():
            for field, value in zip(column_fields, values_row, strict=True):
                observe(
                    f"{model.__name__}.{field}",
                    value,
                    preserve_one_way=(
                        model is AccessCredential and field == "secret_hash"
                    ),
                )

    nested_scopes = {
        path: ".".join(("OntUnit", "desired_config", *path))
        for path in _ONT_DESIRED_CONFIG_CREDENTIAL_PATHS
    }
    for scope in nested_scopes.values():
        register(scope)
    for path, value in desired_config_values_for_paths(
        db, _ONT_DESIRED_CONFIG_CREDENTIAL_PATHS
    ):
        observe(nested_scopes[path], value)

    settings_scope = "DomainSetting.value_text"
    register(settings_scope)
    settings_rows = db.scalars(
        select(DomainSetting)
        .where(DomainSetting.value_text.is_not(None))
        .where(DomainSetting.is_active.is_(True))
        .where(DomainSetting.is_secret.is_(True))
    ).all()
    for settings_row in settings_rows:
        observe(settings_scope, settings_row.value_text)

    connector_scope = "ConnectorConfig.auth_config"
    register(connector_scope)
    connector_rows = db.execute(
        text("SELECT auth_config FROM connector_configs WHERE auth_config IS NOT NULL")
    ).all()
    for connector_row in connector_rows:
        observe(connector_scope, connector_row.auth_config)

    for (
        scope,
        _column_name,
        integrity_sql,
        _rotation_sql,
        _update_sql,
    ) in _RAW_ENCRYPTED_COLUMNS:
        register(scope)
        rows = db.execute(text(integrity_sql)).all()
        for row in rows:
            observe(scope, row[0])

    totals = Counter(dict.fromkeys(_INTEGRITY_STATES, 0))
    for states in counts.values():
        totals.update(states)
    return CredentialIntegrityResult(
        counts=counts,
        totals={state: int(totals[state]) for state in _INTEGRITY_STATES},
        scanned_at=(now or datetime.now(UTC)).astimezone(UTC),
    )


def publish_credential_integrity_snapshot(
    integrity: CredentialIntegrityResult,
    *,
    operation: str,
    operation_status: str,
    extra_observations: tuple[tuple[str, str, float], ...] = (),
) -> bool:
    """Publish the canonical redacted credential-integrity state."""
    from app.services.observability import StateObservation, publish_state_snapshot

    observations = [
        StateObservation(signal=signal, scope=scope, value=value)
        for signal, scope, value in integrity.observations()
    ]
    observations.append(
        StateObservation(
            signal=f"{operation}_status",
            scope=operation_status,
            value=1.0,
        )
    )
    observations.extend(
        StateObservation(signal=signal, scope=scope, value=value)
        for signal, scope, value in extra_observations
    )

    if operation_status in {"blocked", "error", "incomplete"}:
        status = "error"
    elif integrity.totals["undecryptable"] > 0:
        status = "error"
    elif integrity.totals["plaintext"] > 0:
        status = "degraded"
    else:
        status = "ok"
    try:
        return publish_state_snapshot(
            "credentials",
            observations,
            status=status,
            now=integrity.scanned_at,
        )
    except Exception:
        logger.exception(
            "credential_integrity_snapshot_publish_failed operation=%s",
            operation,
        )
        return False


def _rotate_value(
    value: str | None,
    *,
    old_key: str,
    new_key: str,
) -> tuple[str | None, bool]:
    """Rotate a credential value from old key to new key.

    Returns (rotated_value, changed).

    Handles edge cases:
    - Value already encrypted with new key: returns unchanged
    - Plain/legacy values are encrypted with the new key
    - Corrupted/unrecoverable encrypted values raise ValueError
    """
    if not value:
        return value, False

    from app.services.secrets import is_secret_ref

    if is_secret_ref(value):
        return value, False

    if value.startswith("plain:"):
        plain_value = value[6:]
        rotated = encrypt_credential_with_key(plain_value, new_key)
        return rotated, rotated != value

    if value.lower().startswith("cleartext:"):
        plain_value = value[10:]
        rotated = encrypt_credential_with_key(plain_value, new_key)
        return rotated, rotated != value

    if not value.startswith("enc:"):
        rotated = encrypt_credential_with_key(value, new_key)
        return rotated, rotated != value

    # Remediation passes the active key as both old and new. The integrity scan
    # has already validated ciphertext against the active/previous keyring, so
    # remediation must leave every encrypted value untouched and only converge
    # plaintext values.
    if old_key == new_key:
        return value, False

    # Try decrypting with old key first
    try:
        decrypted_plaintext = decrypt_credential_with_key(value, old_key)
        if decrypted_plaintext is None:
            return value, False
    except ValueError:
        # Old key didn't work - check if already encrypted with new key
        try:
            decrypt_credential_with_key(value, new_key)
            # Successfully decrypted with new key - already rotated
            logger.debug("Value already encrypted with new key, skipping")
            return value, False
        except ValueError:
            raise ValueError(
                "Cannot decrypt value with either old or new key"
            ) from None

    # Re-encrypt with new key
    rotated = encrypt_credential_with_key(decrypted_plaintext, new_key)
    if rotated == value:
        return value, False
    return rotated, True


def _record_identity(row: Any) -> str:
    row_id = getattr(row, "id", None)
    return str(row_id) if row_id is not None else "<no-id>"


def _validate_model_fields() -> None:
    for model, fields in _MODEL_FIELDS:
        columns = model.__table__.columns
        for field in fields:
            if field not in columns and not hasattr(model, field):
                raise ValueError(
                    f"{model.__name__}.{field} is not a mapped column or model attribute"
                )


def _rotate_model_fields(
    db: Session,
    model: type[Any],
    fields: tuple[str, ...],
    *,
    old_key: str,
    new_key: str,
) -> tuple[int, int]:
    updated_records = 0
    updated_values = 0
    columns = model.__table__.columns
    column_lengths = {
        field: getattr(columns[field].type, "length", None)
        for field in fields
        if field in columns
    }
    logger.debug("Rotating credential fields for model %s", model.__name__)
    load_attributes = [model.id]
    for field in fields:
        if field in columns:
            load_attributes.append(getattr(model, field))
        elif model is OntUnit and field == "pppoe_password":
            load_attributes.append(desired_config_column())
    query = select(model).options(load_only(*load_attributes))
    for row in db.scalars(query).all():
        row_changed = False
        for field in fields:
            current = getattr(row, field, None)
            if (
                model is AccessCredential
                and field == "secret_hash"
                and is_one_way_access_credential_secret(current)
            ):
                continue
            try:
                rotated, changed = _rotate_value(
                    current, old_key=old_key, new_key=new_key
                )
            except ValueError as exc:
                raise ValueError(
                    f"Failed to rotate {model.__name__}.{field} "
                    f"id={_record_identity(row)}"
                ) from exc
            if not changed:
                continue
            max_length = column_lengths.get(field)
            if (
                max_length is not None
                and isinstance(rotated, str)
                and len(rotated) > max_length
            ):
                raise ValueError(
                    f"Rotated value for {model.__name__}.{field} id={_record_identity(row)} "
                    f"exceeds column length {max_length}"
                )
            setattr(row, field, rotated)
            row_changed = True
            updated_values += 1
        if row_changed:
            updated_records += 1
    return updated_records, updated_values


def _rotate_ont_desired_config_credentials(
    db: Session,
    *,
    old_key: str,
    new_key: str,
) -> tuple[int, int]:
    updated_records = 0
    updated_values = 0
    query = select(OntUnit).options(load_only(OntUnit.id, desired_config_column()))
    for ont in db.scalars(query).all():

        def rotate_nested_value(
            path: tuple[str, ...], current: Any
        ) -> tuple[Any, bool]:
            try:
                return _rotate_value(current, old_key=old_key, new_key=new_key)
            except ValueError as exc:
                dotted_path = ".".join(("desired_config", *path))
                raise ValueError(
                    f"Failed to rotate OntUnit.{dotted_path} id={_record_identity(ont)}"  # noqa: B023
                ) from exc

        changed_values = rotate_desired_config_credentials(
            ont,
            _ONT_DESIRED_CONFIG_CREDENTIAL_PATHS,
            rotate_nested_value,
        )
        if changed_values:
            updated_records += 1
            updated_values += changed_values
    return updated_records, updated_values


def _rotate_domain_settings(
    db: Session, *, old_key: str, new_key: str
) -> tuple[int, int]:
    updated_records = 0
    updated_values = 0
    rows = list(
        db.scalars(
            select(DomainSetting)
            .where(DomainSetting.value_text.is_not(None))
            .where(DomainSetting.is_active.is_(True))
            .where(DomainSetting.is_secret.is_(True))
        ).all()
    )
    for row in rows:
        current = str(row.value_text or "")
        # Note: _rotate_value now checks for enc: prefix internally
        try:
            rotated, changed = _rotate_value(current, old_key=old_key, new_key=new_key)
        except ValueError as exc:
            raise ValueError(
                f"Failed to rotate DomainSetting {row.domain.value}.{row.key}"
            ) from exc
        if not changed:
            continue
        row.value_text = rotated
        updated_records += 1
        updated_values += 1
    return updated_records, updated_values


def _rotate_connector_auth_config(
    db: Session, *, old_key: str, new_key: str
) -> tuple[int, int]:
    """Rotate ``ConnectorConfig.auth_config`` (a whole-blob EncryptedJSON column).

    The column is TEXT, so the raw stored value is a plain ``enc:``/``plain:`` (or
    legacy plaintext) string on every dialect. We read/write it via straight SQL —
    not the ORM — so the EncryptedJSON type's ambient-key encode/decode never runs
    and we control the old/new keys explicitly. Legacy plaintext blobs are encrypted
    in passing (``_rotate_value`` encrypts non-``enc:`` input with the new key).
    """
    updated_records = 0
    updated_values = 0
    rows = db.execute(
        text(
            "SELECT id, auth_config FROM connector_configs "
            "WHERE auth_config IS NOT NULL"
        )
    ).all()
    for row in rows:
        raw = row.auth_config
        if not isinstance(raw, str) or not raw:
            continue
        rotated, changed = _rotate_value(raw, old_key=old_key, new_key=new_key)
        if not changed:
            continue
        db.execute(
            text("UPDATE connector_configs SET auth_config = :v WHERE id = :id"),
            {"v": rotated, "id": row.id},
        )
        updated_records += 1
        updated_values += 1
    return updated_records, updated_values


def _rotate_raw_encrypted_columns(
    db: Session, *, old_key: str, new_key: str
) -> tuple[int, int]:
    """Rotate whole-blob/string encrypted columns without ORM decryption."""
    updated_records = 0
    updated_values = 0
    for (
        _scope,
        column_name,
        _integrity_sql,
        rotation_sql,
        update_sql,
    ) in _RAW_ENCRYPTED_COLUMNS:
        rows = db.execute(text(rotation_sql)).mappings()
        for row in rows:
            raw = row[column_name]
            if not isinstance(raw, str) or not raw:
                continue
            rotated, changed = _rotate_value(raw, old_key=old_key, new_key=new_key)
            if not changed:
                continue
            db.execute(text(update_sql), {"value": rotated, "id": row["id"]})
            updated_records += 1
            updated_values += 1
    return updated_records, updated_values


def rotate_credential_encryption_material(
    db: Session,
    *,
    old_key: str,
    new_key: str,
    commit: bool = True,
) -> CredentialKeyRotationResult:
    """Re-encrypt all known credential-at-rest values with a new Fernet key."""
    _validate_model_fields()
    logger.info("Starting credential encryption material rotation")
    updated_records = 0
    updated_values = 0

    for model, fields in _MODEL_FIELDS:
        records, values = _rotate_model_fields(
            db, model, fields, old_key=old_key, new_key=new_key
        )
        updated_records += records
        updated_values += values

    records, values = _rotate_ont_desired_config_credentials(
        db, old_key=old_key, new_key=new_key
    )
    updated_records += records
    updated_values += values

    records, values = _rotate_domain_settings(db, old_key=old_key, new_key=new_key)
    updated_records += records
    updated_values += values

    records, values = _rotate_connector_auth_config(
        db, old_key=old_key, new_key=new_key
    )
    updated_records += records
    updated_values += values

    records, values = _rotate_raw_encrypted_columns(
        db, old_key=old_key, new_key=new_key
    )
    updated_records += records
    updated_values += values

    if commit:
        db.commit()
    else:
        db.flush()

    logger.info(
        "Finished credential encryption material rotation: records=%d values=%d commit=%s",
        updated_records,
        updated_values,
        commit,
    )
    return CredentialKeyRotationResult(
        updated_records=updated_records,
        updated_values=updated_values,
    )


def remediate_credential_encryption(
    db: Session,
    *,
    execute: bool = False,
) -> CredentialRemediationResult:
    """Converge plaintext credential values through the canonical inventory."""
    before = scan_credential_encryption_integrity(db)
    plaintext_before = int(before.totals["plaintext"])
    undecryptable = int(before.totals["undecryptable"])

    if undecryptable > 0:
        result = CredentialRemediationResult(
            status="blocked",
            execute=execute,
            values_scanned=before.values_scanned,
            plaintext_before=plaintext_before,
            plaintext_after=plaintext_before,
            undecryptable=undecryptable,
            reason="undecryptable_credentials",
        )
        publish_credential_integrity_snapshot(
            before,
            operation="remediation",
            operation_status=result.status,
        )
        return result

    active_key = get_encryption_key()
    if active_key is None:
        result = CredentialRemediationResult(
            status="blocked",
            execute=execute,
            values_scanned=before.values_scanned,
            plaintext_before=plaintext_before,
            plaintext_after=plaintext_before,
            undecryptable=0,
            reason="encryption_key_missing",
        )
        publish_credential_integrity_snapshot(
            before,
            operation="remediation",
            operation_status=result.status,
        )
        return result

    if not execute:
        result = CredentialRemediationResult(
            status="dry_run",
            execute=False,
            values_scanned=before.values_scanned,
            plaintext_before=plaintext_before,
            plaintext_after=plaintext_before,
            undecryptable=0,
        )
        publish_credential_integrity_snapshot(
            before,
            operation="remediation",
            operation_status=result.status,
        )
        return result

    key_text = (
        active_key.decode("ascii") if isinstance(active_key, bytes) else str(active_key)
    )
    try:
        updated = rotate_credential_encryption_material(
            db,
            old_key=key_text,
            new_key=key_text,
            commit=True,
        )
    except Exception:
        db.rollback()
        publish_credential_integrity_snapshot(
            before,
            operation="remediation",
            operation_status="error",
        )
        raise

    after = scan_credential_encryption_integrity(db)
    plaintext_after = int(after.totals["plaintext"])
    result = CredentialRemediationResult(
        status="completed" if plaintext_after == 0 else "incomplete",
        execute=True,
        values_scanned=after.values_scanned,
        plaintext_before=plaintext_before,
        plaintext_after=plaintext_after,
        undecryptable=int(after.totals["undecryptable"]),
        updated_records=updated.updated_records,
        updated_values=updated.updated_values,
        reason=None if plaintext_after == 0 else "plaintext_credentials_remain",
    )
    publish_credential_integrity_snapshot(
        after,
        operation="remediation",
        operation_status=result.status,
    )
    return result
