import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from fastapi import HTTPException
from sqlalchemy import (
    Column,
    Integer,
    String,
    case,
    delete,
    insert,
    or_,
    select,
)
from sqlalchemy.orm import Session

from app.models.catalog import (
    AccessCredential,
    NasDevice,
    Subscription,
    SubscriptionStatus,
)
from app.models.connector import ConnectorConfig
from app.models.domain_settings import SettingDomain
from app.models.radius import (
    RadiusClient,
    RadiusServer,
    RadiusSyncJob,
    RadiusSyncRun,
    RadiusSyncStatus,
    RadiusUser,
)
from app.models.subscriber import Subscriber
from app.schemas.radius import (
    RadiusClientCreate,
    RadiusClientUpdate,
    RadiusServerCreate,
    RadiusServerUpdate,
    RadiusSyncJobCreate,
    RadiusSyncJobUpdate,
)
from app.services import radius_dsn, settings_spec
from app.services.access_credential_secret import (
    AccessCredentialSecretFormat,
    classify_access_credential_secret,
    explicit_cleartext_value,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    validate_enum,
)
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.external_radius_targets import (
    active_external_radius_targets,
    external_radius_table,
    get_external_engine,
    sanitize_table_identifier,
)
from app.services.external_radius_targets import (
    authoritative_external_radius_db_url as _authoritative_external_radius_db_url,
)
from app.services.observability import record_task_run
from app.services.radius_access_state import (
    ACTIVE_STATUSES,
    BLOCKED_STATUSES,
    TERMINATED_STATUSES,
)
from app.services.response import ListResponseMixin
from app.services.secrets import resolve_secret

logger = logging.getLogger(__name__)


RADIUS_SYNC_ELIGIBLE_STATUSES = tuple(
    ACTIVE_STATUSES | BLOCKED_STATUSES | TERMINATED_STATUSES
)
# Compatibility aliases for read/NAS adapters. Runtime target ownership lives
# in ``external_radius_targets``; projection writes live in radius_population.
_get_external_engine = get_external_engine
_external_radius_table = external_radius_table


def _projection_result_count(result: dict[str, object], key: str) -> int:
    """Read an integer counter from a structured projection result."""
    value = result.get(key, 0)
    return value if isinstance(value, int) else 0


def _require_projection_result(result: dict[str, object]) -> None:
    if result.get("projection_complete") is not True:
        raise RadiusConnectivityProjectionIncomplete(
            "RADIUS projection contains an unbuildable login"
        )


class ConnectivityProjectionDisposition(StrEnum):
    """Stable result of projecting one subscription's connectivity."""

    projected = "projected"
    ineligible_subscription = "ineligible_subscription"
    missing_login = "missing_login"
    target_unavailable = "target_unavailable"
    unbuildable_login = "unbuildable_login"


class RadiusConnectivityProjectionIncomplete(RuntimeError):
    """A subscription's required RADIUS projection did not converge."""


@dataclass(frozen=True)
class SubscriptionConnectivityOutcome:
    """Typed evidence that local and external connectivity were projected."""

    subscription_id: str
    disposition: ConnectivityProjectionDisposition
    radius_clients_changed: int = 0
    radius_users_changed: int = 0
    external_nas_synced: int = 0
    external_credentials_synced: int = 0
    requested_logins: int = 0
    projected_logins: int = 0
    projection_targets: int = 0

    @property
    def ok(self) -> bool:
        return self.disposition is ConnectivityProjectionDisposition.projected

    def as_dict(self) -> dict[str, int | bool | str]:
        return {
            "ok": self.ok,
            "subscription_id": self.subscription_id,
            "disposition": self.disposition.value,
            "radius_clients_changed": self.radius_clients_changed,
            "radius_users_changed": self.radius_users_changed,
            "external_nas_synced": self.external_nas_synced,
            "external_credentials_synced": self.external_credentials_synced,
            "requested_logins": self.requested_logins,
            "projected_logins": self.projected_logins,
            "projection_targets": self.projection_targets,
        }

    def require_projected(self) -> "SubscriptionConnectivityOutcome":
        if not self.ok:
            raise RadiusConnectivityProjectionIncomplete(
                f"RADIUS connectivity projection incomplete: {self.disposition.value}"
            )
        return self


def authoritative_external_radius_db_url(db: Session) -> str | None:
    return _authoritative_external_radius_db_url(db)


def _safe_decrypt_credential(value: str | None, *, label: str) -> str | None:
    """Decrypt a stored credential, returning None instead of raising when it
    cannot be decrypted (e.g. encrypted under a retired/unavailable key).

    Lets RADIUS sync skip a single undecryptable record rather than aborting the
    entire job — a handful of dead credentials should not keep the whole sync red.
    """
    try:
        return decrypt_credential(value)
    except ValueError:
        logger.warning(
            "Skipping RADIUS sync for %s: credential cannot be decrypted with the "
            "current key (encrypted under a retired/unavailable key)",
            label,
        )
        return None


def _external_password_row(
    credential: AccessCredential,
    *,
    default_attribute: str,
    default_op: str,
) -> tuple[str, str, str] | None:
    secret_hash = str(credential.secret_hash or "").strip()
    if not secret_hash:
        return None
    secret_format = classify_access_credential_secret(secret_hash)
    if secret_format == AccessCredentialSecretFormat.encrypted:
        cleartext = _safe_decrypt_credential(
            secret_hash, label=f"user {credential.username}"
        )
        if cleartext is None:
            # Undecryptable ciphertext (retired key) — skip rather than abort.
            return None
        return ("Cleartext-Password", ":=", cleartext or "")
    if secret_format == AccessCredentialSecretFormat.explicit_cleartext:
        return (
            "Cleartext-Password",
            ":=",
            explicit_cleartext_value(secret_hash),
        )
    if secret_format == AccessCredentialSecretFormat.crypt_hash:
        return ("Crypt-Password", ":=", secret_hash)
    if secret_format == AccessCredentialSecretFormat.pbkdf2_hash:
        logger.warning(
            "Skipping external RADIUS password sync for %s: unsupported legacy PBKDF2 service secret",
            credential.username,
        )
        return None
    if secret_format == AccessCredentialSecretFormat.opaque_hash:
        logger.warning(
            "Skipping external RADIUS password sync for %s: "
            "opaque hash detected (likely migration artifact, not cleartext)",
            credential.username,
        )
        return None
    return (default_attribute, default_op, secret_hash)


