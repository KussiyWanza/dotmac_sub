#!/usr/bin/env python
"""Issue, and separately spend, one legacy Team Inbox completion-gate override.

Preview (the default) writes nothing: it prints the exact live
missing-fields/canonical-values gap for one conversation, which is exactly
the evidence a reviewing operator must supply back via ``--apply`` (the
issuance owner refuses a stale review). ``--apply`` issues a single-use
grant; it does NOT resolve anything by itself. ``--resolve`` (with
``--grant-id``) is the second, separate step that actually spends an issued
grant to transition the conversation to ``resolved`` -- see
``app/services/team_inbox_completion_override.py`` and
``docs/designs/INBOX_CUSTOMER_COMPLETION_GATE.md``.

This is the ONLY way to issue or spend a grant today (Phase 1): there is no
admin-portal route wired to either capability, by design -- the immediate
operational need is a small, individually-reviewed population (two
currently-open legacy conversations for one subscriber), for which a CLI a
staff member with real admin access runs is sufficient.
"""

from __future__ import annotations

import argparse
import json
from uuid import UUID

from app.models.system_user import SystemUser
from app.models.team_inbox import InboxConversation
from app.services import team_inbox_commands
from app.services.auth_dependencies import has_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.system_user_assignments import system_user_role_names
from app.services.team_inbox_completion_override import (
    OVERRIDE_GRANT_SCOPE,
    IssueCompletionOverrideCommand,
    compute_live_evidence,
    issue_override_grant,
)


def _resolve_system_user(db, actor_system_user_id: UUID | None) -> SystemUser | None:
    if actor_system_user_id is None:
        return None
    system_user = db.get(SystemUser, actor_system_user_id)
    if system_user is None or not system_user.is_active:
        return None
    return system_user


def _resolve_permission_granted(db, *, actor_system_user_id: UUID | None) -> bool:
    """Check a real staff principal's granted roles, never a free-text actor.

    ``--actor`` is only an audit label; it proves nothing about who is really
    running this script. This resolves the operator-supplied staff identifier
    against its actual RBAC grants via ``system_user_role_names`` -- the real
    ``Role`` join over ``SystemUserRole`` -- and the same ``has_permission``
    mechanism the admin web routes use, before the owner is allowed to treat
    the action as authorized. A deactivated staff account never resolves.
    Deliberately NOT ``auth_dependencies.user_role_names``, which reads a
    ``SystemUser.roles`` relationship that does not reflect real role
    assignments.
    """

    system_user = _resolve_system_user(db, actor_system_user_id)
    if system_user is None or actor_system_user_id is None:
        return False
    roles = system_user_role_names(db, actor_system_user_id)
    auth = {
        "principal_id": str(actor_system_user_id),
        "principal_type": "system_user",
        "roles": set(roles),
    }
    return has_permission(auth, db, OVERRIDE_GRANT_SCOPE)


def _resolve_actor_person_id(db, *, actor_system_user_id: UUID | None) -> UUID | None:
    """Bridge a SystemUser (RBAC principal) to its bound Party identity.

    ``team_inbox_commands.update_status``'s ``actor_person_id`` is a Party
    identity -- a different identity system from the SystemUser-based RBAC
    principal used for the permission check above. ``SystemUser
    .person_party_id`` is the existing, established bridge between the two:
    the same column is read directly the same way by
    ``app.services.team_inbox_commands._notify_internal_note_mentions``
    (``user.person_party_id == actor_person_id``) and by
    ``app.services.credential_party_binding``. There is no separate
    lookup/reconciliation function in this codebase; every caller reads the
    column directly, so this does the same rather than inventing one.
    """

    system_user = _resolve_system_user(db, actor_system_user_id)
    return system_user.person_party_id if system_user is not None else None


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from exc


def _run_preview(db, *, conversation_id: UUID) -> dict[str, object]:
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None:
        return {
            "error": "conversation_not_found",
            "message": f"Conversation {conversation_id} was not found.",
        }
    missing_fields, canonical_values_digest = compute_live_evidence(db, conversation)
    return {
        "dry_run": True,
        "conversation_id": str(conversation_id),
        "completion_gate_precutover_at": (
            conversation.completion_gate_precutover_at.isoformat()
            if conversation.completion_gate_precutover_at
            else None
        ),
        "status": conversation.status,
        "missing_fields": list(missing_fields),
        "canonical_values_digest": canonical_values_digest,
    }


