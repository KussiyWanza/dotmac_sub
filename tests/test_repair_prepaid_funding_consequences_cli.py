"""Non-vacuous proof of the repair CLI's post-execution evidence check.

`scripts/billing/repair_prepaid_funding_consequences.py`'s `--apply` path
deliberately skips the trigger receipt (`skip_receipt_for_repair=True`), so
there is no receipt-child row to check afterward -- the in-memory
`subscription_decisions` the settlement call returns is the ONLY evidence
available, and `main()` must refuse to resolve the review item when no
decision actually matches the exact subscription being repaired (2026-09,
round 8; this was previously unconditional -- the call not raising was
trusted as proof enough).

`matching_settled_decision` is pure (no I/O, no session), so it is tested
directly with fake evaluation/renewal/decision objects rather than driving
the whole CLI through a real database and permission stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from scripts.billing.repair_prepaid_funding_consequences import (
    matching_settled_decision,
)


@dataclass(frozen=True)
class _FakeDecision:
    subscription_id: UUID
    disposition: str


@dataclass(frozen=True)
class _FakeRenewal:
    subscription_decisions: tuple[_FakeDecision, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _FakeEvaluation:
    renewal: _FakeRenewal | None


def test_matching_settled_decision_finds_the_exact_subscription():
    subscription_id = uuid4()
    evaluation = _FakeEvaluation(
        renewal=_FakeRenewal(
            subscription_decisions=(
                _FakeDecision(
                    subscription_id=subscription_id,
                    disposition="existing_draft_settled",
                ),
            )
        )
    )

    decision = matching_settled_decision(evaluation, subscription_id=subscription_id)

    assert decision is not None
    assert decision.subscription_id == subscription_id


def test_matching_settled_decision_refuses_when_renewal_is_none():
    """The settlement call returned no renewal at all (e.g. a consolidated-
    invoice-allocation terminal disposition) -- nothing to match against."""

    evaluation = _FakeEvaluation(renewal=None)

    assert matching_settled_decision(evaluation, subscription_id=uuid4()) is None


def test_matching_settled_decision_refuses_when_no_decision_for_this_subscription():
    """The call succeeded and funded a DIFFERENT subscription on this
    account, not the one this repair was scoped to -- must not be mistaken
    for evidence that THIS repair's target subscription settled."""

    other_subscription_id = uuid4()
    target_subscription_id = uuid4()
    evaluation = _FakeEvaluation(
        renewal=_FakeRenewal(
            subscription_decisions=(
                _FakeDecision(
                    subscription_id=other_subscription_id,
                    disposition="existing_draft_settled",
                ),
            )
        )
    )

    assert (
        matching_settled_decision(evaluation, subscription_id=target_subscription_id)
        is None
    )


def test_matching_settled_decision_refuses_a_non_settled_disposition():
    """A decision exists for the exact subscription, but its disposition is
    not one of the two that mean a real settlement happened -- must not be
    treated as proof of a successful repair."""

    subscription_id = uuid4()
    evaluation = _FakeEvaluation(
        renewal=_FakeRenewal(
            subscription_decisions=(
                _FakeDecision(
                    subscription_id=subscription_id,
                    disposition="blocked_ambiguous",
                ),
            )
        )
    )

    assert (
        matching_settled_decision(evaluation, subscription_id=subscription_id) is None
    )
