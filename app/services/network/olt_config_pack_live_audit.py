"""Live OLT config-pack audit.

This module compares Dotmac's saved OLT config pack against read-only Huawei
OLT profile output. It is intended for OLT onboarding and periodic audits, not
per-ONT authorization.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OltLineProfile,
    OltOnuTypeProfileMapping,
    OltServiceProfile,
)
from app.services.network.huawei_cli_response import (
    HuaweiCliResource,
    is_huawei_resource_absent,
)
from app.services.network.olt_config_pack import resolve_olt_config_pack
from app.services.network.olt_profile_resolution import parse_line_profile_tr069_enabled
from app.services.network.olt_ssh_profiles import (
    _parse_tr069_profile_detail,
    get_dba_profiles,
    get_tr069_server_profiles,
    get_traffic_tables,
    get_wan_profiles,
)

logger = logging.getLogger(__name__)


class OltDependencyAuditScope(StrEnum):
    """Live dependency sets required by one OLT write workflow."""

    FULL = "full"
    MANAGEMENT_ONLY = "management_only"


@dataclass(frozen=True)
class LiveLineProfileDetail:
    profile_id: int
    gem_indexes: set[int] = field(default_factory=set)
    tr069_management_enabled: bool = False
    tr069_ip_index: int | None = None


@dataclass(frozen=True)
class LiveTr069ProfileDetail:
    profile_id: int
    exists: bool
    name: str = ""
    acs_url: str = ""


@dataclass
class OltConfigPackLiveAudit:
    olt_id: str
    olt_name: str
    success: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return self.success and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "olt_id": self.olt_id,
            "olt_name": self.olt_name,
            "success": self.success,
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "observed": self.observed,
        }


@dataclass(frozen=True)
class CompatibleLineProfileSuggestion:
    profile_id: int
    name: str
    gem_indexes: set[int]
    tr069_management_enabled: bool
    tr069_ip_index: int | None
    binding_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "gem_indexes": sorted(self.gem_indexes),
            "tr069_management_enabled": self.tr069_management_enabled,
            "tr069_ip_index": self.tr069_ip_index,
            "binding_count": self.binding_count,
        }


_DBA_PROFILE_REF_RE = re.compile(
    r"\bdba\s+profile[- ]?id\s*[:=]?\s*(\d+)|\bdba-profile-id\s*[:=]?\s*(\d+)",
    re.IGNORECASE,
)


def extract_dba_profile_ids(raw_config: str | None) -> set[int]:
    """Extract DBA profile references from imported Huawei line-profile config."""
    if not raw_config:
        return set()
    return {
        int(match.group(1) or match.group(2))
        for match in _DBA_PROFILE_REF_RE.finditer(raw_config)
    }


def parse_line_profile_detail(output: str, *, profile_id: int) -> LiveLineProfileDetail:
    """Parse Huawei line-profile detail for GEM/TR-069 compatibility checks."""
    gem_indexes: set[int] = set()
    for match in re.finditer(r"<\s*Gem\s+Index\s+(\d+)\s*>", output, re.IGNORECASE):
        gem_indexes.add(int(match.group(1)))

    tr069_ip_index: int | None = None
    ip_index_match = re.search(r"\bTR069\s+IP\s+index\s*:\s*(\d+)", output, re.I)
    if ip_index_match:
        tr069_ip_index = int(ip_index_match.group(1))

    return LiveLineProfileDetail(
        profile_id=profile_id,
        gem_indexes=gem_indexes,
        tr069_management_enabled=parse_line_profile_tr069_enabled(output),
        tr069_ip_index=tr069_ip_index,
    )


def parse_tr069_profile_detail(
    output: str, *, profile_id: int
) -> LiveTr069ProfileDetail:
    """Parse Huawei TR-069 server profile detail enough to confirm existence."""
    if is_huawei_resource_absent(output, HuaweiCliResource.PROFILE):
        return LiveTr069ProfileDetail(profile_id=profile_id, exists=False)

    values = _parse_tr069_profile_detail(output)
    name = (
        values.get("profile-name")
        or values.get("profile name")
        or values.get("name")
        or ""
    )
    acs_url = values.get("url") or values.get("acs url") or values.get("acs-url") or ""
    return LiveTr069ProfileDetail(
        profile_id=profile_id,
        exists=True,
        name=name,
        acs_url=acs_url,
    )


def _open_enabled_olt_shell(olt: OLTDevice):
    from app.services.network import olt_ssh as core

    try:
        transport, channel, policy = core._open_shell(olt)
    except (core.SSHException, OSError, TimeoutError, ValueError) as exc:
        return False, f"Connection failed: {exc}", None, None, None

    channel.send("enable\n")
    core._read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)
    channel.send("screen-length 0 temporary\n")
    core._read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)
    return True, "Connected.", transport, channel, policy


def _run_live_tr069_profile_command(olt: OLTDevice, *, tr069_profile_id: int):
    from app.services.network import olt_ssh as core

    ok, message, transport, channel, policy = _open_enabled_olt_shell(olt)
    if not ok or transport is None or channel is None or policy is None:
        return False, message, None

    try:
        tr069_output = core._run_huawei_paged_cmd(
            channel,
            f"display ont tr069-server-profile profile-id {tr069_profile_id}",
            prompt=policy.prompt_regex,
        )
        return True, "Live TR-069 profile command completed.", tr069_output
    except Exception as exc:
        logger.exception("Live OLT config-pack audit failed for OLT %s", olt.name)
        return False, f"Live profile read failed: {exc}", None
    finally:
        transport.close()


def _required_traffic_table_ids(pack: Any) -> dict[str, int]:
    fields = (
        "mgmt_traffic_table_inbound",
        "mgmt_traffic_table_outbound",
        "internet_traffic_table_inbound",
        "internet_traffic_table_outbound",
    )
    return {
        field: int(value)
        for field in fields
        if (value := getattr(pack, field, None)) is not None
    }


def _collect_imported_profile_dependencies(
    db: Session,
    *,
    olt_id: Any,
) -> tuple[dict[str, Any], list[str], list[str]]:
    line_profiles = db.scalars(
        select(OltLineProfile).where(OltLineProfile.olt_id == olt_id)
    ).all()
    service_profiles = db.scalars(
        select(OltServiceProfile).where(OltServiceProfile.olt_id == olt_id)
    ).all()
    mappings = db.scalars(
        select(OltOnuTypeProfileMapping).where(
            OltOnuTypeProfileMapping.olt_id == olt_id
        )
    ).all()

    line_profile_ids = {profile.profile_id for profile in line_profiles}
    service_profile_ids = {profile.profile_id for profile in service_profiles}
    mapped_line_ids = {mapping.line_profile_id for mapping in mappings}
    mapped_service_ids = {mapping.service_profile_id for mapping in mappings}
    missing_line_ids = sorted(mapped_line_ids - line_profile_ids)
    missing_service_ids = sorted(mapped_service_ids - service_profile_ids)

    line_profiles_by_id = {profile.profile_id: profile for profile in line_profiles}
    required_dba_ids: set[int] = set()
    for profile_id in mapped_line_ids:
        profile = line_profiles_by_id.get(profile_id)
        if profile is not None:
            required_dba_ids.update(extract_dba_profile_ids(profile.raw_config))

    mapping_wan_profile_ids = sorted(
        {
            int(mapping.wan_config_profile_id)
            for mapping in mappings
            if mapping.wan_config_profile_id is not None
        }
    )
    observed = {
        "imported_line_profile_ids": sorted(line_profile_ids),
        "imported_service_profile_ids": sorted(service_profile_ids),
        "mapping_count": len(mappings),
        "mapped_line_profile_ids": sorted(mapped_line_ids),
        "mapped_service_profile_ids": sorted(mapped_service_ids),
        "required_dba_profile_ids": sorted(required_dba_ids),
        "mapping_wan_config_profile_ids": mapping_wan_profile_ids,
    }

    errors: list[str] = []
    warnings: list[str] = []
    if mappings and missing_line_ids:
        errors.append(
            "Imported ONU type mappings reference missing line profile(s): "
            + ", ".join(str(profile_id) for profile_id in missing_line_ids)
        )
    if mappings and missing_service_ids:
        errors.append(
            "Imported ONU type mappings reference missing service profile(s): "
            + ", ".join(str(profile_id) for profile_id in missing_service_ids)
        )
    if not mappings:
        errors.append(
            "No imported ONU type profile mappings found; run Import OLT State before provisioning."
        )

    return observed, errors, warnings


def suggest_compatible_line_profiles(
    db: Session,
    olt_id: str,
) -> tuple[bool, str, list[CompatibleLineProfileSuggestion]]:
    """Deprecated: line profiles are now imported into OLT mapping tables."""
    olt = db.get(OLTDevice, str(olt_id))
    if olt is None:
        return False, "OLT device not found", []
    pack = resolve_olt_config_pack(db, str(olt.id))
    if pack is None:
        return False, "OLT config pack could not be resolved", []
    del pack
    return (
        False,
        "Line profile suggestions are deprecated; run Import OLT State and use imported mapping coverage.",
        [],
    )


def audit_olt_config_pack_live(
    db: Session,
    olt_id: str,
    *,
    scope: OltDependencyAuditScope = OltDependencyAuditScope.FULL,
) -> OltConfigPackLiveAudit:
    """Compare one OLT's saved config pack with live OLT profile output."""
    scope = OltDependencyAuditScope(scope)
    olt = db.get(OLTDevice, str(olt_id))
    if olt is None:
        return OltConfigPackLiveAudit(
            olt_id=str(olt_id),
            olt_name="unknown",
            success=False,
            errors=["OLT device not found"],
        )

    audit = OltConfigPackLiveAudit(
        olt_id=str(olt.id),
        olt_name=olt.name or str(olt.id),
        success=False,
    )
    audit.observed["dependency_scope"] = scope.value
    pack = resolve_olt_config_pack(db, str(olt.id))
    if pack is None:
        audit.errors.append("OLT config pack could not be resolved")
        return audit

    required = {
        "tr069_olt_profile_id": pack.tr069_olt_profile_id,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        audit.errors.append(
            "Config pack missing fields required for live audit: " + ", ".join(missing)
        )
        return audit

    imported_observed, imported_errors, imported_warnings = (
        _collect_imported_profile_dependencies(db, olt_id=olt.id)
    )
    audit.observed.update(imported_observed)
    audit.errors.extend(imported_errors)
    audit.warnings.extend(imported_warnings)
    required_tr069_id = int(pack.tr069_olt_profile_id)  # type: ignore[arg-type]
    required_traffic_ids = (
        _required_traffic_table_ids(pack)
        if scope is OltDependencyAuditScope.FULL
        else {}
    )
    required_wan_profile_ids = (
        sorted(
            {
                profile_id
                for profile_id in [
                    pack.wan_config_profile_id,
                    *audit.observed.get("mapping_wan_config_profile_ids", []),
                ]
                if profile_id is not None
            }
        )
        if scope is OltDependencyAuditScope.FULL
        else []
    )

    # Live inventory reads can take long enough for PostgreSQL's
    # idle-in-transaction timeout to kill the connection. Release the read
    # transaction before opening SSH sessions; all DB-derived values needed
    # below have been copied to primitives.
    previous = db.expire_on_commit
    db.expire_on_commit = False
    try:
        db.commit()
    finally:
        db.expire_on_commit = previous

    ok, message, tr069_profiles = get_tr069_server_profiles(olt)
    if not ok:
        audit.errors.append(f"Live TR-069 profile inventory failed: {message}")
    tr069_profile_ids = {profile.profile_id for profile in tr069_profiles}
    tr069_detail = next(
        (
            profile
            for profile in tr069_profiles
            if profile.profile_id == required_tr069_id
        ),
        None,
    )
    audit.observed.update(
        {
            "tr069_profile_id": required_tr069_id,
            "tr069_profile_exists": required_tr069_id in tr069_profile_ids,
            "tr069_profile_name": tr069_detail.name if tr069_detail else "",
            "tr069_profile_acs_url": tr069_detail.acs_url if tr069_detail else "",
            "live_tr069_profile_ids": sorted(tr069_profile_ids),
        }
    )
    if ok and required_tr069_id not in tr069_profile_ids:
        audit.errors.append(
            f"Live OLT TR-069 server profile {pack.tr069_olt_profile_id} was not found"
        )

    ok, message, dba_profiles = get_dba_profiles(olt)
    if not ok:
        audit.errors.append(f"Live DBA profile inventory failed: {message}")
    live_dba_ids = {profile.profile_id for profile in dba_profiles}
    required_dba_ids = set(audit.observed.get("required_dba_profile_ids") or [])
    missing_dba_ids = sorted(required_dba_ids - live_dba_ids) if ok else []
    audit.observed.update(
        {
            "live_dba_profile_ids": sorted(live_dba_ids),
            "missing_dba_profile_ids": missing_dba_ids,
        }
    )
    if missing_dba_ids:
        audit.errors.append(
            "Imported line profiles reference missing DBA profile(s): "
            + ", ".join(str(profile_id) for profile_id in missing_dba_ids)
        )

    live_traffic_ids: set[int] = set()
    missing_traffic_fields: dict[str, int] = {}
    if scope is OltDependencyAuditScope.FULL:
        ok, message, traffic_tables = get_traffic_tables(olt)
        if not ok:
            audit.errors.append(f"Live traffic table inventory failed: {message}")
        live_traffic_ids = {table.index for table in traffic_tables}
        missing_traffic_fields = {
            field: table_id
            for field, table_id in required_traffic_ids.items()
            if ok and table_id not in live_traffic_ids
        }
    audit.observed.update(
        {
            "required_traffic_table_ids": required_traffic_ids,
            "live_traffic_table_ids": sorted(live_traffic_ids),
            "missing_traffic_table_ids": missing_traffic_fields,
        }
    )
    if missing_traffic_fields:
        audit.errors.append(
            "Config pack references missing traffic table(s): "
            + ", ".join(
                f"{field}={table_id}"
                for field, table_id in sorted(missing_traffic_fields.items())
            )
        )

    live_wan_ids: set[int] = set()
    missing_wan_profile_ids: list[int] = []
    if scope is OltDependencyAuditScope.FULL:
        ok, message, wan_profiles = get_wan_profiles(olt)
        if not ok:
            audit.errors.append(f"Live WAN profile inventory failed: {message}")
        live_wan_ids = {profile.profile_id for profile in wan_profiles}
        allowed_missing_wan_ids = (
            {0} if getattr(pack, "allow_zero_wan_config_profile_id", False) else set()
        )
        missing_wan_profile_ids = (
            sorted(
                set(required_wan_profile_ids) - live_wan_ids - allowed_missing_wan_ids
            )
            if ok
            else []
        )
    audit.observed.update(
        {
            "required_wan_config_profile_ids": required_wan_profile_ids,
            "live_wan_config_profile_ids": sorted(live_wan_ids),
            "missing_wan_config_profile_ids": missing_wan_profile_ids,
        }
    )
    if missing_wan_profile_ids:
        audit.errors.append(
            "Config pack or mappings reference missing WAN config profile(s): "
            + ", ".join(str(profile_id) for profile_id in missing_wan_profile_ids)
        )

    audit.success = not any("inventory failed" in error for error in audit.errors)

    return audit
