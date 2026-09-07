"""Deployment retirement authority for external quote work, never native quotes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class QuoteRetirementOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["retired"] = "retired"
    reconciled: Literal[0] = 0
    refreshed: Literal[False] = False
    actions_available: Literal[False] = False


def retirement_outcome() -> QuoteRetirementOutcome:
    """Quote transport retirement is approved deployment policy, not a fallback."""
    return QuoteRetirementOutcome()
