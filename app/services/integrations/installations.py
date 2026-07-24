"""Canonical additive owner for connector installation configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationConfigRevision,
    IntegrationInstallation,
    IntegrationInstallationState,
    IntegrationValidationStatus,
)
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.integrations.connectors.http_webhook import validate_https_url
from app.services.integrations.manifest import (
    ConnectorManifest,
    ConnectorRuntimeType,
)
from app.services.integrations.registry import (
    connector_definition,
    pinned_connector_definition,
    require_connector_definition,
    require_pinned_connector_definition,
)
from app.services.integrations.runtime import ValidationResult
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.secrets import is_secret_ref


class InstallationError(ValueError):
    """Raised when an installation violates its manifest or lifecycle."""


class ManifestAdoptionError(DomainError, ValueError):
    """Stable rejection from the installation manifest-adoption owner."""


class CapabilityProvisioningError(DomainError, ValueError):
    """Stable rejection from the installation capability-provisioning owner."""


class ManifestPinState(StrEnum):
    """Deployment support state for one persisted installation pin."""

    current = "current"
    supported_historical = "supported_historical"
    unavailable = "unavailable"


@dataclass(frozen=True, slots=True)
class ManifestPin:
    connector_version: str
    manifest_digest: str


@dataclass(frozen=True, slots=True)
class ManifestPinCheck:
    installation_id: UUID
    connector_key: str
    installation_state: str
    installed_pin: ManifestPin
    deployed_pin: ManifestPin | None
    state: ManifestPinState


@dataclass(frozen=True, slots=True)
class ManifestAdoptionPreview:
    installation_id: UUID
    connector_key: str
    environment: str
    installation_state: str
    installed_pin: ManifestPin
    target_pin: ManifestPin | None
    pin_state: ManifestPinState
    adoption_required: bool
    ready: bool
    blocking_errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdoptManifestCommand:
    installation_id: UUID
    expected_installed_pin: ManifestPin
    target_pin: ManifestPin


@dataclass(frozen=True, slots=True)
class ManifestAdoptionResult:
    installation_id: UUID
    connector_key: str
    previous_pin: ManifestPin
    adopted_pin: ManifestPin
    installation_state: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class ProvisionCapabilityCommand:
    installation_id: UUID
    capability_id: str
    expected_installed_pin: ManifestPin
    expected_binding_id: UUID | None
    expected_binding_state: IntegrationBindingState | None
    capability_scope: Mapping[str, object]
    policy: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CapabilityProvisioningResult:
    installation_id: UUID
    connector_key: str
    capability_binding_id: UUID
    capability_id: str
    installation_state: str
    binding_state: str
    replayed: bool


MANIFEST_ADOPTION_SCOPE = "integration-installation:adopt-manifest"
CAPABILITY_PROVISIONING_SCOPE = "integration-installation:provision-capability"
_ADOPT_MANIFEST_COMMAND = OwnerCommandDefinition(
    owner="integration.installations",
    concern="explicit integration manifest adoption",
    name="adopt_installation_manifest",
)
_PROVISION_CAPABILITY_COMMAND = OwnerCommandDefinition(
    owner="integration.installations",
    concern="integration capability grants and bindings",
    name="provision_installation_capability",
)


CommandResultT = TypeVar("CommandResultT")


def _adoption_error(
    suffix: str,
    message: str,
    **details: object,
) -> ManifestAdoptionError:
    return ManifestAdoptionError(
        code=f"integration.installations.{suffix}",
        message=message,
        details=details,
    )


def _capability_provisioning_error(
    suffix: str,
    message: str,
    **details: object,
) -> CapabilityProvisioningError:
    return CapabilityProvisioningError(
        code=f"integration.installations.{suffix}",
        message=message,
        details=details,
    )


def execute_command(
    db: Session,
    command: Callable[[], CommandResultT],
) -> CommandResultT:
    """Complete one installation-owned unit of work."""

    try:
        result = command()
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def config_revision_digest(
    *,
    config: dict[str, Any],
    secret_refs: dict[str, str],
    schema_version: str,
) -> str:
    payload = json.dumps(
        {
            "config": config,
            "schema_version": schema_version,
            "secret_refs": secret_refs,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def get_installation(
    db: Session, installation_id: UUID | str
) -> IntegrationInstallation:
    installation = db.get(IntegrationInstallation, coerce_uuid(str(installation_id)))
    if installation is None:
        raise InstallationError("integration installation not found")
    return installation


def commit_installation_changes(
    db: Session, installation: IntegrationInstallation
) -> IntegrationInstallation:
    """Commit one application-service unit of work owned by installations."""

    db.commit()
    db.refresh(installation)
    return installation


def list_installations(
    db: Session,
    *,
    connector_key: str | None = None,
    state: IntegrationInstallationState | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[IntegrationInstallation]:
    query = db.query(IntegrationInstallation)
    if connector_key:
        query = query.filter(
            IntegrationInstallation.connector_key == connector_key.strip().lower()
        )
    if state is not None:
        query = query.filter(IntegrationInstallation.state == state.value)
    return (
        query.order_by(
            IntegrationInstallation.connector_key.asc(),
            IntegrationInstallation.name.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )


def _manifest_pin(definition: ConnectorManifest) -> ManifestPin:
    return ManifestPin(
        connector_version=definition.version,
        manifest_digest=definition.digest,
    )


def manifest_pin_check(
    installation: IntegrationInstallation,
) -> ManifestPinCheck:
    """Classify whether one persisted pin is executable by this deployment."""

    installed_pin = ManifestPin(
        connector_version=installation.connector_version,
        manifest_digest=installation.manifest_digest,
    )
    deployed = connector_definition(installation.connector_key)
    deployed_pin = _manifest_pin(deployed) if deployed is not None else None
    supported = pinned_connector_definition(
        installation.connector_key,
        version=installation.connector_version,
        manifest_digest=installation.manifest_digest,
    )
    if supported is None:
        state = ManifestPinState.unavailable
    elif deployed_pin == installed_pin:
        state = ManifestPinState.current
    else:
        state = ManifestPinState.supported_historical
    return ManifestPinCheck(
        installation_id=installation.id,
        connector_key=installation.connector_key,
        installation_state=installation.state,
        installed_pin=installed_pin,
        deployed_pin=deployed_pin,
        state=state,
    )


def list_manifest_pin_checks(
    db: Session,
    *,
    enabled_only: bool = False,
) -> tuple[ManifestPinCheck, ...]:
    """Return deterministic, read-only deployment pin evidence."""

    query = select(IntegrationInstallation)
    if enabled_only:
        query = query.where(
            IntegrationInstallation.state == IntegrationInstallationState.enabled.value
        )
    else:
        query = query.where(
            IntegrationInstallation.state != IntegrationInstallationState.retired.value
        )
    installations = db.scalars(
        query.order_by(
            IntegrationInstallation.connector_key.asc(),
            IntegrationInstallation.name.asc(),
            IntegrationInstallation.id.asc(),
        )
    ).all()
    return tuple(manifest_pin_check(installation) for installation in installations)


def preview_manifest_adoption(
    db: Session,
    *,
    installation_id: UUID | str,
) -> ManifestAdoptionPreview:
    """Preview the exact current definition and target compatibility."""

    installation = get_installation(db, installation_id)
    check = manifest_pin_check(installation)
    target = connector_definition(installation.connector_key)
    errors: list[str] = []
    if installation.state == IntegrationInstallationState.retired.value:
        errors.append("installation_retired")
    if target is None:
        errors.append("target_definition_missing")
    else:
        errors.extend(
            _static_validation_errors_for_definition(
                installation,
                target,
                require_pin_match=False,
            )
        )
    target_pin = _manifest_pin(target) if target is not None else None
    return ManifestAdoptionPreview(
        installation_id=installation.id,
        connector_key=installation.connector_key,
        environment=installation.environment,
        installation_state=installation.state,
        installed_pin=check.installed_pin,
        target_pin=target_pin,
        pin_state=check.state,
        adoption_required=target_pin is not None and target_pin != check.installed_pin,
        ready=not errors,
        blocking_errors=tuple(errors),
    )


def require_enabled_capability_binding(
    db: Session,
    *,
    capability_id: str,
    connector_key: str | None = None,
) -> IntegrationCapabilityBinding:
    """Resolve one enabled binding, requiring an explicit default if ambiguous."""

    query = (
        db.query(IntegrationCapabilityBinding)
        .join(IntegrationInstallation)
        .filter(
            IntegrationCapabilityBinding.capability_id == capability_id,
            IntegrationCapabilityBinding.state == IntegrationBindingState.enabled.value,
            IntegrationInstallation.state == IntegrationInstallationState.enabled.value,
        )
    )
    if connector_key:
        query = query.filter(
            IntegrationInstallation.connector_key == connector_key.strip().lower()
        )
    bindings = query.order_by(IntegrationCapabilityBinding.created_at.asc()).all()
    if not bindings:
        raise InstallationError(f"no enabled binding for {capability_id}")
    if len(bindings) == 1:
        return bindings[0]
    defaults = [
        binding
        for binding in bindings
        if (binding.policy_json or {}).get("default") is True
    ]
    if len(defaults) != 1:
        raise InstallationError(
            f"multiple enabled bindings for {capability_id}; exactly one must be default"
        )
    return defaults[0]


def create_draft(
    db: Session,
    *,
    connector_key: str,
    name: str,
    environment: str = "production",
    actor: str | None = None,
) -> IntegrationInstallation:
    definition = require_connector_definition(connector_key)
    if definition.runtime.type == ConnectorRuntimeType.catalogue_only:
        raise InstallationError(
            f"connector {definition.key} has no approved executable runtime"
        )
    normalized_name = name.strip()
    if not normalized_name:
        raise InstallationError("installation name is required")
    if environment not in {"production", "sandbox", "test"}:
        raise InstallationError("invalid installation environment")

    installation = IntegrationInstallation(
        connector_key=definition.key,
        connector_version=definition.version,
        manifest_digest=definition.digest,
        name=normalized_name,
        environment=environment,
        state=IntegrationInstallationState.draft.value,
        created_by=actor,
        updated_by=actor,
    )
    db.add(installation)
    db.flush()
    return installation


def create_config_revision(
    db: Session,
    *,
    installation_id: UUID | str,
    config: dict[str, Any] | None = None,
    secret_refs: dict[str, str] | None = None,
    schema_version: str = "v1",
    actor: str | None = None,
) -> IntegrationConfigRevision:
    installation = get_installation(db, installation_id)
    if installation.state == IntegrationInstallationState.retired.value:
        raise InstallationError("retired installation cannot be configured")
    definition = _definition_for_installation(installation)
    normalized_config = dict(config or {})
    normalized_secret_refs = {
        str(name): str(reference) for name, reference in dict(secret_refs or {}).items()
    }
    _validate_secret_refs(definition, normalized_secret_refs)
    shape_errors = validate_config_shape(
        normalized_config,
        definition.config_schema,
    )
    if shape_errors:
        raise InstallationError("; ".join(shape_errors))
    normalized_schema_version = schema_version.strip()
    if not normalized_schema_version:
        raise InstallationError("configuration schema version is required")

    digest = config_revision_digest(
        config=normalized_config,
        secret_refs=normalized_secret_refs,
        schema_version=normalized_schema_version,
    )
    existing = (
        db.query(IntegrationConfigRevision)
        .filter(
            IntegrationConfigRevision.installation_id == installation.id,
            IntegrationConfigRevision.config_digest == digest,
        )
        .one_or_none()
    )
    if existing is not None:
        installation.current_config_revision_id = existing.id
        installation.current_config_revision = existing
        installation.updated_by = actor
        db.flush()
        return existing

    next_revision = (
        int(
            db.query(func.max(IntegrationConfigRevision.revision))
            .filter(IntegrationConfigRevision.installation_id == installation.id)
            .scalar()
            or 0
        )
        + 1
    )
    revision = IntegrationConfigRevision(
        installation_id=installation.id,
        revision=next_revision,
        schema_version=normalized_schema_version,
        config_json=normalized_config,
        secret_refs=normalized_secret_refs,
        config_digest=digest,
        validation_status=IntegrationValidationStatus.pending.value,
        created_by=actor,
    )
    db.add(revision)
    db.flush()
    installation.current_config_revision_id = revision.id
    installation.current_config_revision = revision
    installation.state = IntegrationInstallationState.draft.value
    installation.state_reason = "configuration_changed"
    installation.validated_at = None
    installation.updated_by = actor
    for binding in installation.capability_bindings:
        binding.state = IntegrationBindingState.disabled.value
        binding.enabled_at = None
        binding.disabled_at = datetime.now(UTC)
        binding.updated_by = actor
    db.flush()
    return revision


def bind_capability(
    db: Session,
    *,
    installation_id: UUID | str,
    capability_id: str,
    scope: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    actor: str | None = None,
) -> IntegrationCapabilityBinding:
    installation = get_installation(db, installation_id)
    if installation.state == IntegrationInstallationState.retired.value:
        raise InstallationError("retired installation cannot receive capabilities")
    definition = _definition_for_installation(installation)
    if definition.capability(capability_id) is None:
        raise InstallationError(
            f"connector {definition.key} does not declare {capability_id}"
        )
    binding = (
        db.query(IntegrationCapabilityBinding)
        .filter(
            IntegrationCapabilityBinding.installation_id == installation.id,
            IntegrationCapabilityBinding.capability_id == capability_id,
        )
        .one_or_none()
    )
    if binding is None:
        binding = IntegrationCapabilityBinding(
            installation=installation,
            capability_id=capability_id,
            state=IntegrationBindingState.disabled.value,
            created_by=actor,
        )
        db.add(binding)
    else:
        binding.state = IntegrationBindingState.disabled.value
        binding.enabled_at = None
        binding.disabled_at = datetime.now(UTC)
    binding.scope_json = dict(scope or {})
    binding.policy_json = dict(policy or {})
    binding.updated_by = actor
    installation.state = IntegrationInstallationState.draft.value
    installation.state_reason = "capability_binding_changed"
    installation.validated_at = None
    installation.updated_by = actor
    db.flush()
    return binding


def update_binding_policy(
    db: Session,
    *,
    capability_binding_id: UUID | str,
    policy: dict[str, Any],
    actor: str | None = None,
) -> IntegrationCapabilityBinding:
    """Replace an enabled binding's operator-owned dispatch policy."""

    binding = db.get(
        IntegrationCapabilityBinding,
        coerce_uuid(str(capability_binding_id)),
    )
    if binding is None:
        raise InstallationError("integration capability binding not found")
    if (
        binding.installation.state != IntegrationInstallationState.enabled.value
        or binding.state != IntegrationBindingState.enabled.value
    ):
        raise InstallationError("capability binding must be enabled")
    binding.policy_json = dict(policy)
    binding.updated_by = actor
    db.flush()
    return binding


