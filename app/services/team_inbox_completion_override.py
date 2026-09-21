"""Single-use, audited legacy override for the Customer completion gate.

``communications.team_inbox_completion_override`` is the ONLY writer of
``inbox_completion_override_grants``. It narrowly re-opens agent resolution
for conversations that existed before the completion-policy backfill cutover
(``InboxConversation.completion_gate_precutover_at IS NOT NULL``) -- see
``docs/designs/INBOX_CUSTOMER_COMPLETION_GATE.md``.

The marker is eligibility, never authorization: a pre-cutover conversation
may be *considered* for a grant, but a grant is only issued after an
operator with ``support:inbox:completion_override`` reviews the exact live
missing-fields/canonical-values gap on that one conversation, and it is
burned at most once, inside the same transaction that performs the
resolution it unblocks -- ``app/services/team_inbox_status.py``'s
``_apply_status_transition`` is the sole consumer.

This module never resolves a conversation itself and is not a second writer
of ``inbox_conversations.status``; that stays
``communications.team_inbox_status``'s exclusive concern.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.team_inbox import (
    InboxCompletionOverrideGrant,
    InboxCompletionOverrideGrantState,
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxStatusTransitionEvent,
)
from app.services import settings_spec, team_inbox_customer_completion
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.team_inbox_customer_completion import InboxCustomerResolutionReadiness

OWNER = "communications.team_inbox_completion_override"
CONCERN = "single-use legacy customer-completion resolution override"
OVERRIDE_GRANT_SCOPE = "support:inbox:completion_override"

# Registered in `app/services/settings_spec.py`
# (`SettingDomain.comms`/`inbox_completion_override_grant_window_hours`) --
# database-authoritative, resolved via `settings_spec.resolve_integer`.
# `_grant_window_hours` below no longer reads the environment directly (see
# `tests/architecture/test_decision_input_ownership.py`); this constant is
# only the resolver's documented fallback if the spec is ever missing.
_DEFAULT_GRANT_WINDOW_HOURS = 24
_MAX_REASON_TEXT_LENGTH = 2000

# ADR-0008 style open registry: a product/deployment names its own override
# reason vocabulary without a kernel/enum change. Never an ALTER TYPE enum.
_REGISTERED_REASON_CODES: set[str] = set()


def register_override_reason_codes(codes: Iterable[str]) -> None:
    """Extend the accepted ``reason_code`` vocabulary for override grants."""

    _REGISTERED_REASON_CODES.update(codes)


register_override_reason_codes(
    (
        "legacy_conversation_pre_cutover_review",
        "legacy_conversation_data_unrecoverable",
        "legacy_conversation_customer_unreachable",
    )
)

_ISSUE = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="issue_completion_override_grant",
)


class TeamInboxCompletionOverrideError(DomainError):
    pass


def _error(
    suffix: str, message: str, **details: object
) -> TeamInboxCompletionOverrideError:
    return TeamInboxCompletionOverrideError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def _as_utc(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    SQLite drops timezone offsets on round-trip regardless of what was
    inserted, so a ``DateTime(timezone=True)`` column can come back naive
    after a commit/refresh even though PostgreSQL preserves it correctly.
    Comparing a freshly-fetched grant's ``expires_at`` against
    ``datetime.now(UTC)`` must not depend on the driver behaving; matching
    the ``_as_utc`` pattern already used across this codebase (e.g.
    ``app/services/auth_flow.py``, ``app/services/auth_session_refresh.py``).
    """

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _grant_window_hours(db: Session) -> int:
    """Resolve the grant validity window from the registered setting.

    Database-authoritative via ``settings_spec.resolve_integer`` -- never the
    environment directly (``INBOX_COMPLETION_OVERRIDE_GRANT_WINDOW_HOURS`` is
    only the spec's declared bootstrap ``env_var``, materialised into a row by
    the settings seed, exactly like every other Sub runtime setting).
    """

    value = settings_spec.resolve_integer(
        db, SettingDomain.comms, "inbox_completion_override_grant_window_hours"
    )
    return value if value > 0 else _DEFAULT_GRANT_WINDOW_HOURS


def _normalize_reason_text(reason: str | None) -> str:
    normalized = " ".join(str(reason or "").split())
    if not normalized:
        raise _error(
            "override_reason_required",
            "A reviewed override reason is required.",
        )
    if len(normalized) > _MAX_REASON_TEXT_LENGTH:
        raise _error(
            "override_reason_too_long",
            f"The override reason must not exceed {_MAX_REASON_TEXT_LENGTH} characters.",
        )
    return normalized


