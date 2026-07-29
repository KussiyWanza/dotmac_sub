"""Collections services package.

This package provides services for managing collections and dunning processes:
- Dunning cases and workflows
- Account actions (suspend/restore)

The package supports both modular imports (from subpackages) and direct imports
for backwards compatibility.
"""

# Re-export from core module
from app.services.collections._core import (
    BillingEnforcementReconciler,
    DunningActionLogs,
    # Classes
    DunningCases,
    DunningWorkflow,
    FinancialAccessConsequencePreview,
    FinancialAccessConsequenceResult,
    billing_enforcement_reconciler,
    confirm_financial_access_consequence,
    confirm_financial_access_restoration,
    dunning_action_logs,
    # Service instances
    dunning_cases,
    dunning_workflow,
    get_available_balance,
    has_overdue_balance,
    overdue_receivable_snapshot,
    preview_financial_access_consequence,
    preview_financial_access_restoration,
    # Public functions
    restore_account_services,
    restore_account_services_detailed,
)

__all__ = [
    # Classes
    "DunningCases",
    "DunningActionLogs",
    "BillingEnforcementReconciler",
    "DunningWorkflow",
    "FinancialAccessConsequencePreview",
    "FinancialAccessConsequenceResult",
    # Service instances
    "dunning_cases",
    "dunning_action_logs",
    "billing_enforcement_reconciler",
    "dunning_workflow",
    # Public functions
    "get_available_balance",
    "has_overdue_balance",
    "overdue_receivable_snapshot",
    "preview_financial_access_consequence",
    "confirm_financial_access_consequence",
    "preview_financial_access_restoration",
    "confirm_financial_access_restoration",
    "restore_account_services",
    "restore_account_services_detailed",
]