def _radius_sync_subscription_for_subscriber(
    db: Session, subscriber_id: Any
) -> Subscription | None:
    stmt = (
        select(Subscription)
        .where(Subscription.subscriber_id == subscriber_id)
        .where(Subscription.status.in_(RADIUS_SYNC_ELIGIBLE_STATUSES))
        .order_by(
            # Prefer an active subscription when the subscriber has more than
            # one eligible (migrated duplicate-service customers carry active +
            # canceled/suspended rows). Without this, the latest-by-date pick
            # could be a canceled subscription, so RADIUS state — including
            # additional-IP Framed-Routes — gets synced from the wrong service.
            case((Subscription.status == SubscriptionStatus.active, 0), else_=1),
            Subscription.start_at.desc().nullslast(),
            Subscription.created_at.desc(),
        )
    )
    return db.scalars(stmt).first()


def _coerce_int_setting(value: object) -> int | None:
    # settings_spec.resolve_value() is intentionally loose-typed (object).
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


def _is_opaque_radius_password(value: str | None) -> bool:
    return (
        classify_access_credential_secret(value)
        == AccessCredentialSecretFormat.opaque_hash
    )


def _normalize_imported_radius_secret(
    attribute: str | None,
    value: str | None,
) -> tuple[str | None, bool]:
    attr = str(attribute or "").strip().lower()
    raw_value = str(value or "").strip()
    if not raw_value:
        return None, False
    if attr == "crypt-password":
        return raw_value, True
    if attr == "cleartext-password":
        if _is_opaque_radius_password(raw_value):
            return None, False
        return encrypt_credential(raw_value), True
    return None, False


def _dedupe_single_subscriber(rows: list[Subscriber]) -> list[Subscriber]:
    deduped: dict[str, Subscriber] = {}
    for row in rows:
        deduped[str(row.id)] = row
    return list(deduped.values())


def _read_external_radius_credentials(config: dict) -> list[dict[str, str]]:
    radcheck = config["radcheck_table"]
    engine = _get_external_engine(config["db_url"])
    radcheck_table = _external_radius_table(
        radcheck,
        Column("username", String),
        Column("attribute", String),
        Column("op", String),
        Column("value", String),
    )
    query = (
        select(
            radcheck_table.c.username,
            radcheck_table.c.attribute,
            radcheck_table.c.op,
            radcheck_table.c.value,
        )
        .where(
            or_(
                radcheck_table.c.attribute.ilike("cleartext-password"),
                radcheck_table.c.attribute.ilike("crypt-password"),
            )
        )
        .order_by(radcheck_table.c.username)
    )
    selected: dict[str, dict[str, str]] = {}
    priority = {"cleartext-password": 2, "crypt-password": 1}
    with engine.begin() as conn:
        for row in conn.execute(query):
            username = str(row.username or "").strip()
            if not username:
                continue
            attribute = str(row.attribute or "").strip()
            current = selected.get(username)
            current_priority = (
                priority.get(str(current.get("attribute") or "").lower(), 0)
                if current
                else 0
            )
            new_priority = priority.get(attribute.lower(), 0)
            if current is None or new_priority > current_priority:
                selected[username] = {
                    "username": username,
                    "attribute": attribute,
                    "op": str(row.op or "").strip(),
                    "value": str(row.value or "").strip(),
                }
    return list(selected.values())


def _mask_external_radius_value(attribute: str, value: str) -> str:
    attr = str(attribute or "").strip().lower()
    text = str(value or "")
    if attr in {"cleartext-password", "crypt-password"}:
        if not text:
            return ""
        return "••••••••"
    return text


def _read_external_radius_user_records(
    config: dict, username: str
) -> dict[str, list[dict[str, str]]]:
    username_text = str(username or "").strip()
    if not username_text:
        return {"radcheck": [], "radreply": [], "radusergroup": []}

    radcheck = config["radcheck_table"]
    radreply = config["radreply_table"]
    radusergroup = config["radusergroup_table"]
    engine = _get_external_engine(config["db_url"])
    radcheck_table = _external_radius_table(
        radcheck,
        Column("username", String),
        Column("attribute", String),
        Column("op", String),
        Column("value", String),
    )
    radreply_table = _external_radius_table(
        radreply,
        Column("username", String),
        Column("attribute", String),
        Column("op", String),
        Column("value", String),
    )
    radusergroup_table = _external_radius_table(
        radusergroup,
        Column("username", String),
        Column("groupname", String),
        Column("priority", Integer),
    )
    result: dict[str, list[dict[str, str]]] = {
        "radcheck": [],
        "radreply": [],
        "radusergroup": [],
    }

    with engine.begin() as conn:
        for row in conn.execute(
            select(
                radcheck_table.c.username,
                radcheck_table.c.attribute,
                radcheck_table.c.op,
                radcheck_table.c.value,
            )
            .where(radcheck_table.c.username == username_text)
            .order_by(radcheck_table.c.attribute, radcheck_table.c.value)
        ):
            result["radcheck"].append(
                {
                    "username": str(row.username or "").strip(),
                    "attribute": str(row.attribute or "").strip(),
                    "op": str(row.op or "").strip(),
                    "value": _mask_external_radius_value(
                        str(row.attribute or "").strip(),
                        str(row.value or "").strip(),
                    ),
                }
            )
        for row in conn.execute(
            select(
                radreply_table.c.username,
                radreply_table.c.attribute,
                radreply_table.c.op,
                radreply_table.c.value,
            )
            .where(radreply_table.c.username == username_text)
            .order_by(radreply_table.c.attribute, radreply_table.c.value)
        ):
            result["radreply"].append(
                {
                    "username": str(row.username or "").strip(),
                    "attribute": str(row.attribute or "").strip(),
                    "op": str(row.op or "").strip(),
                    "value": str(row.value or "").strip(),
                }
            )
        for row in conn.execute(
            select(
                radusergroup_table.c.username,
                radusergroup_table.c.groupname,
                radusergroup_table.c.priority,
            )
            .where(radusergroup_table.c.username == username_text)
            .order_by(radusergroup_table.c.priority, radusergroup_table.c.groupname)
        ):
            result["radusergroup"].append(
                {
                    "username": str(row.username or "").strip(),
                    "groupname": str(row.groupname or "").strip(),
                    "priority": str(
                        row.priority if row.priority is not None else ""
                    ).strip(),
                }
            )
    return result


