"""Machine-readable exclusivity, precedence, chain, and fanout registry."""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain


class RelationshipMode(str, enum.Enum):
    exclusive = "exclusive"
    precedence = "precedence"
    chain = "chain"
    fanout = "fanout"
    competing = "competing"
    incompatible = "incompatible"


class HandlerStage(enum.IntEnum):
    state = 10
    communication = 20
    external = 30


@dataclass(frozen=True)
class ControlRelationship:
    name: str
    mode: RelationshipMode
    members: tuple[str, ...]
    rule: str


@dataclass(frozen=True)
class HandlerControl:
    handler_name: str
    stage: HandlerStage
    order: int
    capabilities: tuple[str, ...]
    relationship: RelationshipMode = RelationshipMode.fanout


@dataclass(frozen=True)
class EventHandlerStep:
    handler: Any
    handler_name: str
    stage: HandlerStage | None
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class ControlFinding:
    code: str
    severity: str
    message: str
    members: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ControlRelationshipError(ValueError):
    pass


CONTROL_RELATIONSHIPS: tuple[ControlRelationship, ...] = (
    ControlRelationship(
        name="branding_resolution",
        mode=RelationshipMode.precedence,
        members=(
            "brand.organization_profile",
            "brand.reseller_profile",
            "brand.platform_profile",
            "legacy.domain_settings",
            "deployment.brand_config",
        ),
        rule="First non-empty field wins in declared tenant-to-platform order.",
    ),
    ControlRelationship(
        name="team_email_sender_resolution",
        mode=RelationshipMode.precedence,
        members=(
            "team.sender_profile",
            "category.sender_profile",
            "default.sender_profile",
            "deployment.smtp_identity",
        ),
        rule="Team-specific outbound identity wins without changing tenant branding.",
    ),
    ControlRelationship(
        name="whatsapp_provider_selection",
        mode=RelationshipMode.exclusive,
        members=("meta_cloud_api",),
        rule=(
            "An enabled whatsapp installation configured for meta_cloud_api is "
            "the only transport provider."
        ),
    ),
    ControlRelationship(
        name="notification_channels",
        mode=RelationshipMode.fanout,
        members=("web", "email", "push", "nextcloud", "whatsapp"),
        rule="Policy may select multiple channels; each channel selects one provider.",
    ),
    ControlRelationship(
        name="subscription_state_event_pipeline",
        mode=RelationshipMode.chain,
        members=(
            "internal_state_handlers",
            "customer_notifications",
            "external_integrations",
        ),
        rule="A failed state stage blocks later communication and external delivery.",
    ),
    ControlRelationship(
        name="workqueue_assignment",
        mode=RelationshipMode.competing,
        members=("manual_assignment", "routing_rule", "availability_assignment"),
        rule="Multiple candidates may compete, but one atomic owner claim wins.",
    ),
    ControlRelationship(
        name="scheduled_and_event_execution",
        mode=RelationshipMode.competing,
        members=("scheduler_trigger", "domain_event_trigger"),
        rule="Either trigger may request work; one idempotency/claim key executes it.",
    ),
    ControlRelationship(
        name="crm_native_writers",
        mode=RelationshipMode.incompatible,
        members=("crm_writer", "sub_native_writer"),
        rule="A vertical has one writer; CRM ingestion never writes back from Sub.",
    ),
    ControlRelationship(
        name="provisioning_writers",
        mode=RelationshipMode.incompatible,
        members=("legacy_direct_provisioner", "native_workflow_provisioner"),
        rule="A service transition is executed by one provisioning owner.",
    ),
)