def _run_apply(
    db,
    *,
    conversation_id: UUID,
    reason_code: str,
    reason: str,
    actor: str,
    actor_system_user_id: UUID,
    idempotency_key: str,
) -> dict[str, object]:
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None:
        return {
            "error": "conversation_not_found",
            "message": f"Conversation {conversation_id} was not found.",
        }
    missing_fields, canonical_values_digest = compute_live_evidence(db, conversation)

    # Resolve the real permission before entering the owner command
    # boundary: a raw SELECT would otherwise leave this session
    # mid-transaction, and execute_owner_command requires a
    # transaction-free session at entry.
    permission_granted = _resolve_permission_granted(
        db, actor_system_user_id=actor_system_user_id
    )
    db_session_adapter.release_read_transaction(db)

    context = CommandContext.system(
        actor=actor,
        scope=OVERRIDE_GRANT_SCOPE,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    try:
        outcome = issue_override_grant(
            db,
            IssueCompletionOverrideCommand(
                context=context,
                # Use the plain UUID the caller already has -- NEVER read
                # `conversation.id` back off the ORM object here. The
                # `release_read_transaction` commit above expires it
                # (`SessionLocal` uses the default `expire_on_commit=True`),
                # so touching any of its attributes triggers a refresh
                # SELECT that reopens a transaction, which
                # `execute_owner_command` then rejects as an active caller
                # transaction. This is exactly the bug this comment guards
                # against regressing to.
                conversation_id=conversation_id,
                reason_code=reason_code,
                expected_missing_fields=missing_fields,
                expected_canonical_values_digest=canonical_values_digest,
                permission_granted=permission_granted,
                actor_system_user_id=actor_system_user_id,
            ),
        )
    except DomainError as exc:
        return {"error": exc.code, "message": exc.message, "details": exc.details}
    return {
        "grant_id": str(outcome.grant_id),
        "conversation_id": str(outcome.conversation_id),
        "state": outcome.state,
        "granted_at": outcome.granted_at.isoformat(),
        "expires_at": outcome.expires_at.isoformat(),
        "already_issued": outcome.already_issued,
    }


def _run_resolve(
    db,
    *,
    conversation_id: UUID,
    grant_id: UUID,
    actor_system_user_id: UUID,
) -> dict[str, object]:
    """Spend an already-issued grant to actually resolve the conversation.

    This is what closes the loop from ``issue_override_grant`` (which only
    issues a grant -- nothing else in ``app/`` threads a grant id through to
    an actual resolution) to a real ``resolved`` transition. Requires the
    SAME ``support:inbox:completion_override`` permission as issuance:
    there is no separate "spend a grant" permission -- issuance and this
    direct-CLI consumption stay under one scope, since only an operator
    trusted to grant an override should also be trusted to spend one
    out-of-band this way.
    """

    permission_granted = _resolve_permission_granted(
        db, actor_system_user_id=actor_system_user_id
    )
    actor_person_id = _resolve_actor_person_id(
        db, actor_system_user_id=actor_system_user_id
    )
    db_session_adapter.release_read_transaction(db)

    if not permission_granted:
        return {
            "error": f"{OVERRIDE_GRANT_SCOPE}.permission_denied",
            "message": f"Resolving via an override grant requires {OVERRIDE_GRANT_SCOPE}.",
        }
    if actor_person_id is None:
        return {
            "error": "actor_system_user_has_no_party_binding",
            "message": (
                "The acting staff account has no bound Party identity "
                "(SystemUser.person_party_id is null); it cannot be "
                "recorded as the resolving actor."
            ),
        }

    try:
        outcome = team_inbox_commands.update_status(
            db,
            conversation_id=conversation_id,
            status_value="resolved",
            actor_person_id=actor_person_id,
            completion_override_grant_id=grant_id,
        )
    except DomainError as exc:
        return {"error": exc.code, "message": exc.message, "details": exc.details}
    return {
        "conversation_id": outcome.conversation_id,
        "status": outcome.status,
        "already_set": outcome.already_set,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversation-id", type=_uuid, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="Issue a single-use override grant."
    )
    mode.add_argument(
        "--resolve",
        action="store_true",
        help="Spend an already-issued grant to resolve the conversation.",
    )
    parser.add_argument("--grant-id", type=_uuid, help="Required with --resolve.")
    parser.add_argument("--reason-code")
    parser.add_argument("--reason")
    parser.add_argument("--actor")
    parser.add_argument("--actor-system-user-id", type=_uuid)
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()

    if args.apply:
        required = [
            ("--reason-code", args.reason_code),
            ("--reason", args.reason),
            ("--actor", args.actor),
            ("--actor-system-user-id", args.actor_system_user_id),
            ("--idempotency-key", args.idempotency_key),
        ]
        missing = [name for name, value in required if not value]
        if missing:
            parser.error("--apply requires " + ", ".join(missing))
    if args.resolve:
        required = [
            ("--grant-id", args.grant_id),
            ("--actor-system-user-id", args.actor_system_user_id),
        ]
        missing = [name for name, value in required if not value]
        if missing:
            parser.error("--resolve requires " + ", ".join(missing))

    with db_session_adapter.owner_command_session() as db:
        if args.resolve:
            payload = _run_resolve(
                db,
                conversation_id=args.conversation_id,
                grant_id=args.grant_id,
                actor_system_user_id=args.actor_system_user_id,
            )
        elif args.apply:
            payload = _run_apply(
                db,
                conversation_id=args.conversation_id,
                reason_code=args.reason_code,
                reason=args.reason,
                actor=args.actor,
                actor_system_user_id=args.actor_system_user_id,
                idempotency_key=args.idempotency_key,
            )
        else:
            payload = _run_preview(db, conversation_id=args.conversation_id)

    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 1 if "error" in payload else 0


if __name__ == "__main__":
    raise SystemExit(main())