def disable_capability_binding(
    db: Session,
    *,
    capability_binding_id: UUID | str,
    actor: str | None = None,
) -> IntegrationCapabilityBinding:
    """Disable one capability without stopping sibling lifecycle capabilities."""

    binding = db.get(
        IntegrationCapabilityBinding,
        coerce_uuid(str(capability_binding_id)),
    )
    if binding is None:
        raise InstallationError("integration capability binding not found")
    if binding.installation.state != IntegrationInstallationState.enabled.value:
        raise InstallationError("integration installation must be enabled")
    binding.state = IntegrationBindingState.disabled.value
    binding.enabled_at = None
    binding.disabled_at = datetime.now(UTC)
    binding.updated_by = actor
    db.flush()
    return binding


def validate_static(
    db: Session,
    *,
    installation_id: UUID | str,
    actor: str | None = None,
) -> ValidationResult:
    installation = get_installation(db, installation_id)
    if installation.state == IntegrationInstallationState.retired.value:
        raise InstallationError("retired installation cannot be validated")
    installation.state = IntegrationInstallationState.validating.value
    installation.updated_by = actor
    db.flush()

    errors = _static_validation_errors(installation)
    revision = installation.current_config_revision
    if errors:
        installation.state = IntegrationInstallationState.draft.value
        installation.state_reason = ",".join(errors)
        installation.validated_at = None
        if revision is not None:
            revision.validation_status = IntegrationValidationStatus.invalid.value
            revision.validation_errors = list(errors)
    else:
        installation.state = IntegrationInstallationState.disabled.value
        installation.state_reason = "connection_validation_required"
        installation.validated_at = datetime.now(UTC)
        installation.disabled_at = installation.disabled_at or datetime.now(UTC)
        if revision is not None:
            revision.validation_status = IntegrationValidationStatus.valid.value
            revision.validation_errors = None
    db.flush()
    return ValidationResult(valid=not errors, error_codes=tuple(errors))


