"""Deployment retirement authority for external quote work, never native quotes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

QUOTE_ACTIONS_UNAVAILABLE_MESSAGE = (
    "Online quoting is unavailable. Nothing was charged and no quote was "
    "changed. Please contact support to continue."
)


class QuoteRetirementOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["retired"] = "retired"
    reconciled: Literal[0] = 0
    refreshed: Literal[False] = False
    actions_available: Literal[False] = False
    customer_message: str = QUOTE_ACTIONS_UNAVAILABLE_MESSAGE


def retirement_outcome() -> QuoteRetirementOutcome:
    """Quote transport retirement is approved deployment policy, not a fallback."""
    return QuoteRetirementOutcome()