HANDLER_CONTROLS: dict[str, HandlerControl] = {
    "LifecycleHandler": HandlerControl(
        "LifecycleHandler", HandlerStage.state, 10, ("subscription_lifecycle",)
    ),
    "ProvisioningHandler": HandlerControl(
        "ProvisioningHandler", HandlerStage.state, 40, ("service_provisioning",)
    ),
    "SalesLifecycleProjectionHandler": HandlerControl(
        "SalesLifecycleProjectionHandler",
        HandlerStage.state,
        45,
        ("sales_to_service_lifecycle_projection",),
    ),
    "EnforcementHandler": HandlerControl(
        "EnforcementHandler", HandlerStage.state, 50, ("service_enforcement",)
    ),
    "CredentialSessionProjectionHandler": HandlerControl(
        "CredentialSessionProjectionHandler",
        HandlerStage.state,
        60,
        ("credential_session_projection_invalidation",),
    ),
    "IPAssignmentProjectionHandler": HandlerControl(
        "IPAssignmentProjectionHandler",
        HandlerStage.state,
        55,
        ("exact_service_ipv4_projection",),
    ),
    "ArrangementHandler": HandlerControl(
        "ArrangementHandler", HandlerStage.state, 20, ("payment_arrangements",)
    ),
    "SubscriptionChangeExecutionHandler": HandlerControl(
        "SubscriptionChangeExecutionHandler",
        HandlerStage.state,
        25,
        ("subscription_change_payment_release",),
    ),
    "ReferralHandler": HandlerControl(
        "ReferralHandler", HandlerStage.state, 30, ("referral_qualification",)
    ),
    "PrepaidRenewalHandler": HandlerControl(
        "PrepaidRenewalHandler",
        HandlerStage.state,
        47,
        ("prepaid_service_renewal_after_funding",),
    ),
    "NotificationHandler": HandlerControl(
        "NotificationHandler",
        HandlerStage.communication,
        10,
        ("customer_notifications",),
    ),
    "StaffInviteHandler": HandlerControl(
        "StaffInviteHandler",
        HandlerStage.communication,
        20,
        ("staff_invitation_intents",),
    ),
    "ResellerInviteHandler": HandlerControl(
        "ResellerInviteHandler",
        HandlerStage.communication,
        30,
        ("reseller_invitation_intents",),
    ),
    "PasswordRecoveryHandler": HandlerControl(
        "PasswordRecoveryHandler",
        HandlerStage.communication,
        40,
        ("password_recovery_intents",),
    ),
    "WebhookHandler": HandlerControl(
        "WebhookHandler", HandlerStage.external, 10, ("external_webhooks",)
    ),
}

RELATIONSHIP_SETTING_KEYS: set[tuple[SettingDomain, str]] = set()

CHAINED_EVENT_TYPES = {
    "account_credit.deposited",
    "subscription.activated",
    "subscription.suspended",
    "subscription.resumed",
    "subscription.canceled",
    "subscription.upgraded",
    "subscription.downgraded",
    "subscription.expired",
    "usage.exhausted",
    "service_order.assigned",
    "provisioning.completed",
    "provisioning.failed",
}

# Dependencies within the same stage. Stage-to-stage dependencies are derived
# automatically for chained events.
EVENT_HANDLER_DEPENDENCIES: dict[str, dict[str, tuple[str, ...]]] = {
    "account_credit.deposited": {
        "EnforcementHandler": ("PrepaidRenewalHandler",),
    },
    "payment.received": {
        "EnforcementHandler": ("PrepaidRenewalHandler",),
    },
    "subscription.activated": {
        "EnforcementHandler": ("ProvisioningHandler",),
    },
    "subscription.resumed": {
        "EnforcementHandler": ("ProvisioningHandler",),
    },
}


def event_relationship_mode(event_type: str) -> RelationshipMode:
    if event_type in CHAINED_EVENT_TYPES:
        return RelationshipMode.chain
    return RelationshipMode.fanout