def read_external_radius_rows_for_username(
    db: Session, username: str
) -> list[dict[str, Any]]:
    username_text = str(username or "").strip()
    if not username_text:
        return []

    jobs = list(
        db.scalars(
            select(RadiusSyncJob)
            .where(RadiusSyncJob.is_active.is_(True))
            .where(RadiusSyncJob.connector_config_id.isnot(None))
            .where(
                or_(
                    RadiusSyncJob.sync_users.is_(True),
                    RadiusSyncJob.sync_nas_clients.is_(True),
                )
            )
            .order_by(RadiusSyncJob.name.asc())
        ).all()
    )

    rows: list[dict[str, Any]] = []
    for job in jobs:
        config = _external_db_config(db, job)
        if not config:
            rows.append(
                {
                    "job_name": job.name,
                    "server_name": job.server.name if job.server else "",
                    "available": False,
                    "error": "External connector is not fully configured.",
                    "radcheck": [],
                    "radreply": [],
                    "radusergroup": [],
                }
            )
            continue
        try:
            external_rows = _read_external_radius_user_records(config, username_text)
            rows.append(
                {
                    "job_name": job.name,
                    "server_name": job.server.name if job.server else "",
                    "available": True,
                    "error": "",
                    **external_rows,
                }
            )
        except Exception as exc:
            logger.warning(
                "Failed to read external RADIUS rows for %s via job %s",
                username_text,
                job.id,
                exc_info=True,
            )
            rows.append(
                {
                    "job_name": job.name,
                    "server_name": job.server.name if job.server else "",
                    "available": False,
                    "error": str(exc),
                    "radcheck": [],
                    "radreply": [],
                    "radusergroup": [],
                }
            )
    return rows


def import_access_credentials_from_external_radius(
    db: Session,
    *,
    config: dict | None = None,
) -> dict[str, Any]:
    external_config = config
    if external_config is None:
        targets = active_external_radius_targets(db, capability="users")
        external_config = targets[0] if len(targets) == 1 else None
    if not external_config:
        raise ValueError(
            "Exactly one DB-configured external RADIUS user target is required."
        )

    imported_rows = _read_external_radius_credentials(external_config)

    existing_credentials = list(db.scalars(select(AccessCredential)).all())
    existing_by_username = {
        str(credential.username).strip().lower(): credential
        for credential in existing_credentials
        if getattr(credential, "username", None)
    }

    subscriptions = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.login.isnot(None))
            .order_by(Subscription.created_at.desc())
        ).all()
    )
    subscriptions_by_login: dict[str, list[Subscriber]] = {}
    for subscription in subscriptions:
        login = str(subscription.login or "").strip().lower()
        if not login:
            continue
        linked_subscriber = db.get(Subscriber, subscription.subscriber_id)
        if linked_subscriber and linked_subscriber.is_active:
            subscriptions_by_login.setdefault(login, []).append(linked_subscriber)

    subscribers = list(
        db.scalars(select(Subscriber).where(Subscriber.is_active.is_(True))).all()
    )
    subscribers_by_number: dict[str, list[Subscriber]] = {}
    subscribers_by_account_number: dict[str, list[Subscriber]] = {}
    for subscriber_record in subscribers:
        subscriber_number = (
            str(subscriber_record.subscriber_number or "").strip().lower()
        )
        if subscriber_number:
            subscribers_by_number.setdefault(subscriber_number, []).append(
                subscriber_record
            )
        account_number = str(subscriber_record.account_number or "").strip().lower()
        if account_number:
            subscribers_by_account_number.setdefault(account_number, []).append(
                subscriber_record
            )

    created = 0
    updated = 0
    reactivated = 0
    secrets_imported = 0
    secrets_skipped = 0
    matched_existing_credential = 0
    matched_subscription_login = 0
    matched_subscriber_number = 0
    matched_account_number = 0
    unmatched: list[str] = []
    conflicts: list[str] = []

    for row in imported_rows:
        username = str(row["username"]).strip()
        username_key = username.lower()
        credential = existing_by_username.get(username_key)
        subscriber: Subscriber | None = None
        match_source = "none"

        if credential is not None:
            subscriber = db.get(Subscriber, credential.subscriber_id)
            match_source = "existing_credential"
            matched_existing_credential += 1
        else:
            login_matches = _dedupe_single_subscriber(
                subscriptions_by_login.get(username_key, [])
            )
            if len(login_matches) == 1:
                subscriber = login_matches[0]
                match_source = "subscription_login"
                matched_subscription_login += 1
            elif len(login_matches) > 1:
                conflicts.append(username)
                continue
            else:
                number_matches = _dedupe_single_subscriber(
                    subscribers_by_number.get(username_key, [])
                )
                if len(number_matches) == 1:
                    subscriber = number_matches[0]
                    match_source = "subscriber_number"
                    matched_subscriber_number += 1
                elif len(number_matches) > 1:
                    conflicts.append(username)
                    continue
                else:
                    account_matches = _dedupe_single_subscriber(
                        subscribers_by_account_number.get(username_key, [])
                    )
                    if len(account_matches) == 1:
                        subscriber = account_matches[0]
                        match_source = "account_number"
                        matched_account_number += 1
                    elif len(account_matches) > 1:
                        conflicts.append(username)
                        continue

        if subscriber is None:
            unmatched.append(username)
            continue

        imported_secret, secret_usable = _normalize_imported_radius_secret(
            row.get("attribute"),
            row.get("value"),
        )
        if secret_usable:
            secrets_imported += 1
        else:
            secrets_skipped += 1

        if credential is None:
            credential = AccessCredential(
                subscriber_id=subscriber.id,
                username=username,
                secret_hash=imported_secret,
                is_active=True,
            )
            db.add(credential)
            db.flush()
            existing_by_username[username_key] = credential
            created += 1
        else:
            changed = False
            if credential.subscriber_id != subscriber.id:
                conflicts.append(username)
                continue
            if credential.username != username:
                credential.username = username
                changed = True
            if credential.is_active is not True:
                credential.is_active = True
                reactivated += 1
                changed = True
            if imported_secret and credential.secret_hash != imported_secret:
                credential.secret_hash = imported_secret
                changed = True
            if changed:
                updated += 1

        if match_source in {"subscriber_number", "account_number"}:
            matched_subscription: Subscription | None = (
                _radius_sync_subscription_for_subscriber(db, subscriber.id)
            )
            if (
                matched_subscription
                and not str(matched_subscription.login or "").strip()
            ):
                matched_subscription.login = username

    db.commit()

    return {
        "scanned": len(imported_rows),
        "created": created,
        "updated": updated,
        "reactivated": reactivated,
        "matched_existing_credential": matched_existing_credential,
        "matched_subscription_login": matched_subscription_login,
        "matched_subscriber_number": matched_subscriber_number,
        "matched_account_number": matched_account_number,
        "secrets_imported": secrets_imported,
        "secrets_skipped": secrets_skipped,
        "unmatched": len(unmatched),
        "conflicts": len(conflicts),
        "unmatched_examples": unmatched[:10],
        "conflict_examples": conflicts[:10],
    }