def enable_after_connection_validation(
    db: Session,
    *,
    installation_id: UUID | str,
    connection_result: ValidationResult,
    actor: str | None = None,
) -> IntegrationInstallation:
    installation = get_installation(db, installation_id)
    static_errors = _static_validation_errors(installation)
    if static_errors:
        raise InstallationError(
            "installation static validation failed: " + ",".join(static_errors)
        )
    if not connection_result.valid:
        raise InstallationError(
            "connector connection validation failed: "
            + ",".join(connection_result.error_codes)
        )
    installation.state = IntegrationInstallationState.enabled.value
    installation.state_reason = None
    installation.validated_at = datetime.now(UTC)
    installation.enabled_at = datetime.now(UTC)
    installation.disabled_at = None
    installation.updated_by = actor
    for binding in installation.capability_bindings:
        binding.state = IntegrationBindingState.enabled.value
        binding.enabled_at = datetime.now(UTC)
        binding.disabled_at = None
        binding.updated_by = actor
    db.flush()
    return installation


def disable_installation(
    db: Session,
    *,
    installation_id: UUID | str,
    reason: str,
    actor: str | None = None,
) -> IntegrationInstallation:
    installation = get_installation(db, installation_id)
    if installation.state == IntegrationInstallationState.retired.value:
        raise InstallationError("retired installation is already terminal")
    installation.state = IntegrationInstallationState.disabled.value
    installation.state_reason = reason.strip() or "operator_disabled"
    installation.enabled_at = None
    installation.disabled_at = datetime.now(UTC)
    installation.updated_by = actor
    for binding in installation.capability_bindings:
        binding.state = IntegrationBindingState.disabled.value
        binding.enabled_at = None
        binding.disabled_at = datetime.now(UTC)
        binding.updated_by = actor
    db.flush()
    return installation


