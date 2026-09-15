"""Helpers for processing admin system settings form submissions."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import db_session_adapter, domain_settings, settings_spec
from app.services import web_system_settings_views as web_system_settings_views_service
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.settings_api_custom import (
    SettingNormalizationError,
    _normalize_spec_setting,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SettingsFormUpdateResult:
    settings_context: dict[str, object]
    errors: tuple[str, ...]


def form_bool(value: object | None) -> bool:
    """Parse common HTML form boolean values."""
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _is_blank(value: object | None) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _raw_update_payload(
    spec: settings_spec.SettingSpec,
    value: object,
) -> DomainSettingUpdate:
    if isinstance(value, dict | list):
        return DomainSettingUpdate(
            value_type=spec.value_type,
            value_json=value,
            is_secret=spec.is_secret,
            is_active=True,
        )
    return DomainSettingUpdate(
        value_text=str(value),
        is_secret=spec.is_secret,
        is_active=True,
    )


def _prepare_settings_from_specs(
    *,
    form: Mapping[str, object],
    specs: Sequence[settings_spec.SettingSpec],
) -> tuple[tuple[domain_settings.AdminSettingWrite, ...], tuple[str, ...]]:
    updates: list[domain_settings.AdminSettingWrite] = []
    errors: list[str] = []

    for spec in specs:
        raw = form.get(spec.key)
        blank = _is_blank(raw)
        if spec.is_secret and blank:
            continue

        if spec.value_type == SettingValueType.boolean:
            value: object = form_bool(raw)
        elif blank:
            if spec.default is None:
                if spec.required:
                    errors.append(f"{spec.key}: Value is required.")
                # Optional settings without defaults are represented by no row,
                # not by an invalid typed row with no value.
                continue
            value = spec.default
        else:
            value = raw

        try:
            payload = _normalize_spec_setting(
                spec.domain,
                spec.key,
                _raw_update_payload(spec, value),
            )
        except SettingNormalizationError as exc:
            errors.append(f"{spec.key}: {exc.message}")
            continue
        except (TypeError, ValueError):
            errors.append(f"{spec.key}: Invalid setting value.")
            continue

        updates.append(
            domain_settings.AdminSettingWrite(
                domain=spec.domain,
                key=spec.key,
                payload=payload,
            )
        )

    return tuple(updates), tuple(errors)


def _apply_prepared_settings(
    *,
    db: Session,
    context: CommandContext,
    updates: tuple[domain_settings.AdminSettingWrite, ...],
) -> tuple[str, ...]:
    if not updates:
        return ()
    try:
        db_session_adapter.db_session_adapter.release_read_transaction(db)
        domain_settings.apply_admin_settings_form_updates(
            db,
            domain_settings.ApplyAdminSettingsFormCommand(
                context=context,
                updates=updates,
            ),
        )
    except DomainError as exc:
        return (exc.message,)
    except Exception as exc:
        logger.error(
            "Admin settings form update failed",
            extra={"error_type": type(exc).__name__},
        )
        return ("Settings could not be saved. No changes were made.",)
    return ()


def upsert_settings_from_specs(
    *,
    db: Session,
    form: Mapping[str, object],
    specs: Sequence[settings_spec.SettingSpec],
    service: domain_settings.DomainSettings,
    context: CommandContext,
    skip_blank_secrets: bool = True,
) -> list[str]:
    """Validate the complete submission, then save it in one transaction."""
    if not skip_blank_secrets:
        raise ValueError("Admin setting forms must preserve blank secrets.")
    if service.domain is None or any(spec.domain != service.domain for spec in specs):
        return ["Settings service does not match the submitted domain."]
    updates, errors = _prepare_settings_from_specs(form=form, specs=specs)
    if errors:
        return list(errors)
    return list(_apply_prepared_settings(db=db, context=context, updates=updates))


def process_settings_update(
    *,
    db: Session,
    domain_value: str | None,
    form: Mapping[str, object],
    context: CommandContext,
) -> SettingsFormUpdateResult:
    """Validate and atomically apply one posted settings form."""
    errors: list[str] = []
    updates: list[domain_settings.AdminSettingWrite] = []

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
                errors.extend(
                    f"{spec.key}: Settings service not configured."
                    for spec in domain_specs
                )
                continue
            prepared, preparation_errors = _prepare_settings_from_specs(
                form=form,
                specs=domain_specs,
            )
            updates.extend(prepared)
            errors.extend(preparation_errors)
        selected_domain_value = web_system_settings_views_service.ENFORCEMENT_DOMAIN
    elif domain_value == web_system_settings_views_service.BRANDING_DOMAIN:
        selected_domain_value = web_system_settings_views_service.BRANDING_DOMAIN
        errors.append("Use the Branding form to update logo settings.")
    else:
        selected_domain = web_system_settings_views_service.resolve_settings_domain(
            domain_value
        )
        selected_domain_value = selected_domain.value
        specs = settings_spec.list_specs(selected_domain)
        service = settings_spec.DOMAIN_SETTINGS_SERVICE.get(selected_domain)
        if not service:
            errors.append("Settings service not configured for this domain.")
        else:
            prepared, preparation_errors = _prepare_settings_from_specs(
                form=form,
                specs=specs,
            )
            updates.extend(prepared)
            errors.extend(preparation_errors)

    if not errors:
        errors.extend(
            _apply_prepared_settings(
                db=db,
                context=context,
                updates=tuple(updates),
            )
        )

    settings_context = web_system_settings_views_service.build_settings_context(
        db,
        selected_domain_value,
    )
    return SettingsFormUpdateResult(
        settings_context=settings_context,
        errors=tuple(errors),
    )
