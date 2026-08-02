"""Runtime transaction boundary for contracted public command owners.

Adapters own session lifecycle. A contracted service owner calls
``execute_owner_command`` once for one public command; the executor verifies
the architecture manifest, rejects caller-owned or nested transactions, and
commits or rolls back the complete business operation before returning.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import TypeVar
from uuid import UUID, uuid4

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.services.domain_errors import DomainError
from app.services.sot_manifest import (
    OwnerRole,
    TransactionMode,
    contract_validation_errors,
    owner_command_boundary_error_codes,
)

ResultT = TypeVar("ResultT")

_ACTIVE_COMMAND_KEY = "_dotmac_active_owner_command"
_ACTIVE_COMMAND_CONTEXT_KEY = "_dotmac_active_owner_command_context"
_BOUNDARY_COMMIT_KEY = "_dotmac_owner_boundary_commit"
_HELPER_ROLLBACK_KEY = "_dotmac_owner_helper_rollback"
_AUTHORIZED_SAVEPOINT_KEY = "_dotmac_authorized_owner_savepoint"
_OWNER_ROLES = {
    OwnerRole.AUTHORITATIVE_RECORD,
    OwnerRole.OBSERVATION_COLLECTOR,
    OwnerRole.COMMAND_WRITER,
    OwnerRole.RECONCILER,
    OwnerRole.PROJECTION_WRITER,
}


@dataclass(frozen=True)
class CommandContext:
    """Correlation and decision evidence carried by every public command."""

    command_id: UUID
    correlation_id: UUID
    actor: str
    scope: str
    reason: str
    idempotency_key: str | None = None
    causation_id: UUID | None = None

    @classmethod
    def system(
        cls,
        *,
        actor: str,
        scope: str,
        reason: str,
        command_id: UUID | None = None,
        correlation_id: UUID | None = None,
        idempotency_key: str | None = None,
        causation_id: UUID | None = None,
    ) -> CommandContext:
        """Build context for a scheduled or internal system command."""

        resolved_command_id = command_id or uuid4()
        return cls(
            command_id=resolved_command_id,
            correlation_id=correlation_id or resolved_command_id,
            actor=actor,
            scope=scope,
            reason=reason,
            idempotency_key=idempotency_key,
            causation_id=causation_id,
        )


@dataclass(frozen=True)
class OwnerCommandDefinition:
    """Static link from a public command to one exact manifest concern."""

    owner: str
    concern: str
    name: str


class OwnerCommandError(DomainError):
    """Fail-closed command-boundary or manifest-contract violation."""


def _error(
    definition: OwnerCommandDefinition,
    suffix: str,
    message: str,
    **details: object,
) -> OwnerCommandError:
    return OwnerCommandError(
        code=f"{definition.owner}.{suffix}",
        message=message,
        details={"command": definition.name, **details},
    )


def _validate_text(
    definition: OwnerCommandDefinition,
    *,
    field: str,
    value: str | None,
) -> None:
    if value is None or not value.strip():
        raise _error(
            definition,
            "invalid_command_context",
            f"Command {field} cannot be empty.",
            field=field,
        )


def _validate_context(
    definition: OwnerCommandDefinition, context: CommandContext
) -> None:
    for field, value in (
        ("command_id", context.command_id),
        ("correlation_id", context.correlation_id),
    ):
        if not isinstance(value, UUID):
            raise _error(
                definition,
                "invalid_command_context",
                f"Command {field} must be a UUID.",
                field=field,
            )
    if context.causation_id is not None and not isinstance(context.causation_id, UUID):
        raise _error(
            definition,
            "invalid_command_context",
            "Command causation_id must be a UUID when supplied.",
            field="causation_id",
        )
    _validate_text(definition, field="actor", value=context.actor)
    _validate_text(definition, field="scope", value=context.scope)
    _validate_text(definition, field="reason", value=context.reason)
    if context.idempotency_key is not None:
        _validate_text(
            definition,
            field="idempotency_key",
            value=context.idempotency_key,
        )


@cache
def _validate_manifest(definition: OwnerCommandDefinition) -> None:
    for field, value in (
        ("owner", definition.owner),
        ("concern", definition.concern),
        ("name", definition.name),
    ):
        _validate_text(definition, field=field, value=value)
    # Lazy import prevents the declarative registry from depending on its
    # runtime executor while it is being constructed.
    from app.services.sot_relationships import all_services, service_relationship

    try:
        service = service_relationship(definition.owner)
    except KeyError as exc:
        raise _error(
            definition,
            "command_contract_violation",
            "Command owner is absent from the architecture manifest.",
        ) from exc

    contract = service.contract
    if contract is None:
        raise _error(
            definition,
            "command_contract_violation",
            "Command owner has not completed its typed manifest contract.",
        )

    contract_errors = contract_validation_errors(
        service,
        service_names={item.name for item in all_services()},
    )
    if contract_errors:
        raise _error(
            definition,
            "command_contract_violation",
            "Command owner's typed manifest contract is invalid.",
            contract_error_count=len(contract_errors),
        )

    required_error_codes = set(owner_command_boundary_error_codes(definition.owner))
    missing_error_codes = required_error_codes - set(contract.errors.domain_codes)
    if missing_error_codes:
        raise _error(
            definition,
            "command_contract_violation",
            "Command owner has not declared every runtime boundary error.",
            missing_error_code_count=len(missing_error_codes),
        )

    concern = next(
        (item for item in contract.concerns if item.name == definition.concern),
        None,
    )
    if concern is None:
        raise _error(
            definition,
            "command_contract_violation",
            "Command concern is absent from the owner's typed contract.",
            concern=definition.concern,
        )

    expected_mode = (
        TransactionMode.COORDINATOR_MANAGED
        if concern.role is OwnerRole.APPLICATION_COORDINATOR
        else TransactionMode.OWNER_MANAGED
    )
    if concern.role not in _OWNER_ROLES | {OwnerRole.APPLICATION_COORDINATOR}:
        raise _error(
            definition,
            "command_contract_violation",
            "Manifest concern does not grant command transaction ownership.",
            concern=definition.concern,
            role=concern.role.value,
        )
    if contract.transaction.mode is not expected_mode:
        raise _error(
            definition,
            "command_contract_violation",
            "Manifest transaction mode does not match the command owner role.",
            concern=definition.concern,
            mode=contract.transaction.mode.value,
        )


@event.listens_for(Session, "before_commit")
def _reject_helper_commit(session: Session) -> None:
    definition = session.info.get(_ACTIVE_COMMAND_KEY)
    if definition is None or session.info.get(_BOUNDARY_COMMIT_KEY):
        return
    authorized_savepoint = session.info.get(_AUTHORIZED_SAVEPOINT_KEY)
    if (
        authorized_savepoint is not None
        and session.get_nested_transaction() is authorized_savepoint
    ):
        return
    raise _error(
        definition,
        "nested_transaction_completion",
        "Only the public command boundary may commit its transaction.",
    )


@event.listens_for(Session, "after_soft_rollback")
def _record_helper_rollback(session: Session, previous_transaction: object) -> None:
    if session.info.get(_AUTHORIZED_SAVEPOINT_KEY) is previous_transaction:
        return
    if session.info.get(_ACTIVE_COMMAND_KEY) is not None:
        session.info[_HELPER_ROLLBACK_KEY] = True


def execute_owner_savepoint(
    db: Session,
    operation: Callable[[], ResultT],
) -> ResultT:
    """Isolate one explicitly optional step inside an active owner command.

    The helper alone completes its savepoint. The participant callback remains
    transaction-neutral, and direct helper commit/rollback calls still fail
    closed under the surrounding owner command.
    """

    definition = db.info.get(_ACTIVE_COMMAND_KEY)
    if definition is None:
        raise RuntimeError("Owner savepoints require an active owner command")
    if db.in_nested_transaction():
        raise _error(
            definition,
            "nested_transaction_completion",
            "Owner savepoints cannot be nested.",
        )

    savepoint = db.begin_nested()
    try:
        result = operation()
        if db.get_nested_transaction() is not savepoint or not savepoint.is_active:
            raise _error(
                definition,
                "nested_transaction_completion",
                "A command helper completed the authorized owner savepoint.",
            )
        db.info[_AUTHORIZED_SAVEPOINT_KEY] = savepoint
        savepoint.commit()
        return result
    except BaseException:
        if savepoint.is_active:
            db.info[_AUTHORIZED_SAVEPOINT_KEY] = savepoint
            savepoint.rollback()
        raise
    finally:
        db.info.pop(_AUTHORIZED_SAVEPOINT_KEY, None)


def current_command_context(db: Session) -> CommandContext:
    """Return the active owner command's context for participant provenance.

    Participants staging typed records inside a host command (e.g. subledger
    posting groups) inherit the command identity from here instead of
    threading context through every call frame. Fails closed outside a
    command.
    """

    context = db.info.get(_ACTIVE_COMMAND_CONTEXT_KEY)
    if context is None:
        raise OwnerCommandError(
            code="owner_command.no_active_context",
            message="No active owner command context on this session.",
        )
    return context


def owner_command_active(db: Session, *, owner: str | None = None) -> bool:
    """Return whether ``db`` is inside the requested public owner command.

    Passing an owner prevents a participant owned by another service from
    treating the current transaction as its own command boundary.
    """

    definition = db.info.get(_ACTIVE_COMMAND_KEY)
    if definition is None:
        return False
    return owner is None or definition.owner == owner


def execute_owner_command(
    db: Session,
    *,
    definition: OwnerCommandDefinition,
    context: CommandContext,
    operation: Callable[[], ResultT],
) -> ResultT:
    """Run one contracted command in a new, complete root transaction.

    The session must be transaction-free at entry. Success is committed before
    this function returns. Any exception rolls the owned transaction back.
    Nested public commands and helper-level transaction completion fail closed.
    """

    if db.info.get(_ACTIVE_COMMAND_KEY) is not None:
        raise _error(
            definition,
            "nested_owner_command",
            "A public owner command cannot run inside another owner command.",
        )
    if db.in_transaction():
        # A public adapter must not pass pending reads or writes into an owner
        # command. Clear the invalid caller transaction so domain-error mapping
        # never receives a poisoned or ambiguous session.
        db.rollback()
        raise _error(
            definition,
            "active_caller_transaction",
            "Owner command requires a transaction-free session at entry.",
        )

    _validate_context(definition, context)
    _validate_manifest(definition)

    bind = db.get_bind()
    # Test harnesses and explicit integration coordinators may bind a Session
    # to an already-open Connection transaction. Guard this command with a
    # connection savepoint so a command rollback cannot erase an earlier,
    # successfully completed command in that external lifecycle boundary.
    external_guard = (
        bind.begin_nested()
        if isinstance(bind, Connection) and bind.in_transaction()
        else None
    )
    transaction = db.begin()
    db.info[_ACTIVE_COMMAND_KEY] = definition
    db.info[_ACTIVE_COMMAND_CONTEXT_KEY] = context
    try:
        result = operation()
        if db.info.get(_HELPER_ROLLBACK_KEY):
            raise _error(
                definition,
                "nested_transaction_completion",
                "Only the public command boundary may roll back its transaction.",
            )
        if db.in_nested_transaction():
            raise _error(
                definition,
                "nested_transaction_completion",
                "Command returned with an open nested transaction.",
            )
        if db.get_transaction() is not transaction or not transaction.is_active:
            raise _error(
                definition,
                "nested_transaction_completion",
                "A command helper completed the owner transaction.",
            )

        db.info[_BOUNDARY_COMMIT_KEY] = True
        transaction.commit()
        if external_guard is not None and external_guard.is_active:
            external_guard.commit()
        return result
    except BaseException:
        if transaction.is_active:
            transaction.rollback()
        elif db.in_transaction():
            db.rollback()
        if external_guard is not None and external_guard.is_active:
            external_guard.rollback()
        raise
    finally:
        db.info.pop(_BOUNDARY_COMMIT_KEY, None)
        db.info.pop(_HELPER_ROLLBACK_KEY, None)
        db.info.pop(_AUTHORIZED_SAVEPOINT_KEY, None)
        db.info.pop(_ACTIVE_COMMAND_KEY, None)
        db.info.pop(_ACTIVE_COMMAND_CONTEXT_KEY, None)