def quarantine_installation(
    db: Session,
    *,
    installation_id: UUID | str,
    reason: str,
    actor: str | None = None,
) -> IntegrationInstallation:
    installation = disable_installation(
        db,
        installation_id=installation_id,
        reason=reason,
        actor=actor,
    )
    installation.state = IntegrationInstallationState.quarantined.value
    installation.quarantined_at = datetime.now(UTC)
    db.flush()
    return installation


def retire_installation(
    db: Session,
    *,
    installation_id: UUID | str,
    reason: str,
    actor: str | None = None,
) -> IntegrationInstallation:
    installation = get_installation(db, installation_id)
    if installation.state != IntegrationInstallationState.retired.value:
        if installation.state != IntegrationInstallationState.quarantined.value:
            disable_installation(
                db,
                installation_id=installation.id,
                reason=reason,
                actor=actor,
            )
        installation.state = IntegrationInstallationState.retired.value
        installation.state_reason = reason.strip() or "operator_retired"
        installation.retired_at = datetime.now(UTC)
        installation.updated_by = actor
        db.flush()
    return installation


def provision_installation_capability(
    db: Session,
    command: ProvisionCapabilityCommand,
    *,
    context: CommandContext,
) -> CapabilityProvisioningResult:
    """Add and connection-validate one capability without an exposed draft state."""

    return execute_owner_command(
        db,
        definition=_PROVISION_CAPABILITY_COMMAND,
        context=context,
        operation=lambda: _provision_installation_capability(
            db,
            command=command,
            context=context,
        ),
    )


