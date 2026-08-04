"""Collections services - backwards compatibility wrapper.

This module provides backwards compatibility for code that imports from
`app.services.collections`. The actual implementation is in the
`app.services.collections` package (collections/_core.py).

For new code, prefer importing from the package::

    from app.services.collections import dunning_cases, billing_enforcement_reconciler
"""

import logging

# Re-export everything from the package for backwards compatibility
from app.services.collections._core import (
    BillingEnforcementReconciler,
    DunningActionLogs,
    DunningCases,
    DunningWorkflow,
    FinancialAccessConsequencePreview,
    FinancialAccessConsequenceResult,
    FinancialAccessRestorationParticipantCommand,
    FinancialAccessRestorationParticipantError,
    billing_enforcement_reconciler,
    confirm_financial_access_consequence,
    confirm_financial_access_restoration,
    confirm_financial_access_restoration_for_owner,
    dunning_action_logs,
    dunning_cases,
    dunning_workflow,
    get_available_balance,
    preview_financial_access_consequence,
    preview_financial_access_restoration,
    restore_account_services,
    restore_account_services_detailed,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DunningCases",
    "DunningActionLogs",
    "BillingEnforcementReconciler",
    "DunningWorkflow",
    "FinancialAccessConsequencePreview",
    "FinancialAccessConsequenceResult",
    "FinancialAccessRestorationParticipantCommand",
    "FinancialAccessRestorationParticipantError",
    "dunning_cases",
    "dunning_action_logs",
    "billing_enforcement_reconciler",
    "dunning_workflow",
    "get_available_balance",
    "preview_financial_access_consequence",
    "confirm_financial_access_consequence",
    "preview_financial_access_restoration",
    "confirm_financial_access_restoration",
    "confirm_financial_access_restoration_for_owner",
    "restore_account_services",
    "restore_account_services_detailed",
]
