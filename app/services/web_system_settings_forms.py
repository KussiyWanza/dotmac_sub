"""Helpers for processing admin system settings form submissions."""

from __future__ import annotations

import logging
from typing import cast

from app.schemas.settings import DomainSettingUpdate
from app.services import settings_spec
from app.services import web_system_settings_views as web_system_settings_views_service

logger = logging.getLogger(__name__)


def form_bool(value: str | None) -> bool:
    """Parse common HTML form boolean values."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def upsert_settings_from_specs(
    *,
    db,
    form,
    specs: list[settings_spec.SettingSpec],
    service,
    skip_blank_secrets: bool = True,
) -> list[str]:
    """Validate/normalize submitted spec values and upsert to settings store."""
    errors: list[str] = []

    for spec in specs:
        raw = form.get(spec.key)
        if skip_blank_secrets and spec.is_secret and (raw is None or raw == ""):
            continue

        value: object
        if spec.value_type == settings_spec.SettingValueType.boolean:
            value = form_bool(raw)
        elif spec.value_type == settings_spec.SettingValueType.integer:
            if raw in (None, ""):
                value = spec.default
            else:
                try:
                    value = int(str(raw))
                except ValueError:
                    errors.append(f"{spec.key}: Value must be an integer.")
                    continue
        else:
            if raw in (None, ""):
                if spec.value_type == settings_spec.SettingValueType.string:
                    value = spec.default if spec.default is not None else ""
                elif spec.value_type == settings_spec.SettingValueType.json:
                    value = spec.default if spec.default is not None else {}
                else:
                    value = spec.default
            else:
                value = raw

        if spec.allowed and value is not None and value not in spec.allowed:
            errors.append(
                f"{spec.key}: Value must be one of {', '.join(sorted(spec.allowed))}."
            )
            continue

        if isinstance(value, int):
            if spec.min_value is not None and value < spec.min_value:
                errors.append(f"{spec.key}: Minimum value is {spec.min_value}.")
                continue
            if spec.max_value is not None and value > spec.max_value:
                errors.append(f"{spec.key}: Maximum value is {spec.max_value}.")
                continue

        if value is None:
            value_text, value_json = None, None
        else:
            value_text, value_json_raw = settings_spec.normalize_for_db(spec, value)
            value_json = cast(
                dict[object, object] | list[object] | bool | int | str | None,
                value_json_raw,
            )
            # Non-JSON settings must always use the text column. Keep this
            # boundary defensive so a deployed spec mismatch cannot produce
            # a validation 500 for a valid integer setting submission.
            if spec.value_type in {
                settings_spec.SettingValueType.integer,
                settings_spec.SettingValueType.string,
            }:
                value_text = str(
                    value_json_raw if value_json_raw is not None else value
                )
                value_json = None

        payload = DomainSettingUpdate(
            value_type=spec.value_type,
            value_text=value_text,
            value_json=value_json,
            is_secret=spec.is_secret,
            is_active=True,
        )
        service.upsert_by_key(db, spec.key, payload)

    return errors


def process_settings_update(
    *,
    db,
    domain_value: str | None,
    form,
) -> tuple[dict, list[str]]:
    """Apply posted settings for a domain and return refreshed context + errors."""
    errors: list[str] = []
    if domain_value == web_system_settings_views_service.ENFORCEMENT_DOMAIN:
        specs = web_system_settings_views_service.enforcement_specs()
        domain_to_specs: dict[
            settings_spec.SettingDomain, list[settings_spec.SettingSpec]
        ] = {}
        for spec in specs:
            domain_to_specs.setdefault(spec.domain, []).append(spec)
        for spec_domain, domain_specs in domain_to_specs.items():
            service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(spec_domain)
            if not service:
                for spec in domain_specs:
                    errors.append(f"{spec.key}: Settings service not configured.")
                continue
            errors.extend(
                upsert_settings_from_specs(
                    db=db,
                    form=form,
                    specs=domain_specs,
                    service=service,
                )
            )
        settings_context = web_system_settings_views_service.build_settings_context(
            db,
            web_system_settings_views_service.ENFORCEMENT_DOMAIN,
        )
    elif domain_value == web_system_settings_views_service.BRANDING_DOMAIN:
        settings_context = web_system_settings_views_service.build_settings_context(
            db,
            web_system_settings_views_service.BRANDING_DOMAIN,
        )
        errors.append("Use the Branding form to update logo settings.")
    else:
        selected_domain = web_system_settings_views_service.resolve_settings_domain(
            domain_value
        )
        specs = settings_spec.list_specs(selected_domain)
        service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(selected_domain)
        if not service:
            errors.append("Settings service not configured for this domain.")
        else:
            errors.extend(
                upsert_settings_from_specs(
                    db=db,
                    form=form,
                    specs=specs,
                    service=service,
                )
            )
        settings_context = web_system_settings_views_service.build_settings_context(
            db,
            selected_domain.value,
        )
    return settings_context, errors