def handler_event_types(handler_name: str) -> frozenset[str] | None:
    """Return the handler's executable event scope; ``None`` means wildcard."""
    if handler_name == "LifecycleHandler":
        from app.services.events.types import SUBSCRIPTION_LIFECYCLE_MAP

        return frozenset(item.value for item in SUBSCRIPTION_LIFECYCLE_MAP)
    if handler_name == "CredentialSessionProjectionHandler":
        from app.services.events.handlers.credential_session_projection import (
            HANDLED_EVENT_TYPES,
        )

        return frozenset(item.value for item in HANDLED_EVENT_TYPES)
    if handler_name == "IPAssignmentProjectionHandler":
        from app.services.events.handlers.ip_assignment_projection import (
            HANDLED_EVENT_TYPES,
        )

        return frozenset(item.value for item in HANDLED_EVENT_TYPES)
    if handler_name == "NotificationHandler":
        from app.services.events.handlers.notification import EVENT_NOTIFICATION_SPECS

        return frozenset(item.value for item in EVENT_NOTIFICATION_SPECS)
    if handler_name == "PrepaidRenewalHandler":
        from app.services.events.handlers.prepaid_renewal import HANDLED_EVENT_TYPES

        return frozenset(item.value for item in HANDLED_EVENT_TYPES)
    if handler_name == "StaffInviteHandler":
        from app.services.events.handlers.staff_invite import HANDLED_EVENT_TYPES

        return frozenset(item.value for item in HANDLED_EVENT_TYPES)
    if handler_name == "ResellerInviteHandler":
        from app.services.events.handlers.reseller_invite import HANDLED_EVENT_TYPES

        return frozenset(item.value for item in HANDLED_EVENT_TYPES)
    if handler_name == "PasswordRecoveryHandler":
        from app.services.events.handlers.password_recovery import HANDLED_EVENT_TYPES

        return frozenset(item.value for item in HANDLED_EVENT_TYPES)
    if handler_name == "WebhookHandler":
        # Event subscriptions are data-driven; the delivery adapter is a
        # wildcard and filters by enabled typed subscriptions at runtime.
        return None
    if handler_name == "ArrangementHandler":
        from app.services.events.handlers.arrangements import HANDLED_EVENT_TYPES

        return frozenset(item.value for item in HANDLED_EVENT_TYPES)
    if handler_name == "SubscriptionChangeExecutionHandler":
        from app.services.events.handlers.subscription_change_execution import (
            HANDLED_EVENT_TYPES,
        )

        return frozenset(item.value for item in HANDLED_EVENT_TYPES)
    if handler_name == "EnforcementHandler":
        from app.services.events.handlers.enforcement import HANDLED_EVENT_TYPES

        return frozenset(item.value for item in HANDLED_EVENT_TYPES)
    if handler_name == "ProvisioningHandler":
        from app.services.events.handlers.provisioning import HANDLED_EVENT_TYPES

        return frozenset(item.value for item in HANDLED_EVENT_TYPES)
    if handler_name == "SalesLifecycleProjectionHandler":
        from app.services.events.handlers.sales_lifecycle_projection import (
            HANDLED_EVENT_TYPES,
        )

        return frozenset(item.value for item in HANDLED_EVENT_TYPES)
    if handler_name == "ReferralHandler":
        from app.services.events.handlers.referral import REFERRAL_QUALIFY_EVENTS

        return frozenset(item.value for item in REFERRAL_QUALIFY_EVENTS)
    raise ControlRelationshipError(
        f"Event handler {handler_name} has no executable event-scope declaration"
    )


def event_execution_plan(
    event_type: str, handlers: Iterable[Any]
) -> list[EventHandlerStep]:
    """Build the ordered, dependency-aware plan for one event."""
    resolved = list(handlers)
    if resolved and all(
        handler.__class__.__name__ in HANDLER_CONTROLS for handler in resolved
    ):
        resolved = sorted(
            resolved,
            key=lambda handler: (
                HANDLER_CONTROLS[handler.__class__.__name__].stage,
                HANDLER_CONTROLS[handler.__class__.__name__].order,
            ),
        )
    applicable: list[Any] = []
    for handler in resolved:
        name = handler.__class__.__name__
        if name not in HANDLER_CONTROLS:
            applicable.append(handler)
            continue
        event_types = handler_event_types(name)
        if event_types is None or event_type in event_types:
            applicable.append(handler)

    chained = event_relationship_mode(event_type) == RelationshipMode.chain
    all_declared = all(
        handler.__class__.__name__ in HANDLER_CONTROLS for handler in applicable
    )
    steps: list[EventHandlerStep] = []
    for index, handler in enumerate(applicable):
        name = handler.__class__.__name__
        control = HANDLER_CONTROLS.get(name)
        dependencies: list[str] = []
        if chained and all_declared and control is not None:
            dependencies.extend(
                prior.__class__.__name__
                for prior in applicable
                if HANDLER_CONTROLS[prior.__class__.__name__].stage < control.stage
            )
        elif chained:
            # Preserve deterministic behavior for extensions/tests that have not
            # entered the production control registry yet.
            dependencies.extend(
                prior.__class__.__name__ for prior in applicable[:index]
            )
        if all_declared and control is not None:
            # Explicit owner-to-owner ordering is narrower than a chained event:
            # it must not make independent communication or external delivery
            # depend on an unrelated state consequence.
            dependencies.extend(
                dependency
                for dependency in EVENT_HANDLER_DEPENDENCIES.get(event_type, {}).get(
                    name, ()
                )
                if any(
                    candidate.__class__.__name__ == dependency
                    for candidate in applicable
                )
            )
        steps.append(
            EventHandlerStep(
                handler=handler,
                handler_name=name,
                stage=control.stage if control else None,
                dependencies=tuple(dict.fromkeys(dependencies)),
            )
        )
    return steps


