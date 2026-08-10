"""Feature module toggle management."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service

logger = logging.getLogger(__name__)


MODULE_KEY_MAP: dict[str, str] = {
    "network": "module_network_enabled",
    "integrations": "module_integrations_enabled",
    "crm": "module_crm_enabled",
    "provisioning": "module_provisioning_enabled",
    "vpn": "module_vpn_enabled",
    "gis": "module_gis_enabled",
    "reports": "module_reports_enabled",
}

MODULE_LABELS: dict[str, str] = {
    "network": "Network",
    "provisioning": "Provisioning",
    "vpn": "VPN",
    "gis": "GIS",
    "reports": "Reports",
    "integrations": "Integrations",
    "crm": "CRM",
}

MODULE_ORDER = [
    "network",
    "provisioning",
    "vpn",
    "gis",
    "reports",
    "integrations",
    "crm",
]


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _resolve_module_flag(db: Session, setting_key: str, default: bool = True) -> bool:
    try:
        setting = domain_settings_service.modules_settings.get_by_key(db, setting_key)
    except HTTPException:
        return default
    if setting.value_json is not None:
        return _coerce_bool(setting.value_json, default=default)
    return _coerce_bool(setting.value_text, default=default)


def invalidate_module_cache() -> None:
    """Kept as a no-op seam: there is no aggregate cache to invalidate.

    The map used to be cached whole under `modules:states` — a key that is not
    a setting, in an unscoped keyspace, invalidated from two call sites. It is
    now rebuilt from the per-flag reads below, which is cheap because those go
    through the settings resolver and its cache.

    The function survives because callers treat it as "module state changed,
    drop what depends on it". Deleting the name would move that decision to
    every caller; keeping it means the day this needs a cache again, one place
    grows one.
    """


def load_module_states(db: Session, *, force_refresh: bool = False) -> dict[str, bool]:
    """Every module flag, rebuilt from the resolver rather than cached whole.

    `force_refresh` is now always true in effect and kept for callers.
    """

    del force_refresh  # no aggregate cache to bypass

    return {
        module_name: _resolve_module_flag(db, setting_key, default=True)
        for module_name, setting_key in MODULE_KEY_MAP.items()
    }


def is_module_enabled(db: Session, module_name: str) -> bool:
    """Return whether a module is enabled."""
    states = load_module_states(db)
    return states.get(module_name, True)


def load_feature_states(
    db: Session,
    *,
    force_refresh: bool = False,
) -> dict[str, bool]:
    """Return permanent customer-portal capabilities for legacy layouts."""
    del db, force_refresh
    return {"services_view": True}


def list_payment_providers(db: Session) -> list[dict[str, Any]]:
    """Return payment-provider rows (id/name/provider_type/is_active).

    Backs the module-manager on/off toggles. Defensive: returns ``[]`` if the
    table can't be read (e.g. before the billing tables exist).
    """
    from app.models.billing import PaymentProvider

    try:
        rows = db.query(PaymentProvider).order_by(PaymentProvider.name).all()
    except Exception:
        logger.warning("Failed to load payment providers", exc_info=True)
        return []
    providers: list[dict[str, Any]] = []
    for row in rows:
        provider_type = getattr(row.provider_type, "value", str(row.provider_type))
        providers.append(
            {
                "id": str(row.id),
                "name": row.name,
                "provider_type": provider_type,
                "is_active": bool(row.is_active),
            }
        )
    return providers


def update_provider_flags(db: Session, *, payload: dict[str, bool]) -> None:
    """Set ``is_active`` per payment-provider id.

    Defensive: unknown or malformed ids are skipped; only commits if a row
    actually changed. Flipping a provider off here removes it from billing
    checkout.
    """
    if not payload:
        return
    from app.models.billing import PaymentProvider

    changed = False
    for provider_id, enabled in payload.items():
        try:
            provider = db.get(PaymentProvider, uuid.UUID(str(provider_id)))
        except (ValueError, TypeError):
            provider = None
        if provider is None:
            continue
        provider.is_active = bool(enabled)
        changed = True
    if changed:
        db.commit()


def module_manager_page_state(db: Session) -> dict[str, Any]:
    from app.services import control_registry
    from app.services.control_registry import Layer

    modules = load_module_states(db)
    features = load_feature_states(db)
    features_by_module: dict[str, list[dict[str, Any]]] = {
        module_name: [] for module_name in MODULE_ORDER
    }
    independent_features: list[dict[str, Any]] = []
    for control in sorted(control_registry.all_controls(), key=lambda item: item.key):
        if control.layer is not Layer.feature:
            continue
        resolution = control_registry.resolve_control(db, control.key)
        entry = {
            "key": control.key,
            "label": control.key.replace(".", " / ").replace("_", " ").title(),
            "description": control.description,
            "effective": resolution.enabled,
            "own_enabled": resolution.own_enabled,
            "stored": (
                "inherit"
                if resolution.canonical_value is None
                else "on"
                if resolution.canonical_value
                else "off"
            ),
            "source": resolution.source,
            "precedence": resolution.precedence,
            "affected_scope": resolution.affected_scope,
            "updated_at": resolution.canonical_updated_at,
        }
        if control.owner_module:
            features_by_module.setdefault(control.owner_module, []).append(entry)
        else:
            independent_features.append(entry)

    cards: list[dict[str, Any]] = []
    for module_name in MODULE_ORDER:
        cards.append(
            {
                "name": module_name,
                "label": MODULE_LABELS.get(module_name, module_name.title()),
                "enabled": bool(modules.get(module_name, True)),
                "features": features_by_module.get(module_name, []),
            }
        )
    return {
        "module_cards": cards,
        "module_states": modules,
        "feature_states": features,
        "independent_features": independent_features,
        "payment_providers": list_payment_providers(db),
    }


def _upsert_boolean_setting(db: Session, key: str, enabled: bool) -> None:
    domain_settings_service.modules_settings.upsert_by_key(
        db,
        key,
        DomainSettingUpdate(
            domain=SettingDomain.modules,
            value_type=SettingValueType.boolean,
            value_text="true" if enabled else "false",
            value_json=None,
            is_active=True,
        ),
    )


def update_module_flags(db: Session, *, payload: dict[str, bool]) -> None:
    for module_name, enabled in payload.items():
        setting_key = MODULE_KEY_MAP.get(module_name)
        if not setting_key:
            continue
        _upsert_boolean_setting(db, setting_key, bool(enabled))
    invalidate_module_cache()


def require_module_enabled(module_name: str) -> Callable[..., None]:
    """Dependency factory that returns 404 when a module is disabled."""

    def _dependency(db: Session = Depends(get_db)) -> None:
        if not is_module_enabled(db, module_name):
            raise HTTPException(status_code=404, detail="Not found")

    return _dependency
