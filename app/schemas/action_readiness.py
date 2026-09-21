"""Transport (Pydantic) models for ``ActionReadiness``.

The dataclasses in ``app.services.action_readiness`` are the decided shape;
these models exist only to serialize that shape onto a JSON response or web
context. No route is mounted for this schema in this change — it is embedded
into an existing consumer's own response/context (see
``app.services.web_billing_payment_proofs``). A generic cross-domain
readiness endpoint was deliberately rejected: it would recreate the excluded
"global workflow engine" via an implicit dispatch table.

Same import-cleanliness constraint as the two service modules: no
``sqlalchemy``, ``app.models``, or ``app.db``, and no ``Session`` reference.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.services.action_readiness import (
    ActionableBlocker,
    ActionReadiness,
    ReadinessImpact,
    ReadinessState,
)


class BlockerEvidenceRead(BaseModel):
    summary: str
    observed_at: datetime | None = None
    source: str | None = None
    detail_url: str | None = None


class RepairActionRead(BaseModel):
    """Transport shape for ``RepairAction``.

    ``runner`` (the dotted path to the actual domain function) is
    deliberately NEVER exposed here — a client names a repair by ``key`` and
    invokes it via ``action_url``, never by dispatching the runner itself.
    """

    key: str
    label: str
    owner: str
    action_url: str | None = None
    automatic: bool = False


class ActionableBlockerRead(BaseModel):
    code: str
    owner: str
    customer_message: str
    staff_detail: str | None = None
    evidence: BlockerEvidenceRead
    impact: ReadinessImpact = ReadinessImpact.blocking
    repair: RepairActionRead | None = None


class NextActionRead(BaseModel):
    key: str
    label: str
    owner: str
    url: str | None = None
    permission: str | None = None
    clears_blocker_code: str | None = None


class OperationReferenceRead(BaseModel):
    kind: str
    id: UUID
    status: str | None = None
    detail_url: str | None = None


class ActionCorrelationRead(BaseModel):
    correlation_id: UUID
    causation_id: UUID | None = None
    idempotency_key: str | None = None
    workflow_key: str | None = None
    operation_reference: OperationReferenceRead | None = None


class ActionReadinessRead(BaseModel):
    action_key: str
    subject_type: str
    subject_id: str
    owner: str
    state: ReadinessState
    evaluated_at: datetime
    blockers: tuple[ActionableBlockerRead, ...] = ()
    next_actions: tuple[NextActionRead, ...] = ()
    correlation: ActionCorrelationRead | None = None

    @classmethod
    def from_projection(
        cls,
        readiness: ActionReadiness,
        *,
        include_staff_detail: bool,
    ) -> ActionReadinessRead:
        """Serialize a decided ``ActionReadiness``, nulling staff detail when asked."""

        def blocker_read(blocker: ActionableBlocker) -> ActionableBlockerRead:
            repair = blocker.repair
            return ActionableBlockerRead(
                code=blocker.code,
                owner=blocker.owner,
                customer_message=blocker.customer_message,
                staff_detail=blocker.staff_detail if include_staff_detail else None,
                evidence=BlockerEvidenceRead(
                    summary=blocker.evidence.summary,
                    observed_at=blocker.evidence.observed_at,
                    source=blocker.evidence.source,
                    detail_url=blocker.evidence.detail_url,
                ),
                impact=blocker.impact,
                repair=(
                    RepairActionRead(
                        key=repair.key,
                        label=repair.label,
                        owner=repair.owner,
                        action_url=repair.action_url,
                        automatic=repair.automatic,
                    )
                    if repair is not None
                    else None
                ),
            )

        correlation = readiness.correlation
        correlation_read = None
        if correlation is not None:
            operation_reference = correlation.operation_reference
            correlation_read = ActionCorrelationRead(
                correlation_id=correlation.correlation_id,
                causation_id=correlation.causation_id,
                idempotency_key=correlation.idempotency_key,
                workflow_key=correlation.workflow_key,
                operation_reference=(
                    OperationReferenceRead(
                        kind=operation_reference.kind,
                        id=operation_reference.id,
                        status=operation_reference.status,
                        detail_url=operation_reference.detail_url,
                    )
                    if operation_reference is not None
                    else None
                ),
            )

        return cls(
            action_key=readiness.action_key,
            subject_type=readiness.subject_type,
            subject_id=readiness.subject_id,
            owner=readiness.owner,
            state=readiness.state,
            evaluated_at=readiness.evaluated_at,
            blockers=tuple(blocker_read(blocker) for blocker in readiness.blockers),
            next_actions=tuple(
                NextActionRead(
                    key=next_action.key,
                    label=next_action.label,
                    owner=next_action.owner,
                    url=next_action.url,
                    permission=next_action.permission,
                    clears_blocker_code=next_action.clears_blocker_code,
                )
                for next_action in readiness.next_actions
            ),
            correlation=correlation_read,
        )