def _provision_installation_capability(
    db: Session,
    *,
    command: ProvisionCapabilityCommand,
    context: CommandContext,
) -> CapabilityProvisioningResult:
    if context.scope != CAPABILITY_PROVISIONING_SCOPE:
        raise _capability_provisioning_error(
            "capability_provisioning_scope_invalid",
            "Capability provisioning requires the dedicated command scope.",
            scope=context.scope,
        )
    capability_id = command.capability_id.strip()
    if not capability_id:
        raise _capability_provisioning_error(
            "invalid_capability",
            "Capability provisioning requires a capability identifier.",
        )
    if len(context.actor) > 160:
        raise _capability_provisioning_error(
            "invalid_command_context",
            "Capability provisioning actor exceeds the installation audit limit.",
            field="actor",
        )

    expected_pin = _normalized_adoption_pin(
        command.expected_installed_pin,
        field="expected_installed_pin",
    )
    installation = db.scalar(
        select(IntegrationInstallation)
        .where(IntegrationInstallation.id == command.installation_id)
        .with_for_update()
    )
    if installation is None:
        raise _capability_provisioning_error(
            "not_found",
            "Integration installation was not found.",
            installation_id=str(command.installation_id),
        )
    actual_pin = ManifestPin(
        connector_version=installation.connector_version,
        manifest_digest=installation.manifest_digest,
    )
    if actual_pin != expected_pin:
        raise _capability_provisioning_error(
            "stale_manifest_pin",
            "Installation manifest pin changed after capability review.",
            installation_id=str(installation.id),
            actual_connector_version=actual_pin.connector_version,
            actual_manifest_digest=actual_pin.manifest_digest,
        )
    if installation.state != IntegrationInstallationState.enabled.value:
        raise _capability_provisioning_error(
            "invalid_transition",
            "Capability provisioning requires an enabled installation.",
            installation_id=str(installation.id),
            installation_state=installation.state,
        )
    definition = _definition_for_installation(installation)
    if definition.capability(capability_id) is None:
        raise _capability_provisioning_error(
            "invalid_capability",
            "The pinned connector manifest does not declare this capability.",
            connector_key=installation.connector_key,
            capability_id=capability_id,
        )

    binding = db.scalar(
        select(IntegrationCapabilityBinding)
        .where(
            IntegrationCapabilityBinding.installation_id == installation.id,
            IntegrationCapabilityBinding.capability_id == capability_id,
        )
        .with_for_update()
    )
    actual_binding_id = binding.id if binding is not None else None
    actual_binding_state = (
        IntegrationBindingState(binding.state) if binding is not None else None
    )
    if (
        actual_binding_id != command.expected_binding_id
        or actual_binding_state != command.expected_binding_state
    ):
        raise _capability_provisioning_error(
            "stale_capability_binding",
            "Capability binding changed after provisioning review.",
            installation_id=str(installation.id),
            capability_id=capability_id,
            actual_binding_id=(
                str(actual_binding_id) if actual_binding_id is not None else None
            ),
            actual_binding_state=(
                actual_binding_state.value if actual_binding_state is not None else None
            ),
        )

    expected_scope = dict(command.capability_scope)
    expected_policy = dict(command.policy)
    if (
        binding is not None
        and binding.state == IntegrationBindingState.enabled.value
        and dict(binding.scope_json or {}) == expected_scope
        and dict(binding.policy_json or {}) == expected_policy
    ):
        return CapabilityProvisioningResult(
            installation_id=installation.id,
            connector_key=installation.connector_key,
            capability_binding_id=binding.id,
            capability_id=binding.capability_id,
            installation_state=installation.state,
            binding_state=binding.state,
            replayed=True,
        )

    binding = bind_capability(
        db,
        installation_id=installation.id,
        capability_id=capability_id,
        scope=expected_scope,
        policy=expected_policy,
        actor=context.actor,
    )
    static_result = validate_static(
        db,
        installation_id=installation.id,
        actor=context.actor,
    )
    if not static_result.valid:
        raise _capability_provisioning_error(
            "invalid_configuration",
            "Installation static validation failed after capability provisioning.",
            installation_id=str(installation.id),
            error_codes=static_result.error_codes,
        )

    from app.services.integrations.runtime_execution import (
        build_execution_context,
        validate_connection,
    )

    runtime_context = build_execution_context(
        db,
        capability_binding_id=binding.id,
        allow_disabled=True,
    )
    connection_result = validate_connection(runtime_context)
    if not connection_result.valid:
        raise _capability_provisioning_error(
            "connection_validation_failed",
            "Connector connection validation failed; capability was not enabled.",
            installation_id=str(installation.id),
            capability_id=capability_id,
            error_codes=connection_result.error_codes,
        )
    enable_after_connection_validation(
        db,
        installation_id=installation.id,
        connection_result=connection_result,
        actor=context.actor,
    )
    db.flush()
    emit_event(
        db,
        EventType.integration_installation_capability_provisioned,
        {
            "schema_version": 1,
            "installation_id": str(installation.id),
            "connector_key": installation.connector_key,
            "connector_version": installation.connector_version,
            "manifest_digest": installation.manifest_digest,
            "capability_binding_id": str(binding.id),
            "capability_id": capability_id,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
            "idempotency_key": context.idempotency_key,
            "reason": context.reason,
        },
        actor=context.actor,
    )
    return CapabilityProvisioningResult(
        installation_id=installation.id,
        connector_key=installation.connector_key,
        capability_binding_id=binding.id,
        capability_id=binding.capability_id,
        installation_state=installation.state,
        binding_state=binding.state,
        replayed=False,
    )


