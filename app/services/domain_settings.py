import builtins
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingCreate, DomainSettingUpdate
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
)
from app.services.response import ListResponseMixin

# `is_openbao_ref` only: this module no longer WRITES to OpenBao. A secret
# setting is stored as ciphertext and the encryption key is what lives there.
from app.services.secrets import is_openbao_ref
from app.services.setting_domain_registry import is_declared


class DomainSettings(ListResponseMixin):
    def __init__(self, domain: SettingDomain | None = None) -> None:
        self.domain = domain

    def _write_secret_ref(
        self,
        *,
        key: str,
        value_text: str | None,
        is_secret: bool,
        allow_plain_fallback: bool = False,
    ) -> str | None:
        """Encrypt a secret setting's value for storage.

        This used to write the value into OpenBao and store a
        `bao://secret/settings/<domain>#<key>` REFERENCE in the column, which
        made every read of a secret setting a network call — starter ADR-0009's
        forbidden shape — and, at three call sites that never dereferenced,
        handed a provider the literal string `bao://…` as its credential
        (RADIUS, Google, Mapbox).

        The value is now stored as `enc:<key_id>:<token>` and decrypted by the
        kernel resolver on read, with no network on that path at all. Where the
        material lives is unchanged in spirit — the encryption KEY is held from
        OpenBao at boot (`app/services/kernel_key_provider.py`), so the database
        never carries anything readable without it.

        Fails closed. `encrypt_value` raises `SettingsEncryptionError` when no
        active key is configured, and that is deliberate: the alternative is a
        plaintext credential in a column plus a log line nobody reads. The
        SQLite-only plain fallback stays for the metadata test lanes, which
        have no keyring and are not storing anything real.

        A value that is ALREADY ciphertext passes through — `encrypt_value` is
        idempotent for the active key and re-encrypts one written under a
        retired key, which is what makes rotation a rewrite rather than a
        no-op. A legacy `bao://` reference also passes through untouched: those
        are converted by `scripts/one_off/encrypt_secret_settings.py`, and
        re-encrypting the reference TEXT here would store the pointer rather
        than the secret.
        """

        from dotmac_kernel.settings_crypto import (
            SettingsEncryptionError,
            encrypt_value,
        )

        if not is_secret or value_text is None:
            return value_text
        normalized = value_text.strip()
        if not normalized:
            return value_text
        if is_openbao_ref(normalized):
            # A reference the operator supplied verbatim, or one the conversion
            # script has not reached. Storing it is what the old path did;
            # converting it is that script's job, and encrypting the pointer
            # would store the pointer instead of the secret.
            return normalized
        try:
            return encrypt_value(normalized)
        except SettingsEncryptionError:
            if allow_plain_fallback:
                return normalized
            raise HTTPException(
                status_code=500,
                detail=(
                    "No settings encryption key is configured, so a secret "
                    "setting cannot be stored. Provision "
                    "secret/settings/crypto#settings_encryption_keyring."
                ),
            ) from None

    def _allow_plain_secret_fallback(self, db: Session) -> bool:
        bind = db.get_bind()
        return getattr(getattr(bind, "dialect", None), "name", None) == "sqlite"

    @staticmethod
    def _validate_relationship_change(
        db: Session, domain: SettingDomain, key: str, value: object
    ) -> None:
        from app.services.control_relationships import (
            ControlRelationshipError,
            validate_setting_change,
        )

        try:
            validate_setting_change(db, domain, key, value)
        except ControlRelationshipError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _prepare_create_payload(
        self, db: Session, key: str, payload: DomainSettingCreate
    ) -> DomainSettingCreate:
        resolved = self._write_secret_ref(
            key=key,
            value_text=payload.value_text,
            is_secret=payload.is_secret,
            allow_plain_fallback=self._allow_plain_secret_fallback(db),
        )
        if resolved == payload.value_text:
            return payload
        data = payload.model_dump()
        data["value_text"] = resolved
        return DomainSettingCreate(**data)

    def _prepare_update_payload(
        self,
        db: Session,
        key: str,
        payload: DomainSettingUpdate,
        *,
        existing_is_secret: bool = False,
    ) -> DomainSettingUpdate:
        data = payload.model_dump(exclude_unset=True)
        effective_is_secret = bool(data.get("is_secret", existing_is_secret))
        if self.domain:
            from app.services.settings_spec import get_spec

            spec = get_spec(self.domain, key)
            if spec and spec.is_secret:
                effective_is_secret = True
        if effective_is_secret:
            data["is_secret"] = True
        resolved = self._write_secret_ref(
            key=key,
            value_text=data.get("value_text"),
            is_secret=effective_is_secret,
            allow_plain_fallback=self._allow_plain_secret_fallback(db),
        )
        if resolved is not None:
            data["value_text"] = resolved
        return DomainSettingUpdate(**data)

    def _resolve_domain(self, payload_domain: SettingDomain | None) -> SettingDomain:
        if self.domain and payload_domain and payload_domain != self.domain:
            raise HTTPException(status_code=400, detail="Setting domain mismatch")
        if self.domain:
            return self.domain
        if payload_domain:
            # `SettingDomain` is an open type, so the request schema no longer
            # rejects an unknown domain the way the enum did. This is the one
            # funnel where a request BODY becomes a row's domain, so the
            # declaration check belongs here — reaching the ORM listener
            # instead would turn a bad request into a 500. The listener stays
            # as the backstop for non-HTTP writers.
            if not is_declared(payload_domain):
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown setting domain '{payload_domain}'",
                )
            return payload_domain
        raise HTTPException(status_code=400, detail="Setting domain is required")

    def create(self, db: Session, payload: DomainSettingCreate):
        payload = self._prepare_create_payload(db, payload.key, payload)
        data = payload.model_dump()
        data["domain"] = self._resolve_domain(payload.domain)
        self._validate_relationship_change(
            db,
            data["domain"],
            payload.key,
            payload.value_json
            if payload.value_json is not None
            else payload.value_text,
        )
        setting = DomainSetting(**data)
        db.add(setting)
        db.commit()
        db.refresh(setting)
        # Invalidate cache for this setting
        return setting

    def get(self, db: Session, setting_id: str):
        setting = db.get(DomainSetting, coerce_uuid(setting_id))
        if not setting or (self.domain and setting.domain != self.domain):
            raise HTTPException(status_code=404, detail="Setting not found")
        return setting

    def list(
        self,
        db: Session,
        domain: SettingDomain | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(DomainSetting)
        effective_domain = self.domain or domain
        if effective_domain:
            query = query.filter(DomainSetting.domain == effective_domain)
        if is_active is None:
            query = query.filter(DomainSetting.is_active.is_(True))
        else:
            query = query.filter(DomainSetting.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": DomainSetting.created_at, "key": DomainSetting.key},
        )
        return apply_pagination(query, limit, offset).all()

    def update(self, db: Session, setting_id: str, payload: DomainSettingUpdate):
        setting = db.get(DomainSetting, coerce_uuid(setting_id))
        if not setting or (self.domain and setting.domain != self.domain):
            raise HTTPException(status_code=404, detail="Setting not found")
        payload = self._prepare_update_payload(
            db,
            setting.key,
            payload,
            existing_is_secret=setting.is_secret,
        )
        data = payload.model_dump(exclude_unset=True)
        if "domain" in data and data["domain"] != setting.domain:
            raise HTTPException(status_code=400, detail="Setting domain mismatch")
        pending_value = data.get(
            "value_json",
            data.get("value_text", setting.value_json or setting.value_text),
        )
        self._validate_relationship_change(
            db, setting.domain, setting.key, pending_value
        )
        for key, value in data.items():
            setattr(setting, key, value)
        db.commit()
        db.refresh(setting)
        # Invalidate cache for this setting
        return setting

    def get_optional_by_key(
        self,
        db: Session,
        key: str,
        *,
        active_only: bool = False,
    ) -> DomainSetting | None:
        """Return one scoped setting without transport-specific not-found errors."""

        if not self.domain:
            raise HTTPException(status_code=400, detail="Setting domain is required")
        query = (
            db.query(DomainSetting)
            .filter(DomainSetting.domain == self.domain)
            .filter(DomainSetting.key == key)
        )
        if active_only:
            query = query.filter(DomainSetting.is_active.is_(True))
        return query.first()

    def get_by_key(self, db: Session, key: str):
        setting = self.get_optional_by_key(db, key)
        if not setting:
            raise HTTPException(status_code=404, detail="Setting not found")
        return setting

    def upsert_by_key(self, db: Session, key: str, payload: DomainSettingUpdate):
        if not self.domain:
            raise HTTPException(status_code=400, detail="Setting domain is required")
        setting = (
            db.query(DomainSetting)
            .filter(DomainSetting.domain == self.domain)
            .filter(DomainSetting.key == key)
            .first()
        )
        payload = self._prepare_update_payload(
            db,
            key,
            payload,
            existing_is_secret=bool(setting and setting.is_secret),
        )
        if setting:
            data = payload.model_dump(exclude_unset=True)
            data.pop("domain", None)
            data.pop("key", None)
            pending_value = data.get(
                "value_json",
                data.get("value_text", setting.value_json or setting.value_text),
            )
            self._validate_relationship_change(db, self.domain, key, pending_value)
            for field, value in data.items():
                setattr(setting, field, value)
            db.commit()
            db.refresh(setting)
            # Invalidate cache for this setting
            return setting
        create_payload = DomainSettingCreate(
            domain=self.domain,
            key=key,
            value_type=payload.value_type or SettingValueType.string,
            value_text=payload.value_text,
            value_json=payload.value_json,
            is_secret=payload.is_secret or False,
            is_active=True if payload.is_active is None else payload.is_active,
        )
        # create() already invalidates cache
        return self.create(db, create_payload)

    def stage_upsert_by_key(
        self, db: Session, key: str, payload: DomainSettingUpdate
    ) -> DomainSetting:
        """Upsert one setting without completing the caller-owned transaction."""
        if not self.domain:
            raise HTTPException(status_code=400, detail="Setting domain is required")
        setting = (
            db.query(DomainSetting)
            .filter(DomainSetting.domain == self.domain)
            .filter(DomainSetting.key == key)
            .first()
        )
        payload = self._prepare_update_payload(
            db,
            key,
            payload,
            existing_is_secret=bool(setting and setting.is_secret),
        )
        if setting is not None:
            data = payload.model_dump(exclude_unset=True)
            data.pop("domain", None)
            data.pop("key", None)
            pending_value = data.get(
                "value_json",
                data.get("value_text", setting.value_json or setting.value_text),
            )
            self._validate_relationship_change(db, self.domain, key, pending_value)
            for field, value in data.items():
                setattr(setting, field, value)
        else:
            create_payload = self._prepare_create_payload(
                db,
                key,
                DomainSettingCreate(
                    domain=self.domain,
                    key=key,
                    value_type=payload.value_type or SettingValueType.string,
                    value_text=payload.value_text,
                    value_json=payload.value_json,
                    is_secret=payload.is_secret or False,
                    is_active=True if payload.is_active is None else payload.is_active,
                ),
            )
            data = create_payload.model_dump()
            data["domain"] = self._resolve_domain(create_payload.domain)
            self._validate_relationship_change(
                db,
                data["domain"],
                key,
                create_payload.value_json
                if create_payload.value_json is not None
                else create_payload.value_text,
            )
            setting = DomainSetting(**data)
            db.add(setting)
        db.flush()
        return setting

    def ensure_by_key(
        self,
        db: Session,
        key: str,
        value_type: SettingValueType,
        value_text: str | None = None,
        value_json: dict[str, Any]
        | builtins.list[Any]
        | bool
        | int
        | str
        | None = None,
        is_secret: bool = False,
    ):
        if not self.domain:
            raise HTTPException(status_code=400, detail="Setting domain is required")
        existing = (
            db.query(DomainSetting)
            .filter(DomainSetting.domain == self.domain)
            .filter(DomainSetting.key == key)
            .first()
        )
        if existing:
            return existing
        # For non-string settings we commonly store the parsed value in value_json.
        if value_type not in {SettingValueType.json, SettingValueType.boolean}:
            value_json = None
        payload = DomainSettingCreate(
            domain=self.domain,
            key=key,
            value_type=value_type,
            value_text=value_text,
            value_json=value_json,
            is_secret=is_secret,
            is_active=True,
        )
        try:
            return self.create(db, payload)
        except IntegrityError:
            db.rollback()
            raced = (
                db.query(DomainSetting)
                .filter(DomainSetting.domain == self.domain)
                .filter(DomainSetting.key == key)
                .first()
            )
            if raced:
                return raced
            raise

    def delete(self, db: Session, setting_id: str):
        setting = db.get(DomainSetting, setting_id)
        if not setting or (self.domain and setting.domain != self.domain):
            raise HTTPException(status_code=404, detail="Setting not found")
        setting.is_active = False
        db.commit()
        # Invalidate cache for this setting


settings = DomainSettings()
auth_settings = DomainSettings(SettingDomain.auth)
audit_settings = DomainSettings(SettingDomain.audit)
billing_settings = DomainSettings(SettingDomain.billing)
catalog_settings = DomainSettings(SettingDomain.catalog)
subscriber_settings = DomainSettings(SettingDomain.subscriber)
imports_settings = DomainSettings(SettingDomain.imports)
network_settings = DomainSettings(SettingDomain.network)
network_monitoring_settings = DomainSettings(SettingDomain.network_monitoring)
provisioning_settings = DomainSettings(SettingDomain.provisioning)
geocoding_settings = DomainSettings(SettingDomain.geocoding)
usage_settings = DomainSettings(SettingDomain.usage)
radius_settings = DomainSettings(SettingDomain.radius)
notification_settings = DomainSettings(SettingDomain.notification)
collections_settings = DomainSettings(SettingDomain.collections)
lifecycle_settings = DomainSettings(SettingDomain.lifecycle)
workflow_settings = DomainSettings(SettingDomain.workflow)
projects_settings = DomainSettings(SettingDomain.projects)
inventory_settings = DomainSettings(SettingDomain.inventory)
comms_settings = DomainSettings(SettingDomain.comms)
tr069_settings = DomainSettings(SettingDomain.tr069)
snmp_settings = DomainSettings(SettingDomain.snmp)
bandwidth_settings = DomainSettings(SettingDomain.bandwidth)
# `subscription_engine_settings` went with the domain: it had no spec, no
# route, no reader and no writer, and the concern moved to the dedicated
# `subscription_engine_settings` TABLE long ago. See the accessor note in
# app/models/domain_settings.py.
gis_settings = DomainSettings(SettingDomain.gis)
scheduler_settings = DomainSettings(SettingDomain.scheduler)
modules_settings = DomainSettings(SettingDomain.modules)
integration_settings = DomainSettings(SettingDomain.integration)
