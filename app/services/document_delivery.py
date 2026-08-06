"""Reusable owner for emailing a branded document to a party.

Every document we email — a Quote today, a shared catalog next, statements and
contracts after — repeats the same sequence: arbitrate the idempotency key,
render branded bodies, submit a communication intent, derive queued-or-
suppressed, stage an audit event, emit a domain event, return a typed outcome.
Hand-rolling that per document type is how two paths end up disagreeing about
what a replayed key means.

**What this owns:** the sequence and its semantics.
**What it does not own:** storage. Each document type keeps its own typed row
and passes a ``record`` callback. A Quote delivery request is contractual
evidence with RESTRICT foreign keys; a shared catalog is marketing. They hold
genuinely different things and must not be forced into one table. Duplicated
rows are fine when the content differs — duplicated *decisions* are what rot.

Delivery *policy* — suppression, dedupe, channel resolution — is already owned
by :mod:`app.services.communication_intents`; this module calls it rather than
re-deciding. Recipient resolution is owned by
:func:`app.services.party.resolve_email_recipient`.

Idempotency semantics, fixed here so no caller can invent its own:

* same key + same document  -> replay the original outcome, write nothing
* same key + a different document -> ``idempotency_conflict``, never a second send
* no key -> refused; an un-keyed send cannot be safely retried
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.notification import NotificationChannel
from app.services.audit_adapter import stage_audit_event
from app.services.communication_intents import (
    CommunicationAttachment,
    CommunicationAttachmentKind,
    CommunicationIntent,
    submit,
)
from app.services.domain_errors import DomainError
from app.services.email_template import render_email_bodies
from app.services.events import EventType, emit_event
from app.services.party import EmailRecipient


class DocumentDeliveryError(DomainError):
    """Stable failure raised by the document delivery owner."""


class DocumentKind(enum.StrEnum):
    """What is being delivered. Names the audit trail and the event payload."""

    quote = "quote"
    catalog_share = "catalog_share"


@dataclass(frozen=True, slots=True)
class DocumentArtifact:
    """The rendered thing being attached, staged by the document type."""

    artifact_id: UUID
    filename: str
    attachment_kind: CommunicationAttachmentKind


@dataclass(frozen=True, slots=True)
class DocumentComposition:
    """The message wrapped around the artifact.

    ``brand`` is a tuple of pairs rather than a dict so the composition cannot
    be mutated after the caller hands it over — it is reproduced verbatim in
    the audit trail.
    """

    subject: str
    body: str
    template_code: str
    category: str
    intent_event_type: str
    brand: tuple[tuple[str, str], ...] = ()
    app_url: str = ""
    body_html: str | None = None
    body_text: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    def brand_mapping(self) -> dict[str, str]:
        return dict(self.brand)

    def metadata_mapping(self) -> dict[str, str]:
        return dict(self.metadata)


@dataclass(frozen=True, slots=True)
class ExistingDelivery:
    """A prior delivery found under the same idempotency key."""

    delivery_id: UUID
    entity_id: UUID
    artifact_id: UUID
    communication_intent_id: UUID
    queued: bool
    #: The address the document actually went to, masked. Taken from the
    #: stored contact point, not re-resolved: a replay must report what
    #: happened, not what would happen if it were sent again today.
    recipient_masked: str
    suppression_reasons: tuple[str, ...] = ()
    #: Carried so a replay reports the same notifications as the original send
    #: rather than an empty list — a caller cannot tell a replay from a fresh
    #: send otherwise, which defeats the point of an idempotent API.
    notification_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """What the document type needs in order to write its own typed row."""

    delivery_id: UUID
    entity_id: UUID
    artifact_id: UUID
    communication_intent_id: UUID
    recipient_contact_point_id: UUID
    queued: bool
    idempotency_key: str
    actor_id: UUID | None


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """Immutable result. Never the ORM row — a delivery record is evidence."""

    delivery_id: UUID
    entity_id: UUID
    artifact_id: UUID
    communication_intent_id: UUID
    notification_ids: tuple[UUID, ...]
    recipient_masked: str
    queued: bool
    replayed: bool
    suppression_reasons: tuple[str, ...]


def mask_email(value: str) -> str:
    """Enough to recognise an address in an audit trail, not enough to reuse.

    Lifted verbatim from the Quote delivery owner so the masking in existing
    audit records stays byte-identical. Note the star count tracks the local
    part's length — a mild disclosure that predates this module; changing it
    would rewrite how every historical audit entry reads, so it is left alone
    deliberately rather than by oversight.
    """
    local, separator, domain = (value or "").partition("@")
    if not separator:
        return "hidden recipient"
    visible = local[:1]
    return f"{visible}{'*' * max(3, len(local) - 1)}@{domain}"


def _error(suffix: str, message: str, **details: object) -> DocumentDeliveryError:
    return DocumentDeliveryError(
        code=f"communication.document_delivery.{suffix}",
        message=message,
        details=details or None,
    )


def resolve_replay(
    existing: ExistingDelivery,
    *,
    kind: DocumentKind,
    entity_id: UUID,
    idempotency_key: str,
) -> DeliveryOutcome:
    """The outcome for a key that already delivered. Writes nothing.

    Call this BEFORE resolving a recipient. A replay must not fail because the
    address has since been deactivated — the send already happened, and the
    caller is entitled to learn what it was.
    """
    if existing.entity_id != entity_id:
        raise _error(
            "idempotency_conflict",
            f"This {kind.value} delivery key was used for another {kind.value}",
            idempotency_key=idempotency_key,
        )
    return DeliveryOutcome(
        delivery_id=existing.delivery_id,
        entity_id=existing.entity_id,
        artifact_id=existing.artifact_id,
        communication_intent_id=existing.communication_intent_id,
        notification_ids=existing.notification_ids,
        recipient_masked=existing.recipient_masked,
        queued=existing.queued,
        replayed=True,
        suppression_reasons=existing.suppression_reasons,
    )


def deliver(
    db: Session,
    *,
    kind: DocumentKind,
    entity_id: UUID,
    entity_type: str,
    idempotency_key: str | None,
    actor: str | None,
    actor_id: UUID | None,
    request_id: str | None,
    recipient: EmailRecipient,
    artifact: DocumentArtifact,
    composition: DocumentComposition,
    subscriber_id: UUID,
    record: Callable[[DeliveryRecord], None],
    event_type: EventType,
    on_queued: Callable[[], None] | None = None,
) -> DeliveryOutcome:
    """Send one document. Replays are handled by :func:`resolve_replay` first.

    ``record`` writes the caller's own typed row once the intent has been
    submitted, so a row is never written for a send that failed to submit.
    This owner never reads or writes storage it does not own.

    ``on_queued`` runs only on a genuinely queued (not suppressed) send, for
    state the document type owns — a Quote moving from draft to sent, for
    instance. It must not be used for state another owner is authoritative for.
    """
    if not idempotency_key:
        raise _error(
            "idempotency_key_required",
            f"{kind.value} delivery requires an idempotency key",
        )

    if composition.body_html is None:
        body_html, rendered_text = render_email_bodies(
            composition.body,
            subject=composition.subject,
            base_url=composition.app_url,
            brand=composition.brand_mapping(),
        )
        body_text = composition.body_text or rendered_text or composition.body
    else:
        body_html = composition.body_html
        body_text = composition.body_text or composition.body

    delivery_id = uuid4()
    intent_result = submit(
        db,
        CommunicationIntent(
            subscriber_id=subscriber_id,
            event_type=composition.intent_event_type,
            category=composition.category,
            template_code=composition.template_code,
            subject=composition.subject,
            body=body_text or composition.body,
            channels=(NotificationChannel.email,),
            include_reseller=False,
            resolve_subscriber_identity=False,
            recipients={NotificationChannel.email: recipient.email},
            attachments=(
                CommunicationAttachment(
                    kind=artifact.attachment_kind,
                    entity_id=artifact.artifact_id,
                    filename=artifact.filename,
                ),
            ),
            metadata={
                **composition.metadata_mapping(),
                "document_kind": kind.value,
                "entity_id": str(entity_id),
                "delivery_id": str(delivery_id),
                f"{kind.value}_delivery_request_id": str(delivery_id),
                "artifact_id": str(artifact.artifact_id),
                "body_html": body_html,
                "body_text": body_text,
            },
            dedupe_key=f"{kind.value}-email:{idempotency_key}",
        ),
    )
    queued = bool(intent_result.queued)

    record(
        DeliveryRecord(
            delivery_id=delivery_id,
            entity_id=entity_id,
            artifact_id=artifact.artifact_id,
            communication_intent_id=intent_result.intent_id,
            recipient_contact_point_id=recipient.contact_point_id,
            queued=queued,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
        )
    )

    if queued and on_queued is not None:
        on_queued()

    stage_audit_event(
        db,
        action=f"{kind.value}.email_{'queued' if queued else 'suppressed'}",
        entity_type=entity_type,
        entity_id=str(entity_id),
        actor_type=AuditActorType.user,
        actor_id=actor,
        request_id=request_id,
        metadata={
            "delivery_id": str(delivery_id),
            "communication_intent_id": str(intent_result.intent_id),
            "artifact_id": str(artifact.artifact_id),
            "recipient_contact_point_id": str(recipient.contact_point_id),
            "recipient_masked": mask_email(recipient.email),
            "suppression_reasons": list(intent_result.suppressed),
        },
    )
    emit_event(
        db,
        event_type,
        {
            "document_kind": kind.value,
            "entity_id": str(entity_id),
            "delivery_id": str(delivery_id),
            "communication_intent_id": str(intent_result.intent_id),
            "artifact_id": str(artifact.artifact_id),
            "queued": queued,
        },
        actor=actor,
    )
    db.flush()

    return DeliveryOutcome(
        delivery_id=delivery_id,
        entity_id=entity_id,
        artifact_id=artifact.artifact_id,
        communication_intent_id=intent_result.intent_id,
        notification_ids=tuple(item.id for item in intent_result.deliveries),
        recipient_masked=mask_email(recipient.email),
        queued=queued,
        replayed=False,
        suppression_reasons=tuple(intent_result.suppressed),
    )