def _normalized_adoption_pin(pin: ManifestPin, *, field: str) -> ManifestPin:
    version = pin.connector_version.strip()
    digest = pin.manifest_digest.strip().lower()
    if not version:
        raise _adoption_error(
            "invalid_manifest",
            "Connector version is required for manifest adoption.",
            field=field,
        )
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise _adoption_error(
            "invalid_manifest",
            "Manifest adoption requires a SHA-256 digest.",
            field=field,
        )
    return ManifestPin(connector_version=version, manifest_digest=digest)


def adopt_installation_manifest(
    db: Session,
    command: AdoptManifestCommand,
    *,
    context: CommandContext,
) -> ManifestAdoptionResult:
    """Atomically adopt an exact deployed manifest after compatibility review."""

    return execute_owner_command(
        db,
        definition=_ADOPT_MANIFEST_COMMAND,
        context=context,
        operation=lambda: _adopt_installation_manifest(
            db,
            command=command,
            context=context,
        ),
    )


def _adopt_installation_manifest(
    db: Session,
    *,
    command: AdoptManifestCommand,
    context: CommandContext,
) -> ManifestAdoptionResult:
    if context.scope != MANIFEST_ADOPTION_SCOPE:
        raise _adoption_error(
            "manifest_adoption_scope_invalid",
            "Manifest adoption requires the dedicated command scope.",
            scope=context.scope,
        )
    if len(context.actor) > 160:
        raise _adoption_error(
            "invalid_command_context",
            "Manifest adoption actor exceeds the installation audit limit.",
            field="actor",
        )

    expected_pin = _normalized_adoption_pin(
        command.expected_installed_pin,
        field="expected_installed_pin",
    )
    target_pin = _normalized_adoption_pin(
        command.target_pin,
        field="target_pin",
    )
    installation = db.scalar(
        select(IntegrationInstallation)
        .where(IntegrationInstallation.id == command.installation_id)
        .with_for_update()
    )
    if installation is None:
        raise _adoption_error(
            "not_found",
            "Integration installation was not found.",
            installation_id=str(command.installation_id),
        )
    deployed = pinned_connector_definition(
        installation.connector_key,
        version=target_pin.connector_version,
        manifest_digest=target_pin.manifest_digest,
    )
    if deployed is None:
        raise _adoption_error(
            "target_manifest_not_deployed",
            "Reviewed target manifest is not available in this deployment.",
            connector_key=installation.connector_key,
            target_connector_version=target_pin.connector_version,
            target_manifest_digest=target_pin.manifest_digest,
        )

    actual_pin = ManifestPin(
        connector_version=installation.connector_version,
        manifest_digest=installation.manifest_digest,
    )
    if actual_pin == target_pin:
        return ManifestAdoptionResult(
            installation_id=installation.id,
            connector_key=installation.connector_key,
            previous_pin=actual_pin,
            adopted_pin=target_pin,
            installation_state=installation.state,
            replayed=True,
        )
    if actual_pin != expected_pin:
        raise _adoption_error(
            "stale_manifest_pin",
            "Installation manifest pin changed after the adoption review.",
            installation_id=str(installation.id),
            actual_connector_version=actual_pin.connector_version,
            actual_manifest_digest=actual_pin.manifest_digest,
        )
    if installation.state == IntegrationInstallationState.retired.value:
        raise _adoption_error(
            "invalid_transition",
            "Retired installations cannot adopt another manifest.",
            installation_id=str(installation.id),
        )

    compatibility_errors = _static_validation_errors_for_definition(
        installation,
        deployed,
        require_pin_match=False,
    )
    if compatibility_errors:
        raise _adoption_error(
            "manifest_adoption_incompatible",
            "Installation configuration is incompatible with the target manifest.",
            installation_id=str(installation.id),
            error_codes=tuple(compatibility_errors),
        )

    installation.connector_version = target_pin.connector_version
    installation.manifest_digest = target_pin.manifest_digest
    installation.updated_by = context.actor
    db.flush()
    emit_event(
        db,
        EventType.integration_installation_manifest_adopted,
        {
            "schema_version": 1,
            "installation_id": str(installation.id),
            "connector_key": installation.connector_key,
            "previous_connector_version": actual_pin.connector_version,
            "previous_manifest_digest": actual_pin.manifest_digest,
            "adopted_connector_version": target_pin.connector_version,
            "adopted_manifest_digest": target_pin.manifest_digest,
            "configuration_revision_id": (
                str(installation.current_config_revision_id)
                if installation.current_config_revision_id
                else None
            ),
            "capability_binding_ids": [
                str(binding.id)
                for binding in sorted(
                    installation.capability_bindings,
                    key=lambda item: (item.capability_id, str(item.id)),
                )
            ],
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
            "idempotency_key": context.idempotency_key,
            "reason": context.reason,
        },
        actor=context.actor,
    )
    return ManifestAdoptionResult(
        installation_id=installation.id,
        connector_key=installation.connector_key,
        previous_pin=actual_pin,
        adopted_pin=target_pin,
        installation_state=installation.state,
        replayed=False,
    )


