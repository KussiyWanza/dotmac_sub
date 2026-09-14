from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.subscriber import Subscriber
from app.models.team_inbox import (
    InboxAuditEvidenceGrade,
    InboxAuditSource,
    InboxCompletionOverrideGrant,
    InboxCompletionOverrideGrantState,
    InboxConversation,
    InboxConversationStatus,
    InboxCustomerCompletionPolicyVersion,
    InboxMessage,
    InboxStatusTransitionEvent,
)
from app.services import team_inbox_completion_override as override_service
from app.services import (
    team_inbox_customer_completion,
    team_inbox_operations,
    team_inbox_status,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext, execute_owner_command
from app.services.team_inbox_commands import _ADMIN_MUTATION

_CODE = override_service.OWNER


def _policy(db_session, fields=("name", "phone", "address")):
    existing = db_session.query(InboxCustomerCompletionPolicyVersion).first()
    if existing is not None:
        return existing
    policy = InboxCustomerCompletionPolicyVersion(
        version=1, required_fields=list(fields), decision_source="pytest"
    )
    db_session.add(policy)
    db_session.flush()
    return policy


def _conversation(
    db_session,
    *,
    precutover: bool = True,
    status: str = "open",
    phone: str | None = None,
    address: str | None = None,
    name: str | None = "Ada Lovelace",
) -> tuple[InboxConversation, Subscriber]:
    policy = _policy(db_session)
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Lovelace",
        display_name=name,
        email=f"{uuid4().hex}@example.test",
        phone=phone,
        address_line1=address,
    )
    db_session.add(subscriber)
    db_session.flush()
    conversation = InboxConversation(
        subscriber_id=subscriber.id,
        customer_completion_policy_version_id=policy.id,
        channel_type="whatsapp",
        status=status,
        is_active=True,
        completion_gate_precutover_at=(
            datetime.now(UTC) - timedelta(days=1) if precutover else None
        ),
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation, subscriber


def _blocked_conversation(db_session, **kwargs) -> tuple[InboxConversation, Subscriber]:
    """A pre-cutover, open, Customer-classified conversation missing phone+address."""

    kwargs.setdefault("phone", None)
    kwargs.setdefault("address", None)
    return _conversation(db_session, **kwargs)


def _context(**overrides) -> CommandContext:
    defaults: dict[str, object] = dict(
        command_id=uuid4(),
        correlation_id=uuid4(),
        actor="admin@example.test",
        scope=override_service.OVERRIDE_GRANT_SCOPE,
        reason="Legacy conversation reviewed ahead of the completion-policy cutover.",
        idempotency_key=str(uuid4()),
    )
    defaults.update(overrides)
    return CommandContext(**defaults)


def _live_evidence(
    db_session, conversation: InboxConversation
) -> tuple[tuple[str, ...], str]:
    readiness = team_inbox_customer_completion.resolution_readiness(
        db_session, conversation
    )
    missing = tuple(sorted(field.value for field in readiness.missing_fields))
    digest = override_service._canonical_values_digest(db_session, conversation)
    return missing, digest


def _issue(
    db_session,
    conversation,
    *,
    context: CommandContext | None = None,
    permission_granted: bool = True,
):
    """Issue a grant, honoring ``execute_owner_command``'s transaction-free-entry
    requirement.

    In production the operator's earlier read (computing the expected
    evidence) and the issuance write are always separate requests/
    transactions. This helper mirrors that boundary explicitly: gather
    evidence, then commit, then issue -- matching the
    ``db_session.commit()``-before-an-owner-command pattern already used by
    ``tests/test_inbox_customer_completion.py`` for
    ``complete_customer_profile``.
    """

    conversation_id = conversation.id
    missing, digest = _live_evidence(db_session, conversation)
    db_session.commit()
    command = override_service.IssueCompletionOverrideCommand(
        context=context or _context(),
        conversation_id=conversation_id,
        reason_code="legacy_conversation_pre_cutover_review",
        expected_missing_fields=missing,
        expected_canonical_values_digest=digest,
        permission_granted=permission_granted,
        actor_system_user_id=uuid4(),
    )
    return override_service.issue_override_grant(db_session, command)


# --- Issuance preconditions ------------------------------------------------


def test_issue_override_grant_refuses_without_real_permission_grant(db_session):
    """A free-text ``context.scope`` label alone must never authorize a grant.

    ``permission_granted`` is the caller-checked evidence that the real
    staff principal actually holds ``support:inbox:completion_override``
    (resolved via ``system_user_role_names``/``has_permission`` at the
    invocation boundary, never trusted from the command alone).
    """

    conversation, _subscriber = _blocked_conversation(db_session)

    with pytest.raises(DomainError) as excinfo:
        _issue(db_session, conversation, permission_granted=False)

    assert excinfo.value.code == f"{_CODE}.permission_denied"


def test_post_cutover_conversation_can_never_receive_a_grant(db_session):
    """Precondition #1: eligibility requires the pre-cutover marker.

    A post-cutover conversation must be refused even though it is otherwise
    blocked -- the marker, not the block, is what makes a conversation
    eligible for consideration.
    """

    conversation, _subscriber = _blocked_conversation(db_session, precutover=False)

    with pytest.raises(DomainError) as excinfo:
        _issue(db_session, conversation)

    assert excinfo.value.code == f"{_CODE}.override_post_cutover_conversation"


def test_issue_override_grant_requires_conversation_not_already_resolved(db_session):
    """Precondition #2: an already-resolved conversation needs no override."""

    conversation, _subscriber = _blocked_conversation(
        db_session, status=InboxConversationStatus.resolved.value
    )

    with pytest.raises(DomainError) as excinfo:
        _issue(db_session, conversation)

    assert excinfo.value.code == f"{_CODE}.override_conversation_already_resolved"


def test_issue_override_grant_requires_conversation_currently_blocked(db_session):
    """Precondition #3: a conversation that already satisfies the gate is refused."""

    conversation, _subscriber = _conversation(
        db_session, phone="+2348012345678", address="1 Example Road"
    )

    with pytest.raises(DomainError) as excinfo:
        _issue(db_session, conversation)

    assert excinfo.value.code == f"{_CODE}.override_conversation_not_blocked"


def test_issue_override_grant_refuses_stale_evidence(db_session):
    """Precondition #4: expected evidence must match what's live right now."""

    conversation, _subscriber = _blocked_conversation(db_session)
    conversation_id = conversation.id
    db_session.commit()
    command = override_service.IssueCompletionOverrideCommand(
        context=_context(),
        conversation_id=conversation_id,
        reason_code="legacy_conversation_pre_cutover_review",
        expected_missing_fields=("name",),  # stale: name is not actually missing
        expected_canonical_values_digest="0" * 64,
        permission_granted=True,
        actor_system_user_id=uuid4(),
    )

    with pytest.raises(DomainError) as excinfo:
        override_service.issue_override_grant(db_session, command)

    assert excinfo.value.code == f"{_CODE}.override_stale_evidence"


def test_issue_override_grant_requires_customer_identity(db_session):
    conversation, _subscriber = _blocked_conversation(db_session)
    conversation.subscriber_id = None
    db_session.flush()

    with pytest.raises(DomainError) as excinfo:
        _issue(db_session, conversation)

    assert excinfo.value.code == f"{_CODE}.override_requires_customer_identity"


def test_issue_override_grant_happy_path(db_session):
    conversation, _subscriber = _blocked_conversation(db_session)

    outcome = _issue(db_session, conversation)

    assert outcome.already_issued is False
    assert outcome.state == InboxCompletionOverrideGrantState.pending.value
    row = db_session.get(InboxCompletionOverrideGrant, outcome.grant_id)
    assert row is not None
    assert row.conversation_id == conversation.id
    assert row.subscriber_id == conversation.subscriber_id
    assert set(row.missing_fields) == {"address", "phone"}
    assert row.reason_code == "legacy_conversation_pre_cutover_review"
    assert row.state == "pending"


def test_issue_override_grant_idempotent_replay_returns_same_grant(db_session):
    conversation, _subscriber = _blocked_conversation(db_session)
    context = _context()

    first = _issue(db_session, conversation, context=context)
    second = _issue(db_session, conversation, context=context)

    assert second.grant_id == first.grant_id
    assert second.already_issued is True


def test_issue_override_grant_conflicting_idempotency_key_refused(db_session):
    conversation, _subscriber = _blocked_conversation(db_session)
    conversation_id = conversation.id
    key = str(uuid4())
    _issue(db_session, conversation, context=_context(idempotency_key=key))

    # Same idempotency key, but a different reason -- a genuinely different
    # request must not silently replay the first grant's outcome.
    digest = override_service._canonical_values_digest(db_session, conversation)
    db_session.commit()
    command = override_service.IssueCompletionOverrideCommand(
        context=_context(idempotency_key=key, reason="A materially different review."),
        conversation_id=conversation_id,
        reason_code="legacy_conversation_data_unrecoverable",
        expected_missing_fields=("address", "phone"),
        expected_canonical_values_digest=digest,
        permission_granted=True,
        actor_system_user_id=uuid4(),
    )

    with pytest.raises(DomainError) as excinfo:
        override_service.issue_override_grant(db_session, command)

    assert excinfo.value.code == f"{_CODE}.override_idempotency_key_conflict"


def test_issue_override_grant_second_pending_request_refused(db_session):
    """Application-level half of the one-outstanding-grant-per-conversation rule."""

    conversation, _subscriber = _blocked_conversation(db_session)
    _issue(db_session, conversation)

    with pytest.raises(DomainError) as excinfo:
        _issue(db_session, conversation, context=_context())

    assert excinfo.value.code == f"{_CODE}.override_grant_already_pending"


def test_partial_unique_index_blocks_a_second_pending_grant_row(db_session):
    """DB-level half: the partial unique index itself, bypassing the service.

    Two directly-constructed pending grant rows for the same conversation
    race the same ``UNIQUE (conversation_id) WHERE state = 'pending'`` index
    the service's proactive check also relies on. The first insert commits
    (flushes) cleanly; the second must fail at the database, independent of
    any application-level check.
    """

    conversation, _subscriber = _blocked_conversation(db_session)
    now = datetime.now(UTC)
    common = dict(
        conversation_id=conversation.id,
        subscriber_id=conversation.subscriber_id,
        policy_version_id=conversation.customer_completion_policy_version_id,
        missing_fields=["address", "phone"],
        canonical_values_digest="a" * 64,
        reason_code="legacy_conversation_pre_cutover_review",
        reason_text="Reviewed.",
        granted_by="admin@example.test",
        granted_at=now,
        grant_fingerprint="b" * 64,
        command_id=uuid4(),
        correlation_id=uuid4(),
        expires_at=now + timedelta(hours=24),
        state=InboxCompletionOverrideGrantState.pending.value,
    )
    db_session.add(
        InboxCompletionOverrideGrant(
            id=uuid4(), grant_idempotency_key=str(uuid4()), **common
        )
    )
    db_session.flush()

    db_session.add(
        InboxCompletionOverrideGrant(
            id=uuid4(), grant_idempotency_key=str(uuid4()), **common
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- Consumption -------------------------------------------------------


def _readiness(db_session, conversation):
    return team_inbox_customer_completion.resolution_readiness(db_session, conversation)


def test_consume_override_refuses_when_grant_id_absent(db_session):
    conversation, _subscriber = _blocked_conversation(db_session)

    with pytest.raises(DomainError) as excinfo:
        override_service.consume_override_for_resolution(
            db_session,
            conversation=conversation,
            readiness=_readiness(db_session, conversation),
            actor_person_id=None,
            resolution_reason="operator_change",
            override_grant_id=None,
            transition_event_id=uuid4(),
            occurred_at=datetime.now(UTC),
        )

    assert excinfo.value.code == f"{_CODE}.override_absent"


def test_consume_override_refuses_unknown_grant_id(db_session):
    conversation, _subscriber = _blocked_conversation(db_session)

    with pytest.raises(DomainError) as excinfo:
        override_service.consume_override_for_resolution(
            db_session,
            conversation=conversation,
            readiness=_readiness(db_session, conversation),
            actor_person_id=None,
            resolution_reason="operator_change",
            override_grant_id=uuid4(),
            transition_event_id=uuid4(),
            occurred_at=datetime.now(UTC),
        )

    assert excinfo.value.code == f"{_CODE}.override_absent"


def test_consume_override_refuses_conversation_mismatch(db_session):
    conversation, _subscriber = _blocked_conversation(db_session)
    other_conversation, _other = _blocked_conversation(db_session)
    grant = _issue(db_session, other_conversation)

    with pytest.raises(DomainError) as excinfo:
        override_service.consume_override_for_resolution(
            db_session,
            conversation=conversation,
            readiness=_readiness(db_session, conversation),
            actor_person_id=None,
            resolution_reason="operator_change",
            override_grant_id=grant.grant_id,
            transition_event_id=uuid4(),
            occurred_at=datetime.now(UTC),
        )

    assert excinfo.value.code == f"{_CODE}.override_conversation_mismatch"


def test_consume_override_refuses_already_consumed(db_session):
    conversation, _subscriber = _blocked_conversation(db_session)
    grant = _issue(db_session, conversation)
    event = InboxStatusTransitionEvent(
        conversation_id=conversation.id,
        previous_status="open",
        status="resolved",
        reason_code="operator_change",
        source=InboxAuditSource.status_command,
        source_id=f"test:{uuid4()}",
        evidence_grade=InboxAuditEvidenceGrade.native,
        occurred_at=datetime.now(UTC),
    )
    db_session.add(event)
    db_session.flush()

    override_service.consume_override_for_resolution(
        db_session,
        conversation=conversation,
        readiness=_readiness(db_session, conversation),
        actor_person_id=None,
        resolution_reason="operator_change",
        override_grant_id=grant.grant_id,
        transition_event_id=event.id,
        occurred_at=datetime.now(UTC),
    )

    with pytest.raises(DomainError) as excinfo:
        override_service.consume_override_for_resolution(
            db_session,
            conversation=conversation,
            readiness=_readiness(db_session, conversation),
            actor_person_id=None,
            resolution_reason="operator_change",
            override_grant_id=grant.grant_id,
            transition_event_id=event.id,
            occurred_at=datetime.now(UTC),
        )

    assert excinfo.value.code == f"{_CODE}.override_already_consumed"


def test_consume_override_refuses_superseded_after_reopen(db_session):
    conversation, _subscriber = _blocked_conversation(db_session)
    grant = _issue(db_session, conversation)

    reopen_event = InboxStatusTransitionEvent(
        conversation_id=conversation.id,
        previous_status="resolved",
        status="open",
        reason_code="campaign_reopen",
        source=InboxAuditSource.status_command,
        source_id=f"test:{uuid4()}",
        evidence_grade=InboxAuditEvidenceGrade.native,
        occurred_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    db_session.add(reopen_event)
    db_session.flush()

    with pytest.raises(DomainError) as excinfo:
        override_service.consume_override_for_resolution(
            db_session,
            conversation=conversation,
            readiness=_readiness(db_session, conversation),
            actor_person_id=None,
            resolution_reason="operator_change",
            override_grant_id=grant.grant_id,
            transition_event_id=uuid4(),
            occurred_at=datetime.now(UTC),
        )

    assert excinfo.value.code == f"{_CODE}.override_superseded"
    row = db_session.get(InboxCompletionOverrideGrant, grant.grant_id)
    assert row.state == InboxCompletionOverrideGrantState.superseded.value


def test_consume_override_refuses_superseded_after_new_activity(db_session):
    conversation, _subscriber = _blocked_conversation(db_session)
    grant = _issue(db_session, conversation)

    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type="whatsapp",
        direction="inbound",
        body="Any update?",
        created_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    db_session.add(message)
    db_session.flush()

    with pytest.raises(DomainError) as excinfo:
        override_service.consume_override_for_resolution(
            db_session,
            conversation=conversation,
            readiness=_readiness(db_session, conversation),
            actor_person_id=None,
            resolution_reason="operator_change",
            override_grant_id=grant.grant_id,
            transition_event_id=uuid4(),
            occurred_at=datetime.now(UTC),
        )

    assert excinfo.value.code == f"{_CODE}.override_superseded"


def test_consume_override_refuses_expired(db_session):
    conversation, _subscriber = _blocked_conversation(db_session)
    grant = _issue(db_session, conversation)
    row = db_session.get(InboxCompletionOverrideGrant, grant.grant_id)
    # A grant is valid at creation (expires_at > granted_at) but has since
    # expired relative to "now" -- not "issued already-invalid", which the
    # ck_inbox_completion_override_grants_expiry_after_grant CHECK constraint
    # now correctly refuses at flush time.
    row.granted_at = datetime.now(UTC) - timedelta(hours=2)
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.flush()

    with pytest.raises(DomainError) as excinfo:
        override_service.consume_override_for_resolution(
            db_session,
            conversation=conversation,
            readiness=_readiness(db_session, conversation),
            actor_person_id=None,
            resolution_reason="operator_change",
            override_grant_id=grant.grant_id,
            transition_event_id=uuid4(),
            occurred_at=datetime.now(UTC),
        )

    assert excinfo.value.code == f"{_CODE}.override_expired"
    assert row.state == InboxCompletionOverrideGrantState.expired.value


def test_consume_override_refuses_stale_evidence_after_data_change(db_session):
    conversation, subscriber = _blocked_conversation(db_session)
    grant = _issue(db_session, conversation)

    subscriber.phone = "+2348011112222"  # data changed after the grant was issued
    db_session.flush()

    with pytest.raises(DomainError) as excinfo:
        override_service.consume_override_for_resolution(
            db_session,
            conversation=conversation,
            readiness=_readiness(db_session, conversation),
            actor_person_id=None,
            resolution_reason="operator_change",
            override_grant_id=grant.grant_id,
            transition_event_id=uuid4(),
            occurred_at=datetime.now(UTC),
        )

    assert excinfo.value.code == f"{_CODE}.override_stale_evidence"


def test_consume_override_happy_path_burns_grant(db_session):
    conversation, _subscriber = _blocked_conversation(db_session)
    grant = _issue(db_session, conversation)
    occurred_at = datetime.now(UTC)
    # `consumed_transition_event_id` carries a real FK to
    # `inbox_status_transition_events`; a fabricated `uuid4()` here would
    # violate it at the final UPDATE this call performs (a fabricated id is
    # only safe on the OTHER tests in this file that raise before reaching
    # that write -- see the module-level note on this pattern).
    event = InboxStatusTransitionEvent(
        conversation_id=conversation.id,
        previous_status="open",
        status="resolved",
        reason_code="operator_change",
        source=InboxAuditSource.status_command,
        source_id=f"test:{uuid4()}",
        evidence_grade=InboxAuditEvidenceGrade.native,
        occurred_at=occurred_at,
    )
    db_session.add(event)
    db_session.flush()

    outcome = override_service.consume_override_for_resolution(
        db_session,
        conversation=conversation,
        readiness=_readiness(db_session, conversation),
        actor_person_id=None,
        resolution_reason="operator_change",
        override_grant_id=grant.grant_id,
        transition_event_id=event.id,
        occurred_at=occurred_at,
    )

    assert outcome.grant_id == grant.grant_id
    row = db_session.get(InboxCompletionOverrideGrant, grant.grant_id)
    assert row.state == InboxCompletionOverrideGrantState.consumed.value
    assert row.consumed_transition_event_id == event.id
    assert row.consumed_resolution_reason == "operator_change"


# --- End-to-end through the shared status choke point -----------------


def test_full_happy_path_grant_resolve_burn_then_strict_fallback(db_session):
    """Grant -> resolve -> burned -> a second attempt has no grant available.

    After the conversation is resolved and later reopened with new activity,
    a second agent-resolution attempt with no fresh grant correctly falls
    back to the strict, pre-existing gate (``resolution_blocked``), not a
    different override-shaped error -- the override mechanism never widens
    what the strict gate would otherwise refuse.
    """

    conversation, _subscriber = _blocked_conversation(db_session)
    conversation_id = conversation.id
    grant = _issue(db_session, conversation)

    # Consuming a grant requires an active owner command (see
    # `_apply_status_transition`'s savepoint-or-raise guard); every
    # production entry point (direct/bulk/macro) already provides one via
    # `_commit`, so this test provides one explicitly too.
    def operation():
        current = db_session.get(InboxConversation, conversation_id)
        return team_inbox_status.apply_status_transition(
            db_session,
            conversation=current,
            status=InboxConversationStatus.resolved,
            actor_person_id=None,
            reason=team_inbox_status.InboxStatusReason.operator_change,
            completion_override_grant_id=grant.grant_id,
        )

    _run_as_owner_command(db_session, operation)
    conversation = db_session.get(InboxConversation, conversation_id)
    assert conversation.status == InboxConversationStatus.resolved.value
    row = db_session.get(InboxCompletionOverrideGrant, grant.grant_id)
    assert row.state == InboxCompletionOverrideGrantState.consumed.value

    # Reopen with new activity, and no fresh grant is available this time.
    team_inbox_status.apply_status_transition(
        db_session,
        conversation=conversation,
        status=InboxConversationStatus.open,
        actor_person_id=None,
        reason=team_inbox_status.InboxStatusReason.campaign_reopen,
    )

    with pytest.raises(DomainError) as excinfo:
        team_inbox_status.apply_status_transition(
            db_session,
            conversation=conversation,
            status=InboxConversationStatus.resolved,
            actor_person_id=None,
            reason=team_inbox_status.InboxStatusReason.operator_change,
        )

    assert (
        excinfo.value.code
        == "communications.team_inbox_customer_completion.resolution_blocked"
    )


def test_consuming_a_grant_outside_an_owner_command_fails_closed(db_session):
    """A grant id supplied outside an active owner command is a programming
    error and must raise, not silently run un-isolated.

    Every production entry point (direct/bulk/macro) reaches the
    grant-consumption path only through `_commit`'s `execute_owner_command`.
    """

    conversation, _subscriber = _blocked_conversation(db_session)
    grant = _issue(db_session, conversation)

    with pytest.raises(team_inbox_status.InboxStatusTransitionError):
        team_inbox_status.apply_status_transition(
            db_session,
            conversation=conversation,
            status=InboxConversationStatus.resolved,
            actor_person_id=None,
            reason=team_inbox_status.InboxStatusReason.operator_change,
            completion_override_grant_id=grant.grant_id,
        )


# --- Regression: no phantom `resolved` audit events for a blocked
# bulk/macro resolution -------------------------------------------------


def _run_as_owner_command(db_session, operation):
    return execute_owner_command(
        db_session,
        definition=_ADMIN_MUTATION,
        context=CommandContext.system(
            actor="system:pytest",
            scope="team-inbox:operator-command",
            reason="pytest phantom-event regression",
        ),
        operation=operation,
    )


def test_blocked_bulk_resolve_leaves_no_phantom_transition_event(db_session):
    """A blocked bulk resolve with no grant must write zero event rows.

    Before the fix, the event row was created and flushed BEFORE the gate
    check ran, so a caught-and-skipped blocked conversation still committed
    a real `InboxStatusTransitionEvent` falsely claiming a `resolved`
    transition -- even though `conversation.status` never changed.
    """

    conversation, _subscriber = _blocked_conversation(db_session)
    conversation_id = conversation.id
    db_session.commit()

    def operation():
        return team_inbox_operations.bulk_update_status(
            db_session,
            conversation_ids=[conversation_id],
            status_value="resolved",
            actor_person_id=None,
        )

    result = _run_as_owner_command(db_session, operation)

    assert result["updated"] == []
    assert len(result["blocked"]) == 1
    events = (
        db_session.query(InboxStatusTransitionEvent)
        .filter_by(conversation_id=conversation_id)
        .all()
    )
    assert events == []


def test_blocked_macro_run_leaves_no_phantom_transition_event(db_session):
    """A blocked macro run with no grant must write zero event rows."""

    conversation, _subscriber = _blocked_conversation(db_session)
    conversation_id = conversation.id
    macro = team_inbox_operations.create_macro(
        db_session,
        name="Resolve conversation",
        body_text="Resolved.",
        actions=[{"action_type": "set_status", "params": {"status": "resolved"}}],
    )
    macro_id = macro.id
    db_session.commit()

    def operation():
        current = db_session.get(InboxConversation, conversation_id)
        return team_inbox_operations.execute_macro_actions(
            db_session,
            conversation=current,
            macro_id=macro_id,
            actor_person_id=None,
        )

    result = _run_as_owner_command(db_session, operation)

    assert result["actions_failed"] == 1
    events = (
        db_session.query(InboxStatusTransitionEvent)
        .filter_by(conversation_id=conversation_id)
        .all()
    )
    assert events == []


def test_bulk_resolve_with_mismatched_grant_leaves_no_phantom_transition_event(
    db_session,
):
    """A grant-supplied bulk resolve that fails consumption must also write
    zero event rows -- the savepoint wrapping event-creation+consumption
    together is what protects this path (the no-grant path above never
    creates the event row at all).
    """

    conversation, _subscriber = _blocked_conversation(db_session)
    conversation_id = conversation.id
    other_conversation, _other = _blocked_conversation(db_session)
    grant = _issue(
        db_session, other_conversation
    )  # belongs to a DIFFERENT conversation
    db_session.commit()

    def operation():
        return team_inbox_operations.bulk_update_status(
            db_session,
            conversation_ids=[conversation_id],
            status_value="resolved",
            actor_person_id=None,
            override_grant_ids={conversation_id: grant.grant_id},
        )

    result = _run_as_owner_command(db_session, operation)

    assert result["updated"] == []
    assert len(result["blocked"]) == 1
    events = (
        db_session.query(InboxStatusTransitionEvent)
        .filter_by(conversation_id=conversation_id)
        .all()
    )
    assert events == []
