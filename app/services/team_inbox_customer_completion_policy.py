"""Immutable Customer-only completion policy for Team Inbox conversations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.team_inbox import InboxCustomerCompletionPolicyVersion
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "communications.team_inbox_customer_completion_policy"
CONCERN = "immutable Customer resolution-completion policy versions"
DEFAULT_REQUIRED_FIELDS = ("name", "phone", "address")
_DEFAULT_POLICY_NAMESPACE = UUID("6e38dd4a-c681-4fc0-8cf2-499ef399eeb4")


class CustomerCompletionField(StrEnum):
    name = "name"
    phone = "phone"
    address = "address"
    email = "email"
    whatsapp = "whatsapp"
    organization = "organization"
    city_region = "city_region"
    country = "country"
    date_of_birth = "date_of_birth"
    gender = "gender"
    nin = "nin"


class CustomerCompletionPolicyError(DomainError):
    pass


@dataclass(frozen=True, slots=True)
class CreateCustomerCompletionPolicyCommand:
    context: CommandContext
    required_fields: tuple[CustomerCompletionField, ...]
    actor_person_id: UUID | None
    actor_type: AuditActorType
    decision_source: str


@dataclass(frozen=True, slots=True)
class CustomerCompletionPolicyOutcome:
    policy_id: UUID
    version: int
    required_fields: tuple[CustomerCompletionField, ...]


_CREATE = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="create_customer_completion_policy_version",
)


def _error(suffix: str, message: str, **details: object) -> DomainError:
    return CustomerCompletionPolicyError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def active_policy(db: Session) -> InboxCustomerCompletionPolicyVersion | None:
    """Return the latest immutable policy version."""

    return db.scalar(
        select(InboxCustomerCompletionPolicyVersion).order_by(
            InboxCustomerCompletionPolicyVersion.version.desc()
        )
    )


def require_active_policy(db: Session) -> InboxCustomerCompletionPolicyVersion:
    policy = active_policy(db)
    if policy is None:
        raise _error(
            "policy_unavailable",
            "The Inbox Customer completion policy is not configured.",
        )
    return policy


def snapshot_active_policy_id(db: Session) -> UUID:
    """Flush-neutral participant used by the conversation owner at creation."""

    policy = active_policy(db)
    if policy is None and db.get_bind().dialect.name == "sqlite":
        # The fast unit lane intentionally has no Alembic data migration. Keep
        # deployed databases fail-closed while giving metadata-only tests the
        # same initial policy contract.
        policy = InboxCustomerCompletionPolicyVersion(
            id=uuid5(_DEFAULT_POLICY_NAMESPACE, "initial-customer-completion-policy"),
            version=1,
            required_fields=list(DEFAULT_REQUIRED_FIELDS),
            decision_source="unit_test_baseline",
        )
        db.add(policy)
        db.flush()
    if policy is None:
        raise _error(
            "policy_unavailable",
            "The Inbox Customer completion policy is not configured.",
        )
    return policy.id


def create_policy_version(
    db: Session, command: CreateCustomerCompletionPolicyCommand
) -> CustomerCompletionPolicyOutcome:
    """Create, never mutate, the next policy version."""

    def operation() -> CustomerCompletionPolicyOutcome:
        required = tuple(dict.fromkeys(command.required_fields))
        source = command.decision_source.strip()
        if not source:
            raise _error("invalid_decision_source", "Decision source is required.")
        current = db.scalar(
            select(InboxCustomerCompletionPolicyVersion)
            .order_by(InboxCustomerCompletionPolicyVersion.version.desc())
            .with_for_update()
        )
        policy = InboxCustomerCompletionPolicyVersion(
            version=(current.version if current is not None else 0) + 1,
            required_fields=[field.value for field in required],
            created_by_person_id=command.actor_person_id,
            decision_source=source,
        )
        db.add(policy)
        db.flush()
        stage_audit_event(
            db,
            action="inbox_customer_completion_policy.created",
            entity_type="inbox_customer_completion_policy_version",
            entity_id=str(policy.id),
            actor=AuditActor(
                actor_type=command.actor_type,
                actor_id=(
                    str(command.actor_person_id)
                    if command.actor_person_id
                    else command.context.actor
                ),
            ),
            metadata={
                "decision_source": source,
                "policy_version": policy.version,
                "required_fields": [field.value for field in required],
                "correlation_id": str(command.context.correlation_id),
            },
        )
        return CustomerCompletionPolicyOutcome(policy.id, policy.version, required)

    return execute_owner_command(
        db,
        definition=_CREATE,
        context=command.context,
        operation=operation,
    )
