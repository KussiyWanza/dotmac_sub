from __future__ import annotations

import logging

# `held_secret`, not `get_secret`: `app.services.secrets.get_secret` talks to
# OpenBao, and this one is a dict lookup over material loaded at boot.
from dotmac_kernel.secret_sources import get_secret as held_secret
from fastapi import HTTPException
from pyrad.client import Client
from pyrad.dictionary import Dictionary
from pyrad.packet import AccessRequest
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.radius import RadiusServer

logger = logging.getLogger(__name__)


def _setting_value(db: Session, key: str) -> str | None:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.radius)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    if setting.value_text is not None:
        return setting.value_text
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _pick_radius_server(db: Session, server_id: str | None) -> RadiusServer:
    server_id = server_id or _setting_value(db, "auth_server_id")
    query = db.query(RadiusServer).filter(RadiusServer.is_active.is_(True))
    if server_id:
        server = query.filter(RadiusServer.id == server_id).first()
    else:
        server = query.order_by(RadiusServer.created_at.desc()).first()
    if not server:
        raise HTTPException(status_code=400, detail="Radius auth server not configured")
    return server


def authenticate(
    db: Session, username: str, password: str, server_id: str | None = None
) -> None:
    server = _pick_radius_server(db, server_id)
    # What was held at boot — not the `radius/auth_shared_secret` row, and not
    # the environment.
    #
    # That row did not contain the secret. `auth_shared_secret` was declared
    # `is_secret=True`, and every write of a secret setting goes through
    # `DomainSettings._write_secret_ref`, which puts the value in OpenBao and
    # stores a `bao://secret/settings/radius#auth_shared_secret` REFERENCE in
    # the column. This call site never dereferenced it: it took
    # `setting.value_text` and passed it to `secret.encode("utf-8")`, so
    # wherever the secret was configured through the settings path the RADIUS
    # client was handed the literal string `bao://...` as the shared secret.
    #
    # No environment read either, though the other four held secrets keep one.
    # Those are read in modules that OWN a deployment input
    # (`credential_crypto`, `wireguard_crypto`, `auth_flow`'s `_env_value`);
    # this is a business caller, and Sub's rule is that an environment variable
    # is a BOOTSTRAP input materialised into a row, never a runtime source —
    # `tests/architecture/test_decision_input_ownership.py` holds that line and
    # rejected the read that was here. `RADIUS_AUTH_SHARED_SECRET` still
    # bootstraps the OpenBao entry; this reads the result.
    secret = held_secret("radius_auth_shared_secret")
    if not secret:
        raise HTTPException(status_code=400, detail="Radius auth secret not configured")
    dict_path = _setting_value(db, "auth_dictionary_path") or "/etc/raddb/dictionary"
    try:
        dictionary = Dictionary(dict_path)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Radius dictionary not available"
        ) from exc
    client = Client(
        server=server.host,
        secret=secret.encode("utf-8"),
        dict=dictionary,
        authport=server.auth_port,
    )
    client.retries = 1
    client.timeout = float(_setting_value(db, "auth_timeout_sec") or 3)
    req = client.CreateAuthPacket(code=AccessRequest, User_Name=username)
    req["User-Password"] = req.PwCrypt(password)
    try:
        reply = client.SendPacket(req)
    except TimeoutError as exc:
        raise HTTPException(status_code=502, detail="Radius auth timeout") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Radius auth failed") from exc
    if reply.code != reply.AccessAccept:
        raise HTTPException(status_code=401, detail="Invalid credentials")
