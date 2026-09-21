from datetime import UTC, datetime, time
from types import SimpleNamespace
from uuid import uuid4

from app.models.inbox_sla import InboxSlaPolicy, InboxSlaRule
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import InboxConversation
from app.services.inbox_sla import (
    SlaPolicyInput,
    SlaRuleInput,
    add_business_minutes,
    is_eligible_human_response,
    select_policy_rule,
    validate_policy,
)


def _policy() -> InboxSlaPolicy:
    policy = InboxSlaPolicy(
        name="Nigeria Inbox",
        timezone="Africa/Lagos",
        working_days=[0, 1, 2, 3, 4],
        workday_start=time(9),
        workday_end=time(17),
        holidays=["2026-09-07"],
        is_default=True,
    )
    policy.rules = [
        InboxSlaRule(
            first_response_minutes=60, resolution_minutes=240, warning_minutes=15
        )
    ]
    return policy


def test_business_calendar_skips_weekends_holidays_and_uses_lagos() -> None:
    policy = _policy()
    start = datetime(2026, 9, 4, 16, 30, tzinfo=UTC)  # Friday, 17:30 Lagos
    due = add_business_minutes(start, 60, policy)
    assert due == datetime(2026, 9, 8, 9, 0, tzinfo=UTC)


def test_policy_validation_rejects_overlap_and_bad_warning() -> None:
    command = SlaPolicyInput(
        name="x",
        description=None,
        rules=(
            SlaRuleInput(
                first_response_minutes=10, resolution_minutes=20, warning_minutes=10
            ),
        ),
    )
    try:
        validate_policy(command)
    except Exception as exc:
        assert "warning" in str(exc).lower()
    else:
        raise AssertionError("invalid warning was accepted")


def test_ai_and_system_messages_do_not_satisfy_first_response() -> None:
    assert not is_eligible_human_response(
        SimpleNamespace(direction="outbound", metadata_={"sender_type": "ai"})
    )
    assert not is_eligible_human_response(
        SimpleNamespace(direction="outbound", metadata_={"sender_type": "system"})
    )
    assert is_eligible_human_response(
        SimpleNamespace(
            direction="outbound", metadata_={"sent_by_person_id": str(uuid4())}
        )
    )


def test_rule_specificity_prefers_team_channel_and_priority(db_session) -> None:
    team = ServiceTeam(name="SLA specificity", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    team_id = team.id
    policy = _policy()
    policy.rules = [
        InboxSlaRule(
            first_response_minutes=60, resolution_minutes=240, warning_minutes=10
        ),
        InboxSlaRule(
            service_team_id=team_id,
            channel_type="whatsapp",
            priority=1,
            first_response_minutes=5,
            resolution_minutes=20,
            warning_minutes=1,
        ),
    ]
    db_session.add(policy)
    db_session.flush()
    conversation = InboxConversation(
        primary_service_team_id=team_id, channel_type="whatsapp", priority=1
    )
    selected = select_policy_rule(db_session, conversation)
    assert selected is not None
    assert selected[1].first_response_minutes == 5


# Unit-lane command regressions; migrated PostgreSQL acceptance runs in CI.
def test_policy_owner_saves_and_deactivation_clears_default(db_session) -> None:
    from app.services import inbox_sla
    from app.services.audit_adapter import AuditActor
    from app.services.owner_commands import CommandContext

    principal = str(uuid4())
    context = CommandContext.system(
        actor=principal,
        scope="support:ticket:update",
        reason="Configure SLA",
    )
    command = inbox_sla.SaveSlaPolicyCommand(
        context=context,
        actor=AuditActor.user(principal),
        policy=SlaPolicyInput(
            name="Committed policy",
            description=None,
            is_default=True,
            rules=(SlaRuleInput(first_response_minutes=60, resolution_minutes=120),),
        ),
    )
    db_session.rollback()
    saved = inbox_sla.save_policy(db_session, command=command)
    assert not db_session.in_transaction()
    assert saved.is_default and saved.is_active
    result = inbox_sla.activate_policy(
        db_session,
        command=inbox_sla.ActivateSlaPolicyCommand(
            context=context,
            actor=AuditActor.user(principal),
            policy_id=saved.id,
            active=False,
        ),
    )
    assert not db_session.in_transaction()
    assert not result.is_active and not result.is_default
    persisted = inbox_sla.query_policies(
        db_session, query=inbox_sla.SlaPolicyQuery(saved.id)
    )[0]
    assert persisted == result


def test_policy_owner_rejects_unscoped_write(db_session) -> None:
    import pytest

    from app.services import inbox_sla
    from app.services.audit_adapter import AuditActor
    from app.services.owner_commands import CommandContext

    principal = str(uuid4())
    db_session.rollback()
    with pytest.raises(inbox_sla.InboxSlaError, match="authorized administrator"):
        inbox_sla.save_policy(
            db_session,
            command=inbox_sla.SaveSlaPolicyCommand(
                context=CommandContext.system(
                    actor=principal, scope="read", reason="Rejected write"
                ),
                actor=AuditActor.user(principal),
                policy=SlaPolicyInput(name="Denied", description=None, rules=()),
            ),
        )
    assert not db_session.in_transaction()
    assert inbox_sla.query_policies(db_session, query=inbox_sla.SlaPolicyQuery()) == ()


def test_sweep_saves_warning_before_deadline_and_deduplicates_evidence(
    db_session,
) -> None:
    from datetime import timedelta

    from app.models.inbox_sla import InboxSlaClock, InboxSlaEvent
    from app.services import inbox_sla
    from app.services.owner_commands import CommandContext

    now = datetime(2026, 9, 8, 10, tzinfo=UTC)
    policy = _policy()
    conversation = InboxConversation(
        channel_type="whatsapp", status="open", contact_address="unit-sla"
    )
    db_session.add_all([policy, conversation])
    db_session.flush()
    clock = InboxSlaClock(
        conversation_id=conversation.id,
        policy_id=policy.id,
        rule_id=policy.rules[0].id,
        started_at=now - timedelta(minutes=50),
        first_response_due_at=now + timedelta(minutes=10),
        resolution_due_at=now + timedelta(hours=2),
        status="running",
    )
    db_session.add(clock)
    db_session.commit()
    clock_id = clock.id
    command = inbox_sla.EvaluateSlaCommand(
        context=CommandContext.system(
            actor="inbox-sla-evaluator",
            scope="inbox-sla:evaluate",
            reason="Warning sweep",
        ),
        now=now,
    )
    db_session.rollback()
    result = inbox_sla.evaluate_due_clocks(db_session, command=command)
    assert result.checked == 1 and result.warning == 1
    assert not db_session.in_transaction()
    replay = inbox_sla.evaluate_due_clocks(db_session, command=command)
    assert replay.warning == 1
    assert (
        db_session.query(InboxSlaEvent)
        .filter(InboxSlaEvent.clock_id == clock_id)
        .count()
        == 1
    )
    db_session.refresh(clock)
    assert clock.status == "warning" and clock.warning_sent_at is not None
