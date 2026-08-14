"""Generic domain setting API helpers."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.schemas.settings import DomainSettingUpdate
from app.services.settings_api_custom import (
    _get_domain_setting,
    _list_domain_settings,
    _list_domain_settings_response,
    _upsert_domain_setting,
)

logger = logging.getLogger(__name__)


def list_billing_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.billing, is_active, order_by, order_dir, limit, offset
    )


def list_billing_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.billing, is_active, order_by, order_dir, limit, offset
    )


def upsert_billing_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.billing, key, payload)


def get_billing_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.billing, key)


def list_catalog_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.catalog, is_active, order_by, order_dir, limit, offset
    )


def list_catalog_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.catalog, is_active, order_by, order_dir, limit, offset
    )


def upsert_catalog_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.catalog, key, payload)


def get_catalog_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.catalog, key)


def list_subscriber_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.subscriber, is_active, order_by, order_dir, limit, offset
    )


def list_subscriber_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.subscriber, is_active, order_by, order_dir, limit, offset
    )


def upsert_subscriber_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.subscriber, key, payload)


def get_subscriber_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.subscriber, key)


def list_usage_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.usage, is_active, order_by, order_dir, limit, offset
    )


def list_usage_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.usage, is_active, order_by, order_dir, limit, offset
    )


def upsert_usage_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.usage, key, payload)


def get_usage_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.usage, key)


def list_collections_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.collections, is_active, order_by, order_dir, limit, offset
    )


def list_collections_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.collections, is_active, order_by, order_dir, limit, offset
    )


def upsert_collections_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.collections, key, payload)


def get_collections_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.collections, key)


def list_provisioning_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.provisioning, is_active, order_by, order_dir, limit, offset
    )


def list_provisioning_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.provisioning, is_active, order_by, order_dir, limit, offset
    )


def upsert_provisioning_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.provisioning, key, payload)


def get_provisioning_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.provisioning, key)


def list_network_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.network, is_active, order_by, order_dir, limit, offset
    )


def list_network_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.network, is_active, order_by, order_dir, limit, offset
    )


def upsert_network_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.network, key, payload)


def get_network_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.network, key)


def list_inventory_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.inventory, is_active, order_by, order_dir, limit, offset
    )


def list_inventory_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.inventory, is_active, order_by, order_dir, limit, offset
    )


def upsert_inventory_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.inventory, key, payload)


def get_inventory_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.inventory, key)


def list_lifecycle_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.lifecycle, is_active, order_by, order_dir, limit, offset
    )


def list_lifecycle_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.lifecycle, is_active, order_by, order_dir, limit, offset
    )


def upsert_lifecycle_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.lifecycle, key, payload)


def get_lifecycle_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.lifecycle, key)


def list_comms_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.comms, is_active, order_by, order_dir, limit, offset
    )


def list_comms_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.comms, is_active, order_by, order_dir, limit, offset
    )


def upsert_comms_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.comms, key, payload)


def get_comms_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.comms, key)


def list_integration_settings(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings(
        db, SettingDomain.integration, is_active, order_by, order_dir, limit, offset
    )


def list_integration_settings_response(
    db: Session,
    is_active: bool | None,
    order_by: str,
    order_dir: str,
    limit: int,
    offset: int,
):
    return _list_domain_settings_response(
        db, SettingDomain.integration, is_active, order_by, order_dir, limit, offset
    )


def upsert_integration_setting(db: Session, key: str, payload: DomainSettingUpdate):
    return _upsert_domain_setting(db, SettingDomain.integration, key, payload)


def get_integration_setting(db: Session, key: str):
    return _get_domain_setting(db, SettingDomain.integration, key)