class RadiusServers(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: RadiusServerCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "auth_port" not in fields_set:
            default_auth_port = settings_spec.resolve_value(
                db, SettingDomain.radius, "default_auth_port"
            )
            auth_port = _coerce_int_setting(default_auth_port)
            if auth_port is not None:
                data["auth_port"] = auth_port
        if "acct_port" not in fields_set:
            default_acct_port = settings_spec.resolve_value(
                db, SettingDomain.radius, "default_acct_port"
            )
            acct_port = _coerce_int_setting(default_acct_port)
            if acct_port is not None:
                data["acct_port"] = acct_port
        server = RadiusServer(**data)
        db.add(server)
        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def get(db: Session, server_id: str):
        server = db.get(RadiusServer, server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Radius server not found")
        return server

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        stmt = select(RadiusServer)
        if is_active is None:
            stmt = stmt.where(RadiusServer.is_active.is_(True))
        else:
            stmt = stmt.where(RadiusServer.is_active == is_active)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": RadiusServer.created_at, "name": RadiusServer.name},
        )
        return list(db.scalars(apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def update(db: Session, server_id: str, payload: RadiusServerUpdate):
        server = db.get(RadiusServer, server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Radius server not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(server, key, value)
        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def delete(db: Session, server_id: str):
        server = db.get(RadiusServer, server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Radius server not found")
        server.is_active = False
        db.commit()


class RadiusClients(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: RadiusClientCreate):
        client = RadiusClient(**payload.model_dump())
        db.add(client)
        db.commit()
        db.refresh(client)
        return client

    @staticmethod
    def get(db: Session, client_id: str):
        client = db.get(RadiusClient, client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Radius client not found")
        return client

    @staticmethod
    def list(
        db: Session,
        server_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        stmt = select(RadiusClient)
        if server_id:
            stmt = stmt.where(RadiusClient.server_id == server_id)
        if is_active is None:
            stmt = stmt.where(RadiusClient.is_active.is_(True))
        else:
            stmt = stmt.where(RadiusClient.is_active == is_active)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {
                "created_at": RadiusClient.created_at,
                "client_ip": RadiusClient.client_ip,
            },
        )
        return list(db.scalars(apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def update(db: Session, client_id: str, payload: RadiusClientUpdate):
        client = db.get(RadiusClient, client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Radius client not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(client, key, value)
        db.commit()
        db.refresh(client)
        return client

    @staticmethod
    def delete(db: Session, client_id: str):
        client = db.get(RadiusClient, client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Radius client not found")
        client.is_active = False
        db.commit()


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _radius_client_ip_for_nas(nas_device: NasDevice) -> str:
    return (
        nas_device.nas_ip or nas_device.management_ip or nas_device.ip_address or ""
    ).strip()


def radius_client_ip_for_nas(nas_device: NasDevice) -> str:
    """Public RADIUS client identity projection for a NAS device."""
    return _radius_client_ip_for_nas(nas_device)


def _active_radius_servers(db: Session) -> list[RadiusServer]:
    return list(
        db.scalars(
            select(RadiusServer)
            .where(RadiusServer.is_active.is_(True))
            .order_by(RadiusServer.created_at.asc())
        ).all()
    )


# Compatibility URL helpers. Runtime target authority lives in
# ``external_radius_targets``; these normalizers remain for API inputs.
_normalize_external_db_url = radius_dsn.normalize_external_db_url
_container_safe_external_db_url = radius_dsn.container_safe_external_db_url


def _active_external_sync_configs(db: Session) -> list[dict]:
    return active_external_radius_targets(db)


def ensure_radius_clients_for_nas(db: Session, nas_device: NasDevice) -> int:
    """Ensure active RadiusClient rows exist for a NAS on all active servers."""
    client_ip = _radius_client_ip_for_nas(nas_device)
    if not client_ip or not nas_device.shared_secret:
        return 0

    decrypted_secret = _safe_decrypt_credential(
        nas_device.shared_secret, label=f"NAS {nas_device.name}"
    )
    if decrypted_secret is None:
        return 0
    raw_secret = resolve_secret(decrypted_secret)
    if not raw_secret:
        return 0

    servers = _active_radius_servers(db)
    if not servers:
        return 0

    secret_hash = _hash_secret(raw_secret)
    changed = 0
    for server in servers:
        existing = db.scalars(
            select(RadiusClient)
            .where(RadiusClient.server_id == server.id)
            .where(
                or_(
                    RadiusClient.nas_device_id == nas_device.id,
                    RadiusClient.client_ip == client_ip,
                )
            )
        ).first()
        if existing:
            updated = False
            if existing.nas_device_id != nas_device.id:
                existing.nas_device_id = nas_device.id
                updated = True
            if existing.client_ip != client_ip:
                existing.client_ip = client_ip
                updated = True
            if existing.shared_secret_hash != secret_hash:
                existing.shared_secret_hash = secret_hash
                updated = True
            if existing.description != nas_device.name:
                existing.description = nas_device.name
                updated = True
            if existing.is_active is not True:
                existing.is_active = True
                updated = True
            if updated:
                changed += 1
            continue

        db.add(
            RadiusClient(
                server_id=server.id,
                nas_device_id=nas_device.id,
                client_ip=client_ip,
                shared_secret_hash=secret_hash,
                description=nas_device.name,
                is_active=True,
            )
        )
        changed += 1
    return changed


def ensure_radius_users_for_subscription(
    db: Session, subscription: Subscription
) -> int:
    """Ensure internal RadiusUser rows exist for active credentials on a subscription."""
    if subscription.status != SubscriptionStatus.active:
        return 0

    credentials = list(
        db.scalars(
            select(AccessCredential)
            .where(AccessCredential.subscriber_id == subscription.subscriber_id)
            .where(AccessCredential.is_active.is_(True))
            .order_by(
                AccessCredential.updated_at.desc(),
                AccessCredential.created_at.desc(),
            )
        ).all()
    )
    if not credentials:
        return 0

    changed = 0
    for credential in credentials:
        existing_user = db.scalars(
            select(RadiusUser).where(RadiusUser.access_credential_id == credential.id)
        ).first()
        profile_id = credential.radius_profile_id or subscription.radius_profile_id
        if existing_user:
            updated = False
            if existing_user.subscription_id != subscription.id:
                existing_user.subscription_id = subscription.id
                updated = True
            if existing_user.subscriber_id != subscription.subscriber_id:
                existing_user.subscriber_id = subscription.subscriber_id
                updated = True
            if existing_user.username != credential.username:
                existing_user.username = credential.username
                updated = True
            if existing_user.secret_hash != credential.secret_hash:
                existing_user.secret_hash = credential.secret_hash
                updated = True
            if existing_user.radius_profile_id != profile_id:
                existing_user.radius_profile_id = profile_id
                updated = True
            if existing_user.is_active is not True:
                existing_user.is_active = True
                updated = True
            existing_user.last_sync_at = datetime.now(UTC)
            if updated:
                changed += 1
            continue

        db.add(
            RadiusUser(
                subscriber_id=subscription.subscriber_id,
                subscription_id=subscription.id,
                access_credential_id=credential.id,
                username=credential.username,
                secret_hash=credential.secret_hash,
                radius_profile_id=profile_id,
                is_active=True,
                last_sync_at=datetime.now(UTC),
            )
        )
        changed += 1
    return changed


def reconcile_subscription_connectivity(
    db: Session,
    subscription_id: str,
) -> SubscriptionConnectivityOutcome:
    """Project one subscription and report whether every required target converged."""
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription or subscription.status not in RADIUS_SYNC_ELIGIBLE_STATUSES:
        return SubscriptionConnectivityOutcome(
            subscription_id=str(subscription_id),
            disposition=ConnectivityProjectionDisposition.ineligible_subscription,
        )

    radius_clients_changed = 0
    nas_device = None
    if subscription.provisioning_nas_device_id:
        nas_device = db.get(NasDevice, subscription.provisioning_nas_device_id)
        if nas_device:
            radius_clients_changed = ensure_radius_clients_for_nas(db, nas_device)
            db.flush()

    external_nas_synced = 0
    external_credentials_synced = 0
    credentials = list(
        db.scalars(
            select(AccessCredential)
            .where(AccessCredential.subscriber_id == subscription.subscriber_id)
            .where(AccessCredential.is_active.is_(True))
        ).all()
    )
    nas_configs = active_external_radius_targets(db, capability="nas")
    nas_devices = (
        [nas_device] if subscription.provisioning_nas_device_id and nas_device else []
    )
    for config in nas_configs:
        if nas_devices:
            external_nas_synced += _external_sync_nas(config, nas_devices).get(
                "external_nas_synced", 0
            )
    user_configs = active_external_radius_targets(db, capability="users")
    requested_usernames = {
        credential.username for credential in credentials if credential.username
    }
    if subscription.login:
        requested_usernames.add(str(subscription.login))

    if (
        subscription.status in ACTIVE_STATUSES | BLOCKED_STATUSES
        and not subscription.login
    ):
        return SubscriptionConnectivityOutcome(
            subscription_id=str(subscription.id),
            disposition=ConnectivityProjectionDisposition.missing_login,
            radius_clients_changed=radius_clients_changed,
            external_nas_synced=external_nas_synced,
            requested_logins=len(requested_usernames),
        )

    if not user_configs:
        return SubscriptionConnectivityOutcome(
            subscription_id=str(subscription.id),
            disposition=ConnectivityProjectionDisposition.target_unavailable,
            radius_clients_changed=radius_clients_changed,
            external_nas_synced=external_nas_synced,
            requested_logins=len(requested_usernames),
        )

    from app.services.radius_population import reconcile_usernames

    result = reconcile_usernames(
        requested_usernames,
        dry_run=False,
        source_db=db,
    )
    projected_logins = _projection_result_count(result, "projected_logins")
    projection_targets = _projection_result_count(result, "projection_targets")
    unbuildable_logins = _projection_result_count(result, "unbuildable_logins")
    external_credentials_synced = projected_logins * projection_targets
    disposition = (
        ConnectivityProjectionDisposition.unbuildable_login
        if unbuildable_logins
        else ConnectivityProjectionDisposition.projected
    )
    radius_users_changed = 0
    if disposition is ConnectivityProjectionDisposition.projected:
        # RadiusUser is the local observation consumed by exact service-change
        # verification. Never advance its profile/watermark until every
        # required external projection has converged.
        radius_users_changed = ensure_radius_users_for_subscription(db, subscription)
        db.flush()
    return SubscriptionConnectivityOutcome(
        subscription_id=str(subscription.id),
        disposition=disposition,
        radius_clients_changed=radius_clients_changed,
        radius_users_changed=radius_users_changed,
        external_nas_synced=external_nas_synced,
        external_credentials_synced=external_credentials_synced,
        requested_logins=len(requested_usernames),
        projected_logins=projected_logins,
        projection_targets=projection_targets,
    )


def _sanitize_table_identifier(raw: object, fallback: str) -> str:
    return sanitize_table_identifier(raw, fallback)


def _external_db_config(db: Session, job: RadiusSyncJob) -> dict | None:
    return next(
        (
            config
            for config in active_external_radius_targets(db)
            if config["target_id"] == str(job.id)
        ),
        None,
    )


def _external_sync_users(
    db: Session,
    config: dict,
    credentials: list[AccessCredential],
) -> dict[str, int]:
    """Compatibility adapter to the sole projection owner.

    ``config`` is intentionally ignored: one requested reconcile fans out to
    the complete DB-configured target set atomically per target.
    """
    del config
    from app.services.radius_population import reconcile_usernames

    usernames = {
        credential.username for credential in credentials if credential.username
    }
    result = reconcile_usernames(usernames, dry_run=False, source_db=db)
    _require_projection_result(result)
    return {
        "external_users_synced": len(usernames)
        * _projection_result_count(result, "projection_targets"),
        "external_users_skipped_unbuildable": _projection_result_count(
            result, "skipped_decrypt_failed"
        )
        + _projection_result_count(result, "skipped_no_password"),
    }


def _external_sync_nas(
    config: dict,
    nas_devices: list[NasDevice],
) -> dict[str, int]:
    nas_table = config["nas_table"]
    engine = _get_external_engine(config["db_url"])
    nas_sql_table = _external_radius_table(
        nas_table,
        Column("nasname", String),
        Column("shortname", String),
        Column("type", String),
        Column("secret", String),
        Column("description", String),
    )
    created = 0
    with engine.begin() as conn:
        for device in nas_devices:
            client_ip = _radius_client_ip_for_nas(device)
            if not client_ip:
                continue
            # Decrypt the stored credential, then resolve any OpenBao references
            decrypted_secret = _safe_decrypt_credential(
                device.shared_secret, label=f"NAS {device.name}"
            )
            if decrypted_secret is None:
                continue
            secret = resolve_secret(decrypted_secret)
            if not secret:
                continue
            conn.execute(
                delete(nas_sql_table).where(nas_sql_table.c.nasname == client_ip)
            )
            conn.execute(
                insert(nas_sql_table).values(
                    nasname=client_ip,
                    shortname=(device.name or "")[:32],
                    type=device.vendor.value
                    if hasattr(device.vendor, "value")
                    else "other",
                    secret=secret,
                    description=device.description,
                )
            )
            created += 1
    return {"external_nas_synced": created}


def external_radius_nas_client_ips(
    db: Session,
    client_ips: set[str],
) -> set[str]:
    """Return configured external RADIUS NAS clients without reading secrets."""
    wanted = {str(value).strip() for value in client_ips if str(value).strip()}
    if not wanted:
        return set()

    found: set[str] = set()
    for config in _active_external_sync_configs(db):
        nas_table = _external_radius_table(
            config.get("nas_table", "nas"),
            Column("nasname", String),
        )
        engine = _get_external_engine(config["db_url"])
        with engine.connect() as conn:
            rows = conn.execute(
                select(nas_table.c.nasname).where(nas_table.c.nasname.in_(wanted))
            ).scalars()
            found.update(str(value).strip() for value in rows if value)
    return found


@dataclass(frozen=True)
class ExternalRadiusNasSecretInventory:
    present_client_ips: frozenset[str]
    recoverable_secrets: dict[str, str]
    conflicting_client_ips: frozenset[str]


def external_radius_nas_secret_inventory(
    db: Session,
    client_ips: set[str],
) -> ExternalRadiusNasSecretInventory:
    """Read authoritative NAS secrets and reject cross-store conflicts."""
    wanted = {str(value).strip() for value in client_ips if str(value).strip()}
    values: dict[str, set[str]] = {client_ip: set() for client_ip in wanted}
    present: set[str] = set()
    for config in _active_external_sync_configs(db):
        nas_table = _external_radius_table(
            config.get("nas_table", "nas"),
            Column("nasname", String),
            Column("secret", String),
        )
        engine = _get_external_engine(config["db_url"])
        with engine.connect() as conn:
            rows = conn.execute(
                select(nas_table.c.nasname, nas_table.c.secret).where(
                    nas_table.c.nasname.in_(wanted)
                )
            ).all()
        for client_ip, secret in rows:
            normalized_ip = str(client_ip or "").strip()
            if not normalized_ip:
                continue
            present.add(normalized_ip)
            normalized_secret = str(secret or "")
            if normalized_secret:
                values.setdefault(normalized_ip, set()).add(normalized_secret)

    conflicts = {client_ip for client_ip, secrets in values.items() if len(secrets) > 1}
    recoverable = {
        client_ip: next(iter(secrets))
        for client_ip, secrets in values.items()
        if len(secrets) == 1
    }
    return ExternalRadiusNasSecretInventory(
        present_client_ips=frozenset(present),
        recoverable_secrets=recoverable,
        conflicting_client_ips=frozenset(conflicts),
    )


def remove_external_radius_nas_clients(
    db: Session,
    client_ips: set[str],
) -> int:
    """Remove explicitly decommissioned NAS clients from external RADIUS stores."""
    wanted = {str(value).strip() for value in client_ips if str(value).strip()}
    if not wanted:
        return 0

    removed = 0
    for config in _active_external_sync_configs(db):
        nas_table = _external_radius_table(
            config.get("nas_table", "nas"),
            Column("nasname", String),
        )
        engine = _get_external_engine(config["db_url"])
        with engine.begin() as conn:
            result = conn.execute(
                delete(nas_table).where(nas_table.c.nasname.in_(wanted))
            )
            removed += max(int(result.rowcount or 0), 0)
    return removed


@dataclass(frozen=True)
class RadiusNasLifecycleState:
    client_ip: str | None
    internal_active_clients: int
    external_present: bool


@dataclass(frozen=True)
class RadiusNasLifecycleResult:
    desired_active: bool
    internal_clients_changed: int
    external_clients_changed: int


def radius_nas_lifecycle_state(
    db: Session,
    nas_device: NasDevice,
) -> RadiusNasLifecycleState:
    """Resolve bounded internal/external RADIUS client lifecycle evidence."""
    return radius_nas_lifecycle_states(db, [nas_device])[nas_device.id]


def radius_nas_lifecycle_states(
    db: Session,
    nas_devices: list[NasDevice],
) -> dict[object, RadiusNasLifecycleState]:
    """Batch lifecycle evidence without per-device external database queries."""
    if not nas_devices:
        return {}
    device_ids = [device.id for device in nas_devices]
    active_client_ips: dict[object, set[str]] = {}
    active_client_counts: dict[object, int] = {}
    for device_id, client_ip in db.execute(
        select(RadiusClient.nas_device_id, RadiusClient.client_ip)
        .where(RadiusClient.nas_device_id.in_(device_ids))
        .where(RadiusClient.is_active.is_(True))
    ).all():
        normalized_ip = str(client_ip or "").strip()
        if device_id is None:
            continue
        active_client_counts[device_id] = active_client_counts.get(device_id, 0) + 1
        if normalized_ip:
            active_client_ips.setdefault(device_id, set()).add(normalized_ip)

    candidate_ips: dict[object, set[str]] = {}
    for device in nas_devices:
        projected_ip = _radius_client_ip_for_nas(device)
        candidates = set(active_client_ips.get(device.id, set()))
        if projected_ip:
            candidates.add(projected_ip)
        candidate_ips[device.id] = candidates
    external_ips = external_radius_nas_client_ips(
        db, set().union(*candidate_ips.values()) if candidate_ips else set()
    )
    return {
        device.id: RadiusNasLifecycleState(
            client_ip=(
                _radius_client_ip_for_nas(device)
                or next(iter(sorted(active_client_ips.get(device.id, set()))), None)
            ),
            internal_active_clients=active_client_counts.get(device.id, 0),
            external_present=bool(candidate_ips[device.id] & external_ips),
        )
        for device in nas_devices
    }


def apply_radius_nas_lifecycle(
    db: Session,
    nas_device: NasDevice,
    *,
    active: bool,
) -> RadiusNasLifecycleResult:
    """Project one reviewed NAS lifecycle decision into RADIUS stores."""
    if active:
        internal_changed = ensure_radius_clients_for_nas(db, nas_device)
        external_changed = 0
        for config in _active_external_sync_configs(db):
            external_changed += int(
                _external_sync_nas(config, [nas_device]).get("external_nas_synced", 0)
            )
        return RadiusNasLifecycleResult(
            desired_active=True,
            internal_clients_changed=internal_changed,
            external_clients_changed=external_changed,
        )

    clients = db.scalars(
        select(RadiusClient)
        .where(RadiusClient.nas_device_id == nas_device.id)
        .where(RadiusClient.is_active.is_(True))
    ).all()
    client_ips = {
        normalized_ip
        for client in clients
        if (normalized_ip := str(client.client_ip or "").strip())
    }
    for client in clients:
        client.is_active = False
    if client_ip := _radius_client_ip_for_nas(nas_device):
        client_ips.add(client_ip)
    external_changed = remove_external_radius_nas_clients(db, client_ips)
    return RadiusNasLifecycleResult(
        desired_active=False,
        internal_clients_changed=len(clients),
        external_clients_changed=external_changed,
    )


class RadiusUsers(ListResponseMixin):
    @staticmethod
    def get(db: Session, user_id: str):
        user = db.get(RadiusUser, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Radius user not found")
        return user

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        stmt = select(RadiusUser)
        if account_id:
            stmt = stmt.where(RadiusUser.subscriber_id == coerce_uuid(account_id))
        if is_active is None:
            stmt = stmt.where(RadiusUser.is_active.is_(True))
        else:
            stmt = stmt.where(RadiusUser.is_active == is_active)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": RadiusUser.created_at, "username": RadiusUser.username},
        )
        return list(db.scalars(apply_pagination(stmt, limit, offset)).all())


class RadiusSyncJobs(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: RadiusSyncJobCreate):
        server = db.get(RadiusServer, payload.server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Radius server not found")
        if payload.connector_config_id:
            config = db.get(ConnectorConfig, payload.connector_config_id)
            if not config:
                raise HTTPException(
                    status_code=404, detail="Connector config not found"
                )
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "sync_users" not in fields_set:
            default_sync_users = settings_spec.resolve_value(
                db, SettingDomain.radius, "default_sync_users"
            )
            if default_sync_users is not None:
                data["sync_users"] = bool(default_sync_users)
        if "sync_nas_clients" not in fields_set:
            default_sync_clients = settings_spec.resolve_value(
                db, SettingDomain.radius, "default_sync_nas_clients"
            )
            if default_sync_clients is not None:
                data["sync_nas_clients"] = bool(default_sync_clients)
        job = RadiusSyncJob(**data)
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def get(db: Session, job_id: str):
        job = db.get(RadiusSyncJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Radius sync job not found")
        return job

    @staticmethod
    def list(
        db: Session,
        server_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        stmt = select(RadiusSyncJob)
        if server_id:
            stmt = stmt.where(RadiusSyncJob.server_id == server_id)
        if is_active is None:
            stmt = stmt.where(RadiusSyncJob.is_active.is_(True))
        else:
            stmt = stmt.where(RadiusSyncJob.is_active == is_active)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": RadiusSyncJob.created_at, "name": RadiusSyncJob.name},
        )
        return list(db.scalars(apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def update(db: Session, job_id: str, payload: RadiusSyncJobUpdate):
        job = db.get(RadiusSyncJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Radius sync job not found")
        data = payload.model_dump(exclude_unset=True)
        if "server_id" in data:
            server = db.get(RadiusServer, data["server_id"])
            if not server:
                raise HTTPException(status_code=404, detail="Radius server not found")
        if "connector_config_id" in data and data["connector_config_id"]:
            config = db.get(ConnectorConfig, data["connector_config_id"])
            if not config:
                raise HTTPException(
                    status_code=404, detail="Connector config not found"
                )
        for key, value in data.items():
            setattr(job, key, value)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def delete(db: Session, job_id: str):
        job = db.get(RadiusSyncJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Radius sync job not found")
        job.is_active = False
        db.commit()

    @staticmethod
    def run(db: Session, job_id: str):
        job = db.get(RadiusSyncJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Radius sync job not found")
        if not job.is_active:
            raise HTTPException(status_code=400, detail="Radius sync job is inactive")
        default_status = settings_spec.resolve_value(
            db, SettingDomain.radius, "default_sync_status"
        )
        status_value = (
            validate_enum(default_status, RadiusSyncStatus, "status")
            if default_status
            else RadiusSyncStatus.running
        )
        run = RadiusSyncRun(job_id=job.id, status=status_value)
        db.add(run)
        db.commit()
        db.refresh(run)

        started_at = datetime.now(UTC)
        users_created = users_updated = clients_created = clients_updated = 0
        status = RadiusSyncStatus.success
        details: dict[str, object] = {}
        try:
            external_config = _external_db_config(db, job)
            if job.sync_nas_clients:
                nas_devices = list(
                    db.scalars(
                        select(NasDevice).where(NasDevice.is_active.is_(True))
                    ).all()
                )
                for device in nas_devices:
                    client_ip = _radius_client_ip_for_nas(device)
                    if not client_ip:
                        continue
                    # Decrypt the stored credential, then resolve any OpenBao references
                    decrypted_secret = _safe_decrypt_credential(
                        device.shared_secret, label=f"NAS {device.name}"
                    )
                    if decrypted_secret is None:
                        continue
                    raw_secret = resolve_secret(decrypted_secret)
                    if not raw_secret:
                        continue
                    existing_client = db.scalars(
                        select(RadiusClient)
                        .where(RadiusClient.server_id == job.server_id)
                        .where(RadiusClient.client_ip == client_ip)
                    ).first()
                    secret_hash = _hash_secret(raw_secret)
                    if existing_client:
                        existing_client.nas_device_id = device.id
                        existing_client.client_ip = client_ip
                        existing_client.shared_secret_hash = secret_hash
                        existing_client.description = device.name
                        existing_client.is_active = True
                        clients_updated += 1
                    else:
                        client = RadiusClient(
                            server_id=job.server_id,
                            nas_device_id=device.id,
                            client_ip=client_ip,
                            shared_secret_hash=secret_hash,
                            description=device.name,
                            is_active=True,
                        )
                        db.add(client)
                        clients_created += 1
                db.commit()
                details["nas_devices_synced"] = len(nas_devices)
                if external_config:
                    details.update(_external_sync_nas(external_config, nas_devices))

            if job.sync_users:
                credentials = list(
                    db.scalars(
                        select(AccessCredential).where(
                            AccessCredential.is_active.is_(True)
                        )
                    ).all()
                )
                for credential in credentials:
                    subscription = _radius_sync_subscription_for_subscriber(
                        db, credential.subscriber_id
                    )
                    if not subscription:
                        continue
                    existing_user = db.scalars(
                        select(RadiusUser).where(
                            RadiusUser.access_credential_id == credential.id
                        )
                    ).first()
                    if existing_user:
                        existing_user.subscription_id = subscription.id
                        existing_user.subscriber_id = subscription.subscriber_id
                        existing_user.username = credential.username
                        existing_user.secret_hash = credential.secret_hash
                        existing_user.radius_profile_id = credential.radius_profile_id
                        existing_user.is_active = True
                        existing_user.last_sync_at = datetime.now(UTC)
                        users_updated += 1
                    else:
                        user = RadiusUser(
                            subscriber_id=subscription.subscriber_id,
                            subscription_id=subscription.id,
                            access_credential_id=credential.id,
                            username=credential.username,
                            secret_hash=credential.secret_hash,
                            radius_profile_id=credential.radius_profile_id,
                            is_active=True,
                            last_sync_at=datetime.now(UTC),
                        )
                        db.add(user)
                        users_created += 1
                db.commit()
                details["credentials_scanned"] = len(credentials)
                if external_config:
                    details.update(
                        _external_sync_users(db, external_config, credentials)
                    )
        except Exception as exc:
            db.rollback()
            status = RadiusSyncStatus.failed
            details["error"] = str(exc)
            raise
        finally:
            finished_at = datetime.now(UTC)
            run.status = status
            run.finished_at = finished_at
            run.users_created = users_created
            run.users_updated = users_updated
            run.clients_created = clients_created
            run.clients_updated = clients_updated
            run.details = details
            job.last_run_at = finished_at
            db.commit()
            record_task_run(
                "radius_sync",
                status=status.value,
                duration_seconds=(finished_at - started_at).total_seconds(),
            )
            db.refresh(run)
        return run


class RadiusSyncRuns(ListResponseMixin):
    @staticmethod
    def get(db: Session, run_id: str):
        run = db.get(RadiusSyncRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Radius sync run not found")
        return run

    @staticmethod
    def list(
        db: Session,
        job_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        stmt = select(RadiusSyncRun)
        if job_id:
            stmt = stmt.where(RadiusSyncRun.job_id == job_id)
        if status:
            try:
                status_value = RadiusSyncStatus(status)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid status") from exc
            stmt = stmt.where(RadiusSyncRun.status == status_value)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"started_at": RadiusSyncRun.started_at, "status": RadiusSyncRun.status},
        )
        return list(db.scalars(apply_pagination(stmt, limit, offset)).all())


def sync_credential_to_radius(db: Session, credential: AccessCredential) -> bool:
    """Immediately sync a single credential to all active RADIUS sync jobs.

    This is called when a credential is created/updated or when a subscription
    is activated, ensuring the user can authenticate immediately without
    waiting for the periodic sync.

    Args:
        db: Database session
        credential: The access credential to sync

    Returns:
        True if synced to at least one external RADIUS database
    """
    if not credential.is_active:
        return False

    # Check if credential has a sync-eligible subscription
    subscription = _radius_sync_subscription_for_subscriber(
        db, credential.subscriber_id
    )
    if not subscription:
        return False

    if not active_external_radius_targets(db, capability="users"):
        return False
    from app.services.radius_population import reconcile_usernames

    result = reconcile_usernames({credential.username}, dry_run=False, source_db=db)
    _require_projection_result(result)
    return True


def sync_account_credentials_to_radius(db: Session, account_id) -> int:
    """Sync all active credentials for an account to RADIUS.

    Called when a subscription is activated to ensure all the account's
    credentials are immediately available for authentication.

    Args:
        db: Database session
        account_id: The subscriber account ID

    Returns:
        Number of credentials synced
    """
    account_uuid = coerce_uuid(account_id)
    credentials = list(
        db.scalars(
            select(AccessCredential)
            .where(AccessCredential.subscriber_id == account_uuid)
            .where(AccessCredential.is_active.is_(True))
        ).all()
    )

    if not credentials or not active_external_radius_targets(db, capability="users"):
        return 0
    from app.services.radius_population import reconcile_usernames

    result = reconcile_usernames(
        {credential.username for credential in credentials},
        dry_run=False,
        source_db=db,
    )
    _require_projection_result(result)
    return _projection_result_count(
        result, "projected_logins"
    ) * _projection_result_count(result, "projection_targets")


def remove_external_radius_credentials(db: Session, account_id) -> int:
    """Request canonical convergence for every credential on an account."""
    account_uuid = coerce_uuid(account_id)
    credentials = list(
        db.scalars(
            select(AccessCredential).where(
                AccessCredential.subscriber_id == account_uuid
            )
        ).all()
    )
    if not credentials:
        return 0

    from app.services.radius_population import reconcile_usernames

    result = reconcile_usernames(
        {credential.username for credential in credentials},
        dry_run=False,
        source_db=db,
    )
    _require_projection_result(result)
    return _projection_result_count(
        result, "projected_logins"
    ) * _projection_result_count(result, "projection_targets")


def block_external_radius_credentials(db: Session, account_id) -> int:
    """Converge credentials from authoritative blocked/captive source state."""
    account_uuid = coerce_uuid(account_id)
    credentials = list(
        db.scalars(
            select(AccessCredential).where(
                AccessCredential.subscriber_id == account_uuid
            )
        ).all()
    )
    if not credentials:
        return 0

    from app.services.radius_population import reconcile_usernames

    result = reconcile_usernames(
        {credential.username for credential in credentials},
        dry_run=False,
        source_db=db,
    )
    _require_projection_result(result)
    return _projection_result_count(
        result, "projected_logins"
    ) * _projection_result_count(result, "projection_targets")


def unblock_external_radius_credentials(db: Session, account_id) -> int:
    """Converge credentials from authoritative restored source state."""
    account_uuid = coerce_uuid(account_id)
    credentials = list(
        db.scalars(
            select(AccessCredential).where(
                AccessCredential.subscriber_id == account_uuid
            )
        ).all()
    )
    if not credentials:
        return 0

    from app.services.radius_population import reconcile_usernames

    result = reconcile_usernames(
        {credential.username for credential in credentials},
        dry_run=False,
        source_db=db,
    )
    _require_projection_result(result)
    return _projection_result_count(
        result, "projected_logins"
    ) * _projection_result_count(result, "projection_targets")


radius_servers = RadiusServers()
radius_clients = RadiusClients()
radius_users = RadiusUsers()
radius_sync_jobs = RadiusSyncJobs()
radius_sync_runs = RadiusSyncRuns()
