"""Typed field query for ERP-owned expense categories."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services.backoffice import ExpenseCategoryView, build_gateway
from app.services.domain_errors import DomainError
from app.services.dotmac_erp.client import DotMacERPError
from app.services.integrations.installations import InstallationError


@dataclass(frozen=True, slots=True)
class ListExpenseCategories:
    """Read the current authoritative ERP category list."""


class ExpenseCategoryQueryError(DomainError):
    pass


def list_expense_categories(
    db: Session, query: ListExpenseCategories
) -> tuple[ExpenseCategoryView, ...]:
    del query
    try:
        with build_gateway(db) as client:
            return client.get_expense_categories()
    except (InstallationError, DotMacERPError) as exc:
        raise ExpenseCategoryQueryError(
            code="operations.expense_categories.erp_unavailable",
            message="Expense categories are temporarily unavailable.",
        ) from exc
