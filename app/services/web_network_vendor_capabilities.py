"""Service helpers for admin vendor model capability web routes."""

from __future__ import annotations

import json
import logging

from fastapi import Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.network import Tr069ParameterMap, VendorModelCapability
from app.services.network.vendor_capabilities import (
    tr069_parameter_maps,
    vendor_capabilities,
)

logger = logging.getLogger(__name__)


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value.strip() if isinstance(value, str) else default


def _form_int(form: FormData, key: str, default: int | None = None) -> int | None:
    raw = _form_str(form, key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _form_bool(form: FormData, key: str) -> bool:
    return _form_str(form, key) == "true"


def list_context(
    request: Request,
    db: Session,
    search: str | None = None,
    vendor: str | None = None,
) -> dict[str, object]:
    """Return context dict for the vendor capabilities list page."""
    from app.web.admin import get_current_user, get_sidebar_stats

    items = vendor_capabilities.list(db, search=search, vendor=vendor, is_active=None)
    vendors = vendor_capabilities.list_vendors(db)
    return {
        "request": request,
        "active_page": "vendor-capabilities",
        "active_menu": "network",
        "items": items,
        "vendors": vendors,
        "search": search or "",
        "vendor_filter": vendor or "",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def form_context(
    request: Request,
    db: Session,
    capability_id: str | None = None,
) -> dict[str, object]:
    """Return context dict for the create/edit form."""
    from app.web.admin import get_current_user, get_sidebar_stats

    item = vendor_capabilities.get(db, capability_id) if capability_id else None

    # Load parameter maps for this capability
    parameter_maps: list[Tr069ParameterMap] = []
    if item:
        parameter_maps = tr069_parameter_maps.list_for_capability(db, str(item.id))

    return {
        "request": request,
        "active_page": "vendor-capabilities",
        "active_menu": "network",
        "item": item,
        "parameter_maps": parameter_maps,
        "action_url": (
            f"/admin/network/vendor-capabilities/{capability_id}/edit"
            if capability_id
            else "/admin/network/vendor-capabilities/create"
        ),
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def parse_capability_form(form: FormData) -> dict[str, object]:
    """Parse vendor capability form fields into normalized values."""
    # Parse supported_features JSON
    features_raw = _form_str(form, "supported_features")
    supported_features: dict[str, object] = {}
    if features_raw:
        try:
            supported_features = json.loads(features_raw)
        except json.JSONDecodeError:
            pass

    return {
        "vendor": _form_str(form, "vendor"),
        "model": _form_str(form, "model"),
        "firmware_pattern": _form_str(form, "firmware_pattern") or None,
        "tr069_root": _form_str(form, "tr069_root") or None,
        "supported_features": supported_features,
        "max_wan_services": _form_int(form, "max_wan_services", 1),
        "max_lan_ports": _form_int(form, "max_lan_ports", 4),
        "max_ssids": _form_int(form, "max_ssids", 2),
        "supports_vlan_tagging": _form_bool(form, "supports_vlan_tagging"),
        "supports_qinq": _form_bool(form, "supports_qinq"),
        "supports_ipv6": _form_bool(form, "supports_ipv6"),
        "notes": _form_str(form, "notes") or None,
    }


def validate_capability_form(values: dict[str, object]) -> str | None:
    """Validate capability form values. Returns error message or None."""
    vendor = values.get("vendor")
    if not vendor or not str(vendor).strip():
        return "Vendor name is required."
    model = values.get("model")
    if not model or not str(model).strip():
        return "Model name is required."
    return None


def handle_create(db: Session, form_data: dict[str, object]) -> VendorModelCapability:
    """Create a new vendor capability from validated form values."""
    return vendor_capabilities.create(
        db,
        vendor=str(form_data["vendor"]),
        model=str(form_data["model"]),
        firmware_pattern=str(form_data["firmware_pattern"])
        if form_data.get("firmware_pattern")
        else None,
        tr069_root=str(form_data["tr069_root"])
        if form_data.get("tr069_root")
        else None,
        supported_features=form_data["supported_features"]
        if form_data.get("supported_features")
        else None,  # type: ignore[arg-type]
        max_wan_services=int(str(form_data.get("max_wan_services") or 1)),
        max_lan_ports=int(str(form_data.get("max_lan_ports") or 4)),
        max_ssids=int(str(form_data.get("max_ssids") or 2)),
        supports_vlan_tagging=bool(form_data.get("supports_vlan_tagging")),
        supports_qinq=bool(form_data.get("supports_qinq")),
        supports_ipv6=bool(form_data.get("supports_ipv6")),
        notes=str(form_data["notes"]) if form_data.get("notes") else None,
    )


def handle_update(
    db: Session,
    capability_id: str,
    form_data: dict[str, object],
) -> VendorModelCapability:
    """Update a vendor capability from validated form values."""
    return vendor_capabilities.update(
        db,
        capability_id,
        vendor=str(form_data["vendor"]),
        model=str(form_data["model"]),
        firmware_pattern=str(form_data["firmware_pattern"])
        if form_data.get("firmware_pattern")
        else None,
        tr069_root=str(form_data["tr069_root"])
        if form_data.get("tr069_root")
        else None,
        supported_features=form_data["supported_features"]
        if form_data.get("supported_features")
        else None,
        max_wan_services=int(str(form_data.get("max_wan_services") or 1)),
        max_lan_ports=int(str(form_data.get("max_lan_ports") or 4)),
        max_ssids=int(str(form_data.get("max_ssids") or 2)),
        supports_vlan_tagging=bool(form_data.get("supports_vlan_tagging")),
        supports_qinq=bool(form_data.get("supports_qinq")),
        supports_ipv6=bool(form_data.get("supports_ipv6")),
        notes=str(form_data["notes"]) if form_data.get("notes") else None,
    )


def handle_delete(db: Session, capability_id: str) -> None:
    """Soft-delete a vendor capability."""
    vendor_capabilities.delete(db, capability_id)


def parse_param_map_form(form: FormData) -> dict[str, object]:
    """Parse TR-069 parameter map form fields."""
    return {
        "canonical_name": _form_str(form, "canonical_name"),
        "tr069_path": _form_str(form, "tr069_path"),
        "writable": _form_bool(form, "writable"),
        "value_type": _form_str(form, "value_type") or None,
        "notes": _form_str(form, "param_notes") or None,
    }


def validate_param_map_form(values: dict[str, object]) -> str | None:
    """Validate parameter map form values."""
    cn = values.get("canonical_name")
    if not cn or not str(cn).strip():
        return "Canonical name is required."
    tp = values.get("tr069_path")
    if not tp or not str(tp).strip():
        return "TR-069 path is required."
    return None


def handle_param_map_create(
    db: Session, capability_id: str, form_data: dict[str, object]
) -> Tr069ParameterMap:
    """Create a TR-069 parameter map from validated form values."""
    return tr069_parameter_maps.create(
        db,
        capability_id=capability_id,
        canonical_name=str(form_data["canonical_name"]),
        tr069_path=str(form_data["tr069_path"]),
        writable=bool(form_data.get("writable")),
        value_type=str(form_data["value_type"])
        if form_data.get("value_type")
        else None,
        notes=str(form_data["notes"]) if form_data.get("notes") else None,
    )


def handle_param_map_delete(db: Session, param_map_id: str) -> None:
    """Delete a TR-069 parameter map."""
    tr069_parameter_maps.delete(db, param_map_id)