def validate_event_execution_policy(handlers: Iterable[Any]) -> None:
    """Fail startup when scopes or chain dependencies are incomplete/cyclic."""
    resolved = validate_and_order_handlers(handlers)
    from app.services.events.types import EventType

    valid_event_types = {item.value for item in EventType}
    unknown_chained_events = sorted(CHAINED_EVENT_TYPES - valid_event_types)
    if unknown_chained_events:
        raise ControlRelationshipError(
            "Unknown chained event types: " + ", ".join(unknown_chained_events)
        )
    for handler in resolved:
        name = handler.__class__.__name__
        event_types = handler_event_types(name)
        unknown = sorted((event_types or frozenset()) - valid_event_types)
        if unknown:
            raise ControlRelationshipError(
                f"Event handler {name} declares unknown event types: {', '.join(unknown)}"
            )

    dependency_event_types = set(EVENT_HANDLER_DEPENDENCIES)
    unknown_dependency_events = sorted(dependency_event_types - valid_event_types)
    if unknown_dependency_events:
        raise ControlRelationshipError(
            "Unknown dependency event types: " + ", ".join(unknown_dependency_events)
        )

    for event_type in CHAINED_EVENT_TYPES | dependency_event_types:
        dependencies_by_handler = EVENT_HANDLER_DEPENDENCIES.get(event_type, {})
        plan = event_execution_plan(event_type, resolved)
        if event_type in CHAINED_EVENT_TYPES and len(plan) < 2:
            raise ControlRelationshipError(
                f"Chained event {event_type} has fewer than two subscribed handlers"
            )
        plan_names = {step.handler_name for step in plan}
        for handler_name, dependencies in dependencies_by_handler.items():
            if handler_name not in plan_names:
                raise ControlRelationshipError(
                    f"Event {event_type} dependency target {handler_name} is not subscribed"
                )
            missing = sorted(set(dependencies) - plan_names)
            if missing:
                raise ControlRelationshipError(
                    f"Event {event_type} dependencies are not subscribed: "
                    f"{', '.join(missing)}"
                )

        graph = {step.handler_name: set(step.dependencies) for step in plan}
        _validate_acyclic_handler_graph(event_type, graph)


def _validate_acyclic_handler_graph(
    event_type: str, graph: dict[str, set[str]]
) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ControlRelationshipError(
                f"Event {event_type} handler dependency cycle includes {name}"
            )
        if name in visited:
            return
        visiting.add(name)
        for dependency in graph.get(name, set()):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in graph:
        visit(name)


def validate_and_order_handlers(handlers: Iterable[Any]) -> list[Any]:
    resolved = list(handlers)
    names = [handler.__class__.__name__ for handler in resolved]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ControlRelationshipError(
            f"Duplicate event handler registrations: {', '.join(duplicates)}"
        )
    missing = sorted(set(names) - set(HANDLER_CONTROLS))
    if missing:
        raise ControlRelationshipError(
            f"Event handlers missing control declarations: {', '.join(missing)}"
        )

    capability_owner: dict[str, str] = {}
    for name in names:
        for capability in HANDLER_CONTROLS[name].capabilities:
            previous = capability_owner.get(capability)
            if previous:
                raise ControlRelationshipError(
                    f"Exclusive event capability {capability} owned by "
                    f"{previous} and {name}"
                )
            capability_owner[capability] = name
    return sorted(
        resolved,
        key=lambda handler: (
            HANDLER_CONTROLS[handler.__class__.__name__].stage,
            HANDLER_CONTROLS[handler.__class__.__name__].order,
        ),
    )


