"""Owned customer-specific withholding-tax policy facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.customer_tax_policy import CustomerTaxPolicy
from app.models.subscriber import Subscriber
from app.services.domain_errors import DomainError
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

WRITE_SCOPE = "customer-tax-policy:write"

_SET_POLICY_COMMAND = OwnerCommandDefinition(
    owner="financial.customer_tax_policies",
    concern="customer withholding-tax eligibility policy",
    name="set_customer_withholding_tax_policy",
)


class CustomerTaxPolicyError(DomainError, ValueError):
    """Stable customer tax-policy rejection."""


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> CustomerTaxPolicyError:
    return CustomerTaxPolicyError(
        code=f"financial.customer_tax_policies.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class SetCustomerWithholdingTaxPolicyCommand:
    account_id: UUID
    withholding_tax_enabled: bool
    updated_by: str


@dataclass(frozen=True, slots=True)
class CustomerWithholdingTaxPolicy:
    account_id: UUID
    withholding_tax_enabled: bool
    version: int
    updated_by: str | None
    updated_at: datetime | None


def get_customer_withholding_tax_policy(
    db: Session,
    *,
    account_id: UUID,
) -> CustomerWithholdingTaxPolicy:
    row = (
        db.query(CustomerTaxPolicy)
        .filter(CustomerTaxPolicy.account_id == account_id)
        .first()
    )
    if row is None:
        return CustomerWithholdingTaxPolicy(
            account_id=account_id,
            withholding_tax_enabled=False,
            version=0,
            updated_by=None,
            updated_at=None,
        )
    return CustomerWithholdingTaxPolicy(
        account_id=row.account_id,
        withholding_tax_enabled=bool(row.withholding_tax_enabled),
        version=int(row.version),
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


def set_customer_withholding_tax_policy(
    db: Session,
    command: SetCustomerWithholdingTaxPolicyCommand,
    *,
    context: CommandContext,
) -> CustomerWithholdingTaxPolicy:
    return execute_owner_command(
        db,
        definition=_SET_POLICY_COMMAND,
        context=context,
        operation=lambda: _set_customer_withholding_tax_policy(
            db,
            command=command,
        ),
    )


def _set_customer_withholding_tax_policy(
    db: Session,
    *,
    command: SetCustomerWithholdingTaxPolicyCommand,
) -> CustomerWithholdingTaxPolicy:
    actor = str(command.updated_by or "").strip()
    if not actor:
        raise _error("actor_required", "Customer tax-policy actor is required")

    account = lock_for_update(db, Subscriber, command.account_id)
    if account is None:
        raise _error(
            "account_not_found",
            "Customer account was not found",
            account_id=str(command.account_id),
        )

    row = (
        db.query(CustomerTaxPolicy)
        .filter(CustomerTaxPolicy.account_id == command.account_id)
        .with_for_update()
        .first()
    )
    if row is None:
        row = CustomerTaxPolicy(
            account_id=command.account_id,
            withholding_tax_enabled=bool(command.withholding_tax_enabled),
            version=1,
            updated_by=actor,
        )
        db.add(row)
        db.flush()
    elif bool(row.withholding_tax_enabled) != bool(command.withholding_tax_enabled):
        row.withholding_tax_enabled = bool(command.withholding_tax_enabled)
        row.version = int(row.version) + 1
        row.updated_by = actor
        db.flush()

    return CustomerWithholdingTaxPolicy(
        account_id=row.account_id,
        withholding_tax_enabled=bool(row.withholding_tax_enabled),
        version=int(row.version),
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )
