"""Single control-plane resolver: modules, features, and safety in one place.

The app historically grew FIVE blended kinds of setting — product modules,
capability features, scheduler/task toggles, safety gates, and tuning knobs —
resolved in different places with different fail directions. That blend is what
let billing automation be silently half-off.

This module is the single source of truth and the single read path for
capabilities that remain optional:

  * Layer 1 — MODULE   : "does this product area exist?"  (modules.* domain)
  * Layer 2 — FEATURE  : "is this capability on?" (composes with its module)
  * Layer 3 — SAFETY   : guardrails for domains that have not completed a
                         permanent authority cutover.

``is_enabled(db, "network.ont_reconcile")`` is the one optional-capability
call sites should use. A
feature is enabled only if BOTH its module and its own flag are on. Resolution
order per control: explicit canonical DB row → registry default, with a
per-control fail direction (``on_missing``). Historical environment and
database aliases are retained below only as completed-cutover inventory; they
never supply an effective value or route runtime callers.

The customer financial lifecycle completed its permanent cutover in 2026-07.
Billing, catalog, customer, notification, renewal, collection, reconciliation,
and restoration execution are therefore deliberately absent from this
registry: no database row, environment value, module card, or scheduler alias
may disable them. Account policy and external-provider capability are resolved
by their named domain owners instead of a global feature switch.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services import module_manager

logger = logging.getLogger(__name__)


class Layer(str, Enum):
    module = "module"
    feature = "feature"
    safety = "safety"


@dataclass(frozen=True)
class LegacyAlias:
    """Retired setting identity kept only as completed-cutover inventory.

    Runtime resolution and scheduler callers must never read or route through
    this identity.
    """

    domain: SettingDomain
    key: str
    env: str | None = None


@dataclass(frozen=True)
class Control:
    """One control-plane setting with a single, declared meaning."""

    key: str  # canonical dotted key, e.g. "network.ont_reconcile"
    layer: Layer
    default: bool
    # Fail direction when NO value is found anywhere. Revenue/feature controls
    # fail OPEN (absent = on); dangerous capabilities and safety gates fail
    # CLOSED. This is the property whose absence caused the billing outage.
    on_missing: bool
    owner_module: str | None = None  # required for features
    legacy: tuple[LegacyAlias, ...] = field(default_factory=tuple)
    description: str = ""


@dataclass(frozen=True)
class ControlResolution:
    """Effective control state plus the provenance used to reach it."""

    key: str
    enabled: bool
    own_enabled: bool
    source: str
    precedence: str
    affected_scope: str
    updated_at: datetime | None = None
    module_enabled: bool | None = None
    canonical_value: bool | None = None
    canonical_updated_at: datetime | None = None


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


# ---------------------------------------------------------------------------
# Registry. Modules come from module_manager.MODULE_KEY_MAP (Layer 1). Here we
# add the capability features that own scheduler/task behavior. ``legacy``
# entries document completed cutovers only and are never runtime bindings or
# value sources.
# ---------------------------------------------------------------------------

_NET = SettingDomain.network
_R = SettingDomain.radius
_SCH = SettingDomain.scheduler
_G = SettingDomain.gis
_U = SettingDomain.usage
_PRJ = SettingDomain.projects
_SUB = SettingDomain.subscriber


_FEATURE_CONTROLS: tuple[Control, ...] = (
    Control(
        # Master gate for the loyalty + data-capture programme
        # (docs/designs/LOYALTY_AND_CAPTURE.md). DEFAULT OFF: off means no
        # milestone review, no sends, and no field-arrival location capture.
        key="loyalty.campaigns",
        layer=Layer.feature,
        default=False,
        on_missing=False,
        description="Loyalty + data-capture programme master switch.",
    ),
    Control(
        # The customer/agent confirm-your-location prompt. DEFAULT OFF, and
        # independently flippable from the master so ops can capture without
        # running loyalty.
        key="loyalty.capture_prompt",
        layer=Layer.feature,
        default=False,
        on_missing=False,
        description="Portal/agent location confirm-or-correct prompt.",
    ),
    # NOTE: nas_backup_retention intentionally NOT registered — it is network
    # infrastructure housekeeping, not a catalog (product) capability. Leaving it
    # unregistered keeps it on the pure legacy path with no accidental module
    # coupling (disabling a module must not silently stop NAS backup cleanup).
    Control(
        key="usage.warnings",
        layer=Layer.feature,
        default=True,
        on_missing=True,
        legacy=(LegacyAlias(_U, "usage_warning_enabled", "USAGE_WARNING_ENABLED"),),
        description="Usage warning event/notification emission.",
    ),
    Control(
        key="usage.fup_submonthly_rules",
        layer=Layer.feature,
        default=False,
        on_missing=False,
        legacy=(
            LegacyAlias(
                _U,
                "fup_submonthly_rules_enabled",
                "USAGE_FUP_SUBMONTHLY_RULES_ENABLED",
            ),
        ),
        description="Allow daily/weekly FUP rules from samples-derived usage.",
    ),
    Control(
        key="sessions.radius_accounting_import",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _U,
                "radius_accounting_import_enabled",
                "RADIUS_ACCOUNTING_IMPORT_ENABLED",
            ),
        ),
        description="Import RADIUS accounting sessions.",
    ),
    Control(
        key="sessions.radius_reap_stale",
        layer=Layer.feature,
        owner_module="network",
        default=False,
        on_missing=False,
        legacy=(
            LegacyAlias(
                _U, "radius_session_reap_enabled", "RADIUS_SESSION_REAP_ENABLED"
            ),
        ),
        description="Close stale RADIUS accounting sessions.",
    ),
    Control(
        key="access.radius_coa",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(LegacyAlias(_R, "coa_enabled", "RADIUS_COA_ENABLED"),),
        description="RADIUS CoA / disconnect requests.",
    ),
    Control(
        key="access.mikrotik_session_kill",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET,
                "mikrotik_session_kill_enabled",
                "NETWORK_MIKROTIK_SESSION_KILL_ENABLED",
            ),
        ),
        description="MikroTik session kill enforcement.",
    ),
    Control(
        key="access.mikrotik_api_session_kick",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET,
                "mikrotik_api_session_kick_enabled",
                "NETWORK_MIKROTIK_API_SESSION_KICK_ENABLED",
            ),
        ),
        description="MikroTik API session kick enforcement.",
    ),
    Control(
        key="access.address_list_block",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET,
                "address_list_block_enabled",
                "NETWORK_ADDRESS_LIST_BLOCK_ENABLED",
            ),
        ),
        description="MikroTik address-list based blocking.",
    ),
    Control(
        key="network.olt_profile_sync",
        layer=Layer.feature,
        owner_module="network",
        default=False,
        on_missing=False,
        legacy=(
            LegacyAlias(
                _NET,
                "olt_profile_sync_worker_enabled",
                "OLT_PROFILE_SYNC_WORKER_ENABLED",
            ),
        ),
        description="OLT profile sync worker.",
    ),
    Control(
        key="network.ont_reconcile",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        description="Scheduled Huawei ONT desired/observed reconciliation.",
    ),
    Control(
        key="network.cpe_dialer_credential_sync",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        description=(
            "Project the authoritative access credential onto each assigned "
            "ONT's PPPoE dialer (fingerprint comparison; never writes RADIUS)."
        ),
    ),
    Control(
        key="network.forwarding_observation_collection",
        layer=Layer.feature,
        owner_module="network",
        default=False,
        on_missing=False,
        description=(
            "Read-only RouterOS BGP-peer and routing-table observation collection."
        ),
    ),
    Control(
        key="network.tr069_sync",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(LegacyAlias(_NET, "tr069_sync_enabled", "TR069_SYNC_ENABLED"),),
        description="TR-069 inventory sync.",
    ),
    Control(
        key="network.tr069_command_admission",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        description=(
            "Admission of new TR-069 CPE commands. Accepted commands continue "
            "through durable dispatch when this capability is disabled."
        ),
    ),
    Control(
        key="network.tr069_health_check",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET, "tr069_health_check_enabled", "TR069_HEALTH_CHECK_ENABLED"
            ),
        ),
        description="TR-069 health check.",
    ),
    Control(
        key="network.tr069_cleanup",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(LegacyAlias(_NET, "tr069_cleanup_enabled", "TR069_CLEANUP_ENABLED"),),
        description="TR-069 cleanup.",
    ),
    Control(
        key="network.tr069_genieacs_stale_cleanup",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET,
                "tr069_genieacs_stale_cleanup_enabled",
                "TR069_GENIEACS_STALE_CLEANUP_ENABLED",
            ),
        ),
        description="GenieACS stale-device cleanup.",
    ),
    Control(
        key="network.tr069_metrics_scrape",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET, "tr069_metrics_scrape_enabled", "TR069_METRICS_SCRAPE_ENABLED"
            ),
        ),
        description="TR-069 metrics scrape.",
    ),
    Control(
        key="network.tr069_ont_runtime_refresh",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET,
                "tr069_ont_runtime_refresh_enabled",
                "TR069_ONT_RUNTIME_REFRESH_ENABLED",
            ),
        ),
        description="TR-069 ONT runtime refresh.",
    ),
    Control(
        key="vpn.log_cleanup",
        layer=Layer.feature,
        owner_module="vpn",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET, "wireguard_log_cleanup_enabled", "WIREGUARD_LOG_CLEANUP_ENABLED"
            ),
        ),
        description="WireGuard log cleanup.",
    ),
    Control(
        key="vpn.token_cleanup",
        layer=Layer.feature,
        owner_module="vpn",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET,
                "wireguard_token_cleanup_enabled",
                "WIREGUARD_TOKEN_CLEANUP_ENABLED",
            ),
        ),
        description="WireGuard token cleanup.",
    ),
    Control(
        key="crm.ticket_pull",
        layer=Layer.feature,
        owner_module="crm",
        default=False,
        on_missing=False,
        legacy=(
            LegacyAlias(_SCH, "crm_ticket_pull_enabled", "CRM_TICKET_PULL_ENABLED"),
        ),
        description="Pull tickets from CRM.",
    ),
    Control(
        key="quotes.native_read",
        layer=Layer.feature,
        default=False,
        on_missing=False,
        legacy=(
            LegacyAlias(
                _PRJ, "quotes_native_read_enabled", "QUOTES_NATIVE_READ_ENABLED"
            ),
        ),
        description="Serve quote reads from native quote tables.",
    ),
    Control(
        key="quotes.native_write",
        layer=Layer.feature,
        default=False,
        on_missing=False,
        legacy=(
            LegacyAlias(
                _PRJ, "quotes_native_write_enabled", "QUOTES_NATIVE_WRITE_ENABLED"
            ),
        ),
        description="Accept quote writes through the native quote/sales pipeline.",
    ),
    Control(
        key="ai.generation",
        layer=Layer.feature,
        default=False,
        on_missing=False,
        description=(
            "Generate AI insights (owned report projection -> LLM gateway -> ai.insights). "
            "Fails CLOSED: absent means no provider call and no spend."
        ),
    ),
    Control(
        key="sales.lead_dedup",
        layer=Layer.feature,
        default=True,
        on_missing=True,
        legacy=(LegacyAlias(_SUB, "lead_dedup_enabled", "LEAD_DEDUP_ENABLED"),),
        description="Prevent duplicate open leads per subscriber.",
    ),
    Control(
        key="gis.sync",
        layer=Layer.feature,
        owner_module="gis",
        default=True,
        on_missing=True,
        legacy=(LegacyAlias(_G, "sync_enabled", "GIS_SYNC_ENABLED"),),
        description="GIS sync.",
    ),
)


_CONTROLS: dict[str, Control] = {}
# Layer 1: every product module from the existing map, fail-open.
for _m in module_manager.MODULE_KEY_MAP:
    _CONTROLS[_m] = Control(
        key=_m,
        layer=Layer.module,
        default=True,
        on_missing=True,
        description=module_manager.MODULE_LABELS.get(_m, _m.title()),
    )
# Layer 2: capability features with scheduler/task ownership.
for _c in _FEATURE_CONTROLS:
    _CONTROLS[_c.key] = _c


def owner_module_for(canonical_key: str) -> str | None:
    """Owning module of a (feature) control, or None."""
    control = _CONTROLS.get(canonical_key)
    return control.owner_module if control else None


def all_controls() -> Iterable[Control]:
    return _CONTROLS.values()


def canonical_setting_key(control: Control) -> str:
    """Return the modules-domain key owned by the canonical feature writer."""
    return control.key.replace(".", "_")


def _db_setting(db: Session, domain: SettingDomain, key: str) -> DomainSetting | None:
    # query(...).filter(...).filter(...).filter(...).first() — the shape the
    # scheduler tests mock; returns None for "no row".
    return (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == domain)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )


def _db_value(db: Session, domain: SettingDomain, key: str) -> object | None:
    setting = _db_setting(db, domain, key)
    if setting is None:
        return None
    return setting.value_json if setting.value_json is not None else setting.value_text


def _resolve_own_flag_with_source(
    db: Session, control: Control
) -> tuple[bool, str, datetime | None]:
    canonical_key = canonical_setting_key(control)
    setting = _db_setting(db, SettingDomain.modules, canonical_key)
    if setting is not None:
        value = (
            setting.value_json if setting.value_json is not None else setting.value_text
        )
        return _truthy(value), f"database (modules.{canonical_key})", setting.updated_at

    return control.on_missing, "registry default", None


def _resolve_own_flag(db: Session, control: Control) -> bool:
    """Resolve a control's OWN value (ignoring module composition).

    Precedence: canonical DB row (``modules.<feature>``) → ``on_missing``
    default. Retired environment and database aliases are deliberately ignored.
    """
    return _resolve_own_flag_with_source(db, control)[0]


def resolve_control(db: Session, key: str) -> ControlResolution:
    """Resolve a registered control and explain its effective state.

    This is the read-only inspection counterpart to :func:`is_enabled`; both
    use the same precedence and module composition rules.
    """
    control = _CONTROLS.get(key)
    if control is None:
        enabled = is_enabled(db, key)
        return ControlResolution(
            key=key,
            enabled=enabled,
            own_enabled=enabled,
            source="implicit compatibility default",
            precedence="registered controls only",
            affected_scope=key,
        )

    precedence = "modules database row → registry default"
    if control.layer is Layer.module:
        setting_key = module_manager.MODULE_KEY_MAP[control.key]
        setting = _db_setting(db, SettingDomain.modules, setting_key)
        enabled = module_manager.is_module_enabled(db, control.key)
        return ControlResolution(
            key=key,
            enabled=enabled,
            own_enabled=enabled,
            source=(
                f"database (modules.{setting_key})"
                if setting is not None
                else "registry default"
            ),
            precedence="modules database row → registry default",
            affected_scope=f"{control.key} module and owned capabilities",
            updated_at=setting.updated_at if setting is not None else None,
        )

    canonical_setting = _db_setting(
        db, SettingDomain.modules, canonical_setting_key(control)
    )
    canonical_value = None
    if canonical_setting is not None:
        value = (
            canonical_setting.value_json
            if canonical_setting.value_json is not None
            else canonical_setting.value_text
        )
        canonical_value = _truthy(value)

    own_enabled, source, updated_at = _resolve_own_flag_with_source(db, control)
    module_enabled = (
        module_manager.is_module_enabled(db, control.owner_module)
        if control.owner_module
        else None
    )
    enabled = own_enabled and module_enabled is not False
    if module_enabled is False:
        source = f"owner module {control.owner_module} disabled; own source: {source}"
    return ControlResolution(
        key=key,
        enabled=enabled,
        own_enabled=own_enabled,
        source=source,
        precedence=precedence,
        affected_scope=(
            f"{control.owner_module} module / {control.key} capability"
            if control.owner_module
            else f"{control.key} capability"
        ),
        updated_at=updated_at,
        module_enabled=module_enabled,
        canonical_value=canonical_value,
        canonical_updated_at=(
            canonical_setting.updated_at if canonical_setting is not None else None
        ),
    )


def update_canonical_feature_controls(
    db: Session, *, payload: dict[str, bool | None]
) -> list[dict[str, object]]:
    """Persist explicit feature overrides through the canonical settings owner.

    ``None`` means inherit and deactivates the canonical row. Boolean values pin
    the canonical row on or off. The returned change record reports stored and
    effective state separately because the owner module can still mask a feature.
    """
    invalid = sorted(
        key
        for key in payload
        if key not in _CONTROLS or _CONTROLS[key].layer is not Layer.feature
    )
    if invalid:
        raise ValueError(f"Unknown feature controls: {', '.join(invalid)}")

    from app.services.control_relationships import validate_feature_control_changes

    validate_feature_control_changes(db, payload)

    changes: list[dict[str, object]] = []
    for key, requested_value in payload.items():
        control = _CONTROLS[key]
        before = resolve_control(db, key)
        if before.canonical_value is requested_value:
            continue

        setting_key = canonical_setting_key(control)
        setting = _db_setting(db, SettingDomain.modules, setting_key)
        if requested_value is None:
            if setting is not None:
                domain_settings_service.modules_settings.delete(db, str(setting.id))
        else:
            domain_settings_service.modules_settings.upsert_by_key(
                db,
                setting_key,
                DomainSettingUpdate(
                    domain=SettingDomain.modules,
                    value_type=SettingValueType.boolean,
                    value_text="true" if requested_value else "false",
                    value_json=None,
                    is_active=True,
                ),
            )

        after = resolve_control(db, key)
        changes.append(
            {
                "key": key,
                "stored": {
                    "from": before.canonical_value,
                    "to": after.canonical_value,
                },
                "effective": {"from": before.enabled, "to": after.enabled},
                "source": {"from": before.source, "to": after.source},
            }
        )
    return changes


def is_enabled(db: Session, key: str) -> bool:
    """Resolve an optional capability control.

    Core customer-financial lifecycle services are intentionally absent from
    this registry: they are permanent runtime responsibilities, not features
    that operators can disable.
    """
    control = _CONTROLS.get(key)
    if control is None:
        # Unknown key: be conservative-but-non-breaking — treat a bare module
        # name via module_manager, else default on (callers should register).
        if "." not in key:
            return module_manager.is_module_enabled(db, key)
        logger.warning("control_registry: unknown control %r; defaulting on", key)
        return True

    if control.layer is Layer.module:
        return module_manager.is_module_enabled(db, control.key)

    # Feature: module gate AND the feature's own flag.
    if control.owner_module and not module_manager.is_module_enabled(
        db, control.owner_module
    ):
        return False
    return _resolve_own_flag(db, control)
