"""Non-vacuous proof of the issuance/resolution CLI's real behavior.

Mirrors ``tests/test_reconcile_prepaid_drafts_cli.py``'s rationale: a
source-grep architecture test can confirm the CLI *calls* the RBAC resolver,
but it cannot prove the resolver ever actually returns ``True`` for a real
principal, nor that ``--apply``/``--resolve`` actually complete against a
real session. An earlier version of ``_run_apply`` read
``conversation.id`` off the ORM conversation object AFTER
``db_session_adapter.release_read_transaction`` committed it -- since the
session's ``SessionLocal`` uses the default ``expire_on_commit=True``, that
touch re-triggered a refresh SELECT, silently reopening a transaction that
``execute_owner_command`` then rejected as an active caller transaction.
``_run_apply``/``_run_resolve`` are exercised directly here (bypassing
``argparse``/``db_session_adapter.owner_command_session``'s own
``SessionLocal()``) so they run against the exact transaction/session
semantics ``db_session`` provides -- the same shape that caught the bug.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.models.party import Party, PartyType
from app.models.rbac import Permission, Role, RolePermission, SystemUserRole
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxCustomerCompletionPolicyVersion,
)
from app.services.team_inbox_completion_override import OWNER
from scripts.support import issue_inbox_completion_override as cli


def _policy(db_session):
    existing = db_session.query(InboxCustomerCompletionPolicyVersion).first()
    if existing is not None:
        return existing
    policy = InboxCustomerCompletionPolicyVersion(
        version=1,
        required_fields=["name", "phone", "address"],
        decision_source="pytest",
    )
    db_session.add(policy)
    db_session.flush()
    return policy


def _blocked_conversation(db_session) -> InboxConversation:
    policy = _policy(db_session)
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Lovelace",
        display_name="Ada Lovelace",
        email=f"{uuid4().hex}@example.test",
        phone=None,
        address_line1=None,
    )
    db_session.add(subscriber)
    db_session.flush()
    conversation = InboxConversation(
        subscriber_id=subscriber.id,
        customer_completion_policy_version_id=policy.id,
        channel_type="whatsapp",
        status="open",
        is_active=True,
        completion_gate_precutover_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _admin_system_user(db_session, *, bind_party: bool = False) -> SystemUser:
    user = SystemUser(
        id=uuid4(),
        first_name="Test",
        last_name="Staff",
        display_name="Test Staff",
        email=f"staff-{uuid4().hex}@example.test",
        is_active=True,
    )
    if bind_party:
        party = Party(party_type=PartyType.person.value, display_name="Test Staff")
        db_session.add(party)
        db_session.flush()
        user.person_party_id = party.id
        user.party_bound_at = datetime.now(UTC)
        user.party_binding_source = "pytest"
        user.party_binding_reason = "Explicit staff Party binding fixture"
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(name="admin").first()
    if role is None:
        role = Role(name="admin", is_active=True)
        db_session.add(role)
        db_session.flush()
    permission = (
        db_session.query(Permission).filter_by(key=cli.OVERRIDE_GRANT_SCOPE).first()
    )
    if permission is None:
        permission = Permission(key=cli.OVERRIDE_GRANT_SCOPE, is_active=True)
        db_session.add(permission)
        db_session.flush()
    role_permission = (
        db_session.query(RolePermission)
        .filter_by(role_id=role.id, permission_id=permission.id)
        .first()
    )
    if role_permission is None:
        db_session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.flush()
    return user


# --- RBAC resolver: proves the real join, not the dead attribute path ------


def test_resolve_permission_granted_true_for_directly_granted_permission(db_session):
    user = _admin_system_user(db_session)

    assert (
        cli._resolve_permission_granted(db_session, actor_system_user_id=user.id)
        is True
    )


def test_resolve_permission_granted_false_for_unknown_user(db_session):
    assert (
        cli._resolve_permission_granted(db_session, actor_system_user_id=uuid4())
        is False
    )


def test_resolve_permission_granted_false_for_deactivated_staff(db_session):
    user = _admin_system_user(db_session)
    user.is_active = False
    db_session.flush()

    assert (
        cli._resolve_permission_granted(db_session, actor_system_user_id=user.id)
        is False
    )


def test_resolve_permission_granted_false_without_identifier(db_session):
    assert (
        cli._resolve_permission_granted(db_session, actor_system_user_id=None) is False
    )


# --- SystemUser -> Party bridge ---------------------------------------


def test_resolve_actor_person_id_returns_bound_party(db_session):
    user = _admin_system_user(db_session, bind_party=True)

    assert (
        cli._resolve_actor_person_id(db_session, actor_system_user_id=user.id)
        == user.person_party_id
    )


def test_resolve_actor_person_id_none_without_party_binding(db_session):
    user = _admin_system_user(db_session, bind_party=False)

    assert (
        cli._resolve_actor_person_id(db_session, actor_system_user_id=user.id) is None
    )


# --- _run_apply: the real transaction-boundary regression test -------------


def test_run_apply_succeeds_across_the_release_read_transaction_commit(db_session):
    """Reproduces the exact bug: a commit happens mid-``_run_apply``, then a
    conversation identifier is used again afterward. This must not reopen an
    implicit transaction and must not raise
    ``active_caller_transaction``.
    """

    conversation = _blocked_conversation(db_session)
    conversation_id = conversation.id
    user = _admin_system_user(db_session)

    # No manual commit here on purpose: `_run_apply` itself is responsible
    # for releasing the read transaction before entering
    # `issue_override_grant`'s owner-command boundary -- that is exactly the
    # behavior under test.
    result = cli._run_apply(
        db_session,
        conversation_id=conversation_id,
        reason_code="legacy_conversation_pre_cutover_review",
        reason="Reviewed ahead of the completion-policy cutover.",
        actor="admin@example.test",
        actor_system_user_id=user.id,
        idempotency_key=str(uuid4()),
    )

    assert "error" not in result, result
    assert result["conversation_id"] == str(conversation_id)
    assert result["state"] == "pending"
    assert result["already_issued"] is False


def test_run_apply_refuses_without_real_permission_grant(db_session):
    conversation = _blocked_conversation(db_session)
    conversation_id = conversation.id
    unprivileged_user = SystemUser(
        id=uuid4(),
        first_name="No",
        last_name="Access",
        display_name="No Access",
        email=f"noaccess-{uuid4().hex}@example.test",
        is_active=True,
    )
    db_session.add(unprivileged_user)
    db_session.flush()

    result = cli._run_apply(
        db_session,
        conversation_id=conversation_id,
        reason_code="legacy_conversation_pre_cutover_review",
        reason="Reviewed ahead of the completion-policy cutover.",
        actor="admin@example.test",
        actor_system_user_id=unprivileged_user.id,
        idempotency_key=str(uuid4()),
    )

    # `_run_apply` delegates to `issue_override_grant`, whose `DomainError`
    # codes are owner-prefixed (`f"{OWNER}.{suffix}"`, matching this
    # codebase's convention -- see `prepaid_draft_reconciliation`'s
    # `REPAIR_SCOPE`/`_error` pair) rather than prefixed with the RBAC
    # permission-scope string itself. `_run_resolve`'s own out-of-band
    # permission check (not exercised by this test) is the one place that
    # legitimately returns an `OVERRIDE_GRANT_SCOPE`-prefixed error, because
    # it never enters `issue_override_grant`.
    assert result["error"] == f"{OWNER}.permission_denied"


# --- _run_resolve: closes the loop from grant to an actual resolution ------


def test_run_resolve_actually_transitions_the_conversation(db_session):
    conversation = _blocked_conversation(db_session)
    conversation_id = conversation.id
    user = _admin_system_user(db_session, bind_party=True)

    issued = cli._run_apply(
        db_session,
        conversation_id=conversation_id,
        reason_code="legacy_conversation_pre_cutover_review",
        reason="Reviewed ahead of the completion-policy cutover.",
        actor="admin@example.test",
        actor_system_user_id=user.id,
        idempotency_key=str(uuid4()),
    )
    assert "error" not in issued, issued

    resolved = cli._run_resolve(
        db_session,
        conversation_id=conversation_id,
        grant_id=UUID(issued["grant_id"]),
        actor_system_user_id=user.id,
    )

    assert "error" not in resolved, resolved
    assert resolved["status"] == InboxConversationStatus.resolved.value
    row = db_session.get(InboxConversation, conversation_id)
    assert row.status == InboxConversationStatus.resolved.value


def test_run_resolve_refuses_without_a_bound_party(db_session):
    conversation = _blocked_conversation(db_session)
    conversation_id = conversation.id
    user = _admin_system_user(db_session, bind_party=False)

    issued = cli._run_apply(
        db_session,
        conversation_id=conversation_id,
        reason_code="legacy_conversation_pre_cutover_review",
        reason="Reviewed ahead of the completion-policy cutover.",
        actor="admin@example.test",
        actor_system_user_id=user.id,
        idempotency_key=str(uuid4()),
    )
    assert "error" not in issued, issued

    resolved = cli._run_resolve(
        db_session,
        conversation_id=conversation_id,
        grant_id=UUID(issued["grant_id"]),
        actor_system_user_id=user.id,
    )

    assert resolved["error"] == "actor_system_user_has_no_party_binding"