def validate_config_shape(
    config: dict[str, Any], schema: dict[str, Any]
) -> tuple[str, ...]:
    """Validate the safe subset used by manifest-driven admin forms."""

    if not schema:
        return ()
    errors: list[str] = []
    if schema.get("type", "object") != "object":
        return ("config_schema_type_must_be_object",)
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        return ("config_schema_properties_invalid",)
    required = schema.get("required") or []
    if not isinstance(required, list):
        return ("config_schema_required_invalid",)
    for key in required:
        if key not in config:
            errors.append(f"config_required:{key}")
    if schema.get("additionalProperties") is False:
        for key in sorted(set(config) - set(properties)):
            errors.append(f"config_unknown:{key}")
    type_map: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for key, value in config.items():
        spec = properties.get(key)
        if not isinstance(spec, dict) or value is None:
            continue
        expected_name = spec.get("type")
        expected = type_map.get(str(expected_name))
        is_bool_number = isinstance(value, bool) and expected_name in {
            "integer",
            "number",
        }
        if expected is not None and (not isinstance(value, expected) or is_bool_number):
            errors.append(f"config_type:{key}:{expected_name}")
            continue
        allowed = spec.get("enum")
        if isinstance(allowed, list) and value not in allowed:
            errors.append(f"config_enum:{key}")
    return tuple(errors)