def event_topology() -> list[dict[str, object]]:
    return [
        {
            "handler": control.handler_name,
            "stage": control.stage.name,
            "order": control.order,
            "relationship": control.relationship.value,
            "capabilities": list(control.capabilities),
            "event_types": (
                ["*"]
                if handler_event_types(control.handler_name) is None
                else sorted(handler_event_types(control.handler_name) or ())
            ),
        }
        for control in sorted(
            HANDLER_CONTROLS.values(), key=lambda item: (item.stage, item.order)
        )
    ]


def event_policies() -> dict[str, object]:
    handlers = [type(name, (), {})() for name in HANDLER_CONTROLS]
    return {
        "default": RelationshipMode.fanout.value,
        "overrides": {
            event_type: {
                "mode": RelationshipMode.chain.value,
                "steps": [
                    {
                        "handler": step.handler_name,
                        "stage": step.stage.name if step.stage else None,
                        "dependencies": list(step.dependencies),
                    }
                    for step in event_execution_plan(event_type, handlers)
                ],
            }
            for event_type in sorted(CHAINED_EVENT_TYPES)
        },
    }


def audit_event_relationships() -> list[ControlFinding]:
    handlers = [type(name, (), {})() for name in HANDLER_CONTROLS]
    try:
        validate_event_execution_policy(handlers)
    except ControlRelationshipError as exc:
        return [
            ControlFinding(
                code="invalid_event_execution_policy",
                severity="error",
                message=str(exc),
                members=tuple(HANDLER_CONTROLS),
            )
        ]
    return []


def audit_setting_relationships(
    db: Session,
    *,
    pending: tuple[SettingDomain, str, object] | None = None,
) -> list[ControlFinding]:
    return []


def audit_feature_control_relationships(
    db: Session,
    *,
    pending: dict[str, bool | None] | None = None,
) -> list[ControlFinding]:
    """Audit relationships owned by the canonical feature-control writer."""
    from app.services import control_registry

    requested = pending or {}
    controls = {control.key: control for control in control_registry.all_controls()}

    def enabled(key: str) -> bool:
        if key not in requested:
            return control_registry.resolve_control(db, key).own_enabled
        value = requested[key]
        if value is not None:
            return value
        return controls[key].on_missing

    findings: list[ControlFinding] = []
    read_enabled = enabled("quotes.native_read")
    write_enabled = enabled("quotes.native_write")
    if write_enabled and not read_enabled:
        findings.append(
            ControlFinding(
                code="quote_write_before_read_flip",
                severity="error",
                message="Native quote writes require the native read path.",
                members=("quotes.native_write", "quotes.native_read"),
            )
        )
    return findings


def validate_setting_change(
    db: Session, domain: SettingDomain, key: str, value: object
) -> None:
    if (domain, key) not in RELATIONSHIP_SETTING_KEYS:
        return
    errors = [
        finding
        for finding in audit_setting_relationships(db, pending=(domain, key, value))
        if finding.severity == "error"
    ]
    if errors:
        raise ControlRelationshipError("; ".join(item.message for item in errors))


def validate_feature_control_changes(
    db: Session, pending: dict[str, bool | None]
) -> None:
    errors = [
        finding
        for finding in audit_feature_control_relationships(db, pending=pending)
        if finding.severity == "error"
    ]
    if errors:
        raise ControlRelationshipError("; ".join(item.message for item in errors))


def relationship_manifest() -> list[dict[str, object]]:
    return [
        {
            "name": item.name,
            "mode": item.mode.value,
            "members": list(item.members),
            "rule": item.rule,
        }
        for item in CONTROL_RELATIONSHIPS
    ]
