"""Governed NCC profile cleanup commands for AI-collected candidates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.subscriber import (
    Gender,
    Reseller,
    Subscriber,
    SubscriberCategory,
    UserType,
)
from app.models.subscriber_field_verification import SubscriberFieldVerification
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "customer.profile_cleanup"
_PROFILE_CLEANUP_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="governed subscriber DOB and gender cleanup command",
    name="execute_customer_profile_cleanup_command",
)


class ProfileCleanupOutcome(StrEnum):
    saved = "saved"
    ineligible = "ineligible"
    no_change = "no_change"
    refused = "refused"
    invalid = "invalid"
    conflict = "conflict"


@dataclass(frozen=True, slots=True)
class SubmitProfileCleanupCommand:
    context: CommandContext
    subscriber_id: UUID
    source_conversation_id: UUID
    candidate_gender: str | None = None
    candidate_date_of_birth: date | None = None
    gender_mapping: Mapping[str, str] | None = None
    consent_text: str | None = None
    attempt_count: int = 1
    activation_enabled: bool = False


@dataclass(frozen=True, slots=True)
class ProfileCleanupResult:
    outcome: ProfileCleanupOutcome
    subscriber_id: UUID
    saved_fields: tuple[str, ...] = ()
    reason: str | None = None


def _normalized_gender(
    value: str | None, *, gender_mapping: Mapping[str, str] | None = None
) -> Gender | None:
    if value is None:
        return None
    public_value = value.strip().lower().replace(" ", "_")
    mapping = {
        str(key).strip().lower().replace(" ", "_"): str(mapped).strip().lower()
        for key, mapped in (gender_mapping or {}).items()
        if str(key).strip() and str(mapped).strip()
    }
    normalized = mapping.get(public_value, public_value)
    if normalized in {"prefer_not_to_say", "undisclosed", "decline", "refused"}:
        return Gender.unknown
    try:
        return Gender(normalized)
    except ValueError as exc:
        raise ValueError("gender is not an approved value") from exc


def is_direct_residential_customer(subscriber: Subscriber) -> bool:
    reseller = subscriber.reseller
    return (
        subscriber.is_active
        and subscriber.category == SubscriberCategory.residential
        and subscriber.user_type == UserType.customer
        and reseller is not None
        and reseller.is_house
    )


def missing_cleanup_fields(subscriber: Subscriber) -> tuple[str, ...]:
    fields: list[str] = []
    if subscriber.gender in {None, Gender.unknown}:
        fields.append("gender")
    if subscriber.date_of_birth is None:
        fields.append("date_of_birth")
    return tuple(fields)


def submit_profile_cleanup(
    db: Session, command: SubmitProfileCleanupCommand
) -> ProfileCleanupResult:
    if command.attempt_count > 2:
        return ProfileCleanupResult(
            outcome=ProfileCleanupOutcome.invalid,
            subscriber_id=command.subscriber_id,
            reason="maximum cleanup attempts exceeded",
        )
    if not command.activation_enabled:
        return ProfileCleanupResult(
            outcome=ProfileCleanupOutcome.no_change,
            subscriber_id=command.subscriber_id,
            reason="profile cleanup collection is disabled",
        )

    def _operation() -> ProfileCleanupResult:
        subscriber = (
            db.query(Subscriber)
            .filter(Subscriber.id == command.subscriber_id)
            .with_for_update()
            .one_or_none()
        )
        if subscriber is None:
            return ProfileCleanupResult(
                outcome=ProfileCleanupOutcome.ineligible,
                subscriber_id=command.subscriber_id,
                reason="subscriber not found",
            )
        if subscriber.reseller_id is not None and subscriber.reseller is None:
            db.query(Reseller).filter(Reseller.id == subscriber.reseller_id).first()
        if not is_direct_residential_customer(subscriber):
            return ProfileCleanupResult(
                outcome=ProfileCleanupOutcome.ineligible,
                subscriber_id=subscriber.id,
                reason="subscriber is not a direct residential customer",
            )
        missing = set(missing_cleanup_fields(subscriber))
        saved: list[str] = []
        if command.candidate_gender is not None and "gender" in missing:
            gender = _normalized_gender(
                command.candidate_gender,
                gender_mapping=command.gender_mapping,
            )
            if gender is not None and gender is not Gender.unknown:
                subscriber.gender = gender
                saved.append("gender")
                _append_verification(
                    db,
                    subscriber=subscriber,
                    field_key="gender",
                    value=gender.value,
                    command=command,
                )
        if command.candidate_date_of_birth is not None and "date_of_birth" in missing:
            if command.candidate_date_of_birth >= date.today():
                return ProfileCleanupResult(
                    outcome=ProfileCleanupOutcome.invalid,
                    subscriber_id=subscriber.id,
                    reason="date of birth must be in the past",
                )
            subscriber.date_of_birth = command.candidate_date_of_birth
            saved.append("date_of_birth")
            _append_verification(
                db,
                subscriber=subscriber,
                field_key="date_of_birth",
                value=command.candidate_date_of_birth.isoformat(),
                command=command,
            )
        db.flush()
        if not saved:
            return ProfileCleanupResult(
                outcome=ProfileCleanupOutcome.no_change,
                subscriber_id=subscriber.id,
                reason="no missing approved field was provided",
            )
        return ProfileCleanupResult(
            outcome=ProfileCleanupOutcome.saved,
            subscriber_id=subscriber.id,
            saved_fields=tuple(saved),
        )

    return execute_owner_command(
        db,
        definition=_PROFILE_CLEANUP_COMMAND,
        context=command.context,
        operation=_operation,
    )


def _append_verification(
    db: Session,
    *,
    subscriber: Subscriber,
    field_key: str,
    value: str,
    command: SubmitProfileCleanupCommand,
) -> None:
    db.add(
        SubscriberFieldVerification(
            subscriber_id=subscriber.id,
            field_key=field_key,
            value=value,
            source="ai_intake",
            verified_at=datetime.now(UTC),
            verified_by_actor_id=command.context.actor,
            verified_by_actor_name="Dotmac Virtual Assistant",
            evidence={
                "source_conversation_id": str(command.source_conversation_id),
                "consent_recorded": bool(command.consent_text),
                "attempt_count": command.attempt_count,
            },
        )
    )