def _definition_for_installation(
    installation: IntegrationInstallation,
) -> ConnectorManifest:
    try:
        return require_pinned_connector_definition(
            installation.connector_key,
            version=installation.connector_version,
            manifest_digest=installation.manifest_digest,
        )
    except KeyError as exc:
        raise InstallationError(
            "installed connector manifest pin is not deployed"
        ) from exc


def _validate_secret_refs(
    definition: ConnectorManifest,
    secret_refs: dict[str, str],
) -> None:
    declared = {binding.name for binding in definition.secrets}
    unknown = sorted(set(secret_refs) - declared)
    if unknown:
        raise InstallationError("undeclared secret binding(s): " + ",".join(unknown))
    invalid = sorted(
        name for name, reference in secret_refs.items() if not is_secret_ref(reference)
    )
    if invalid:
        raise InstallationError(
            "secret bindings must store references only: " + ",".join(invalid)
        )


def _static_validation_errors(
    installation: IntegrationInstallation,
) -> list[str]:
    try:
        definition = _definition_for_installation(installation)
    except (InstallationError, KeyError):
        return ["definition_mismatch"]
    return _static_validation_errors_for_definition(
        installation,
        definition,
        require_pin_match=True,
    )


def _static_validation_errors_for_definition(
    installation: IntegrationInstallation,
    definition: ConnectorManifest,
    *,
    require_pin_match: bool,
) -> list[str]:
    errors: list[str] = []
    if require_pin_match and (
        installation.connector_version != definition.version
        or installation.manifest_digest != definition.digest
    ):
        errors.append("definition_mismatch")
    revision = installation.current_config_revision
    if revision is None:
        return [*errors, "config_revision_missing"]
    errors.extend(validate_config_shape(revision.config_json, definition.config_schema))
    secret_refs = {
        str(name): str(reference)
        for name, reference in dict(revision.secret_refs or {}).items()
    }
    try:
        _validate_secret_refs(definition, secret_refs)
    except InstallationError:
        errors.append("secret_reference_invalid")
    missing_secrets = sorted(definition.required_secret_names - set(secret_refs))
    errors.extend(f"secret_required:{name}" for name in missing_secrets)
    if definition.capabilities and not installation.capability_bindings:
        errors.append("capability_binding_missing")
    for binding in installation.capability_bindings:
        if definition.capability(binding.capability_id) is None:
            errors.append(f"capability_undeclared:{binding.capability_id}")
    if definition.egress.allow_installation_hosts:
        host, egress_error = validate_https_url(revision.config_json.get("url"))
        if egress_error:
            errors.append(f"egress:{egress_error}")
        else:
            approved_hosts = {
                str(item).strip().lower().rstrip(".")
                for binding in installation.capability_bindings
                for item in (binding.policy_json or {}).get("approved_egress_hosts", [])
                if str(item).strip()
            }
            if host not in approved_hosts:
                errors.append(f"egress_host_not_approved:{host}")
    return errors