def _canonical_values_digest(db: Session, conversation: InboxConversation) -> str:
    values = team_inbox_customer_completion.canonical_customer_values(db, conversation)
    canonical = "|".join(
        f"{field.value}={values.get(field) or ''}"
        for field in sorted(values, key=lambda item: item.value)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_live_evidence(
    db: Session, conversation: InboxConversation
) -> tuple[tuple[str, ...], str]:
    """Return the exact live (missing_fields, canonical_values_digest) pair.

    An operator must review this before requesting a grant -- it is the
    evidence ``IssueCompletionOverrideCommand.expected_missing_fields`` /
    ``expected_canonical_values_digest`` are fenced against. Public so a
    caller (the CLI issuance tool, a future admin route) can gather it
    without reaching into this module's private digest helper.
    """

    readiness = team_inbox_customer_completion.resolution_readiness(db, conversation)
    missing = tuple(sorted(field.value for field in readiness.missing_fields))
    return missing, _canonical_values_digest(db, conversation)


def _grant_fingerprint(
    *,
    conversation_id: UUID,
    reason_code: str,
    reason_text: str,
    missing_fields: tuple[str, ...],
    canonical_values_digest: str,
) -> str:
    payload = "|".join(
        (
            str(conversation_id),
            reason_code,
            reason_text,
            ",".join(sorted(missing_fields)),
            canonical_values_digest,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IssueCompletionOverrideCommand:
    context: CommandContext
    conversation_id: UUID
    reason_code: str
    expected_missing_fields: tuple[str, ...]
    expected_canonical_values_digest: str
    # Real access control lives at the actual invocation boundary: the
    # calling CLI/route resolves a named staff principal's granted roles via
    # ``system_user_role_names`` + ``has_permission`` and passes the result
    # here. This owner only refuses when that caller-checked evidence is
    # missing -- matching ``prepaid_draft_reconciliation.REPAIR_SCOPE``'s
    # ``permission_granted`` contract. ``context.scope`` alone is a free-text
    # label a caller could set to anything; it proves nothing by itself.
    permission_granted: bool
    # The real staff principal whose granted role authorized
    # ``permission_granted``. ``context.actor`` stays a free-text audit
    # label; this is the only identifier with actual RBAC meaning, so it is
    # recorded alongside it on the grant row.
    actor_system_user_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CompletionOverrideGrantOutcome:
    grant_id: UUID
    conversation_id: UUID
    state: str
    granted_at: datetime
    expires_at: datetime
    already_issued: bool


@dataclass(frozen=True, slots=True)
class CompletionOverrideConsumption:
    grant_id: UUID
    conversation_id: UUID
    consumed_at: datetime
    consumed_resolution_reason: str


def _outcome_from_row(
    grant: InboxCompletionOverrideGrant, *, already_issued: bool
) -> CompletionOverrideGrantOutcome:
    return CompletionOverrideGrantOutcome(
        grant_id=grant.id,
        conversation_id=grant.conversation_id,
        state=grant.state,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        already_issued=already_issued,
    )


def issue_override_grant(
    db: Session, command: IssueCompletionOverrideCommand
) -> CompletionOverrideGrantOutcome:
    """Issue a single-use resolution override for one pre-cutover conversation.

    Every precondition below is required; none may be skipped or reordered
    to widen eligibility -- see
    ``tests/test_team_inbox_completion_override.py``.
    """

    def operation() -> CompletionOverrideGrantOutcome:
        if (
            command.context.scope != OVERRIDE_GRANT_SCOPE
            or not command.permission_granted
        ):
            # `context.scope` alone is a free-text label the caller could set
            # to anything; `permission_granted` is the caller-checked
            # evidence that the real staff principal actually holds
            # `support:inbox:completion_override` (resolved via
            # `system_user_role_names` + `has_permission` at the invocation
            # boundary -- see `scripts/support/issue_inbox_completion_override.py`).
            raise _error(
                "permission_denied",
                f"Issuing this override requires {OVERRIDE_GRANT_SCOPE}.",
                scope=command.context.scope,
            )
        if command.reason_code not in _REGISTERED_REASON_CODES:
            raise _error(
                "invalid_reason_code",
                "Unsupported override reason code.",
                reason_code=command.reason_code,
            )
        if not command.context.idempotency_key:
            raise _error(
                "override_idempotency_key_required",
                "An idempotency key is required to issue an override grant.",
            )
        reason_text = _normalize_reason_text(command.context.reason)

        conversation = db.scalar(
            select(InboxConversation)
            .where(InboxConversation.id == command.conversation_id)
            .with_for_update()
        )
        if conversation is None:
            raise _error(
                "conversation_not_found",
                "Conversation was not found.",
                conversation_id=str(command.conversation_id),
            )

        existing = db.scalar(
            select(InboxCompletionOverrideGrant).where(
                InboxCompletionOverrideGrant.conversation_id == conversation.id,
                InboxCompletionOverrideGrant.grant_idempotency_key
                == command.context.idempotency_key,
            )
        )

        if conversation.completion_gate_precutover_at is None:
            raise _error(
                "override_post_cutover_conversation",
                "A conversation created after the completion-policy cutover "
                "can never receive a legacy override.",
                conversation_id=str(conversation.id),
            )
        if conversation.status == InboxConversationStatus.resolved.value:
            raise _error(
                "override_conversation_already_resolved",
                "An already-resolved conversation needs no override.",
                conversation_id=str(conversation.id),
            )

        readiness = team_inbox_customer_completion.resolution_readiness(
            db, conversation
        )
        if readiness.can_agent_resolve:
            raise _error(
                "override_conversation_not_blocked",
                "This conversation is not blocked; no override is needed.",
                conversation_id=str(conversation.id),
            )
        if conversation.subscriber_id is None:
            raise _error(
                "override_requires_customer_identity",
                "An override grant requires a conversation linked to a "
                "Customer; identity-classification blockers cannot be "
                "overridden here.",
                conversation_id=str(conversation.id),
            )

        live_missing = tuple(sorted(field.value for field in readiness.missing_fields))
        expected_missing = tuple(sorted(command.expected_missing_fields))
        live_digest = _canonical_values_digest(db, conversation)
        if (
            live_missing != expected_missing
            or live_digest != command.expected_canonical_values_digest
        ):
            raise _error(
                "override_stale_evidence",
                "The reviewed gap no longer matches the conversation's live "
                "state. Re-review before granting an override.",
                conversation_id=str(conversation.id),
                live_missing_fields=list(live_missing),
                expected_missing_fields=list(expected_missing),
            )

        fingerprint = _grant_fingerprint(
            conversation_id=conversation.id,
            reason_code=command.reason_code,
            reason_text=reason_text,
            missing_fields=live_missing,
            canonical_values_digest=live_digest,
        )
        if existing is not None:
            if existing.grant_fingerprint != fingerprint:
                raise _error(
                    "override_idempotency_key_conflict",
                    "This idempotency key was already used for a different "
                    "override request.",
                    conversation_id=str(conversation.id),
                )
            return _outcome_from_row(existing, already_issued=True)

        pending = db.scalar(
            select(InboxCompletionOverrideGrant).where(
                InboxCompletionOverrideGrant.conversation_id == conversation.id,
                InboxCompletionOverrideGrant.state
                == InboxCompletionOverrideGrantState.pending.value,
            )
        )
        if pending is not None:
            raise _error(
                "override_grant_already_pending",
                "An outstanding override grant already exists for this conversation.",
                conversation_id=str(conversation.id),
                pending_grant_id=str(pending.id),
            )

        granted_at = datetime.now(UTC)
        grant = InboxCompletionOverrideGrant(
            id=uuid4(),
            conversation_id=conversation.id,
            subscriber_id=conversation.subscriber_id,
            policy_version_id=conversation.customer_completion_policy_version_id,
            missing_fields=list(live_missing),
            canonical_values_digest=live_digest,
            reason_code=command.reason_code,
            reason_text=reason_text,
            granted_by=command.context.actor,
            granted_by_system_user_id=command.actor_system_user_id,
            granted_at=granted_at,
            grant_idempotency_key=command.context.idempotency_key,
            grant_fingerprint=fingerprint,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
            expires_at=granted_at + timedelta(hours=_grant_window_hours(db)),
            state=InboxCompletionOverrideGrantState.pending.value,
        )
        db.add(grant)
        try:
            db.flush()
        except IntegrityError as exc:
            raise _error(
                "override_grant_already_pending",
                "An outstanding override grant already exists for this conversation.",
                conversation_id=str(conversation.id),
            ) from exc
        return _outcome_from_row(grant, already_issued=False)

    return execute_owner_command(
        db,
        definition=_ISSUE,
        context=command.context,
        operation=operation,
    )


def consume_override_for_resolution(
    db: Session,
    *,
    conversation: InboxConversation,
    readiness: InboxCustomerResolutionReadiness,
    actor_person_id: UUID | None,
    resolution_reason: str,
    override_grant_id: UUID | None,
    transition_event_id: UUID,
    occurred_at: datetime,
) -> CompletionOverrideConsumption:
    """Burn one grant, single-use, inside the caller's active transaction.

    Flush-only participant: this never begins, commits, or rolls back a
    transaction. It is called only from
    ``team_inbox_status._apply_status_transition`` while resolution is
    already blocked -- never as a second entry point into resolution.
    """

    if readiness.can_agent_resolve:
        raise _error(
            "override_not_required",
            "No override is needed; the resolution already satisfies requirements.",
            conversation_id=str(conversation.id),
        )
    if override_grant_id is None:
        raise _error(
            "override_absent",
            "This conversation requires a granted override to resolve.",
            conversation_id=str(conversation.id),
        )

    grant = db.scalar(
        select(InboxCompletionOverrideGrant)
        .where(InboxCompletionOverrideGrant.id == override_grant_id)
        .with_for_update()
    )
    if grant is None:
        raise _error(
            "override_absent",
            "The referenced override grant was not found.",
            conversation_id=str(conversation.id),
            override_grant_id=str(override_grant_id),
        )
    if grant.conversation_id != conversation.id:
        raise _error(
            "override_conversation_mismatch",
            "The referenced override grant belongs to a different conversation.",
            conversation_id=str(conversation.id),
            override_grant_id=str(override_grant_id),
        )

    if grant.state == InboxCompletionOverrideGrantState.pending.value:
        reopened = db.scalar(
            select(InboxStatusTransitionEvent.id)
            .where(
                InboxStatusTransitionEvent.conversation_id == conversation.id,
                InboxStatusTransitionEvent.occurred_at > grant.granted_at,
                InboxStatusTransitionEvent.status
                != InboxConversationStatus.resolved.value,
            )
            .limit(1)
        )
        new_activity = (
            reopened is None
            and db.scalar(
                select(InboxMessage.id)
                .where(
                    InboxMessage.conversation_id == conversation.id,
                    InboxMessage.created_at > grant.granted_at,
                )
                .limit(1)
            )
            is not None
        )
        if reopened is not None or new_activity:
            grant.state = InboxCompletionOverrideGrantState.superseded.value
            db.flush()
        elif datetime.now(UTC) > _as_utc(grant.expires_at):
            grant.state = InboxCompletionOverrideGrantState.expired.value
            db.flush()

    if grant.state == InboxCompletionOverrideGrantState.consumed.value:
        raise _error(
            "override_already_consumed",
            "This override grant was already consumed.",
            conversation_id=str(conversation.id),
            override_grant_id=str(override_grant_id),
        )
    if grant.state == InboxCompletionOverrideGrantState.superseded.value:
        raise _error(
            "override_superseded",
            "This override grant was superseded by new conversation "
            "activity since it was granted.",
            conversation_id=str(conversation.id),
            override_grant_id=str(override_grant_id),
        )
    if grant.state == InboxCompletionOverrideGrantState.expired.value:
        raise _error(
            "override_expired",
            "This override grant expired before it was used.",
            conversation_id=str(conversation.id),
            override_grant_id=str(override_grant_id),
        )

    live_digest = _canonical_values_digest(db, conversation)
    if (
        live_digest != grant.canonical_values_digest
        or conversation.customer_completion_policy_version_id != grant.policy_version_id
    ):
        raise _error(
            "override_stale_evidence",
            "The conversation's canonical data changed since this override "
            "was granted. Request a fresh override.",
            conversation_id=str(conversation.id),
            override_grant_id=str(override_grant_id),
        )

    result = db.execute(
        update(InboxCompletionOverrideGrant)
        .where(
            InboxCompletionOverrideGrant.id == grant.id,
            InboxCompletionOverrideGrant.state
            == InboxCompletionOverrideGrantState.pending.value,
        )
        .values(
            state=InboxCompletionOverrideGrantState.consumed.value,
            consumed_at=occurred_at,
            consumed_by=(str(actor_person_id) if actor_person_id else None),
            consumed_transition_event_id=transition_event_id,
            consumed_resolution_reason=resolution_reason,
        )
    )
    if result.rowcount == 0:
        raise _error(
            "override_already_consumed",
            "This override grant was already consumed.",
            conversation_id=str(conversation.id),
            override_grant_id=str(override_grant_id),
        )
    db.flush()
    return CompletionOverrideConsumption(
        grant_id=grant.id,
        conversation_id=conversation.id,
        consumed_at=occurred_at,
        consumed_resolution_reason=resolution_reason,
    )
