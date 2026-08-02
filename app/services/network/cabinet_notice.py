"""Operator-initiated cabinet (FDH) service notices — owner ``network.cabinet_notice``.

Composes three existing owners into one confirmed bulk send and adds no
parallel decision path of its own:

- audience + membership token: ``network.outage_impact.resolve_fdh_audience``
  (the same ``fdh_impact_rows`` resolver behind the outage-impact page);
- per-recipient policy: ``customer_notification_policy`` cohort evaluation,
  ``category="service"`` — a transactional communication, so marketing
  unsubscribes and a missing marketing opt-in never block it; only hard
  suppressions (``SuppressionScope.all``: bounce/complaint/erasure) do;
- delivery: one ``communication_intents.submit`` per distinct customer with a
  durable ``dedupe_key``, so a retry or double-confirm is a hard no-op.

Drift protection mirrors the customer bulk-message console: the preview binds
membership (``scope_token``) and content + per-recipient dispositions
(``impact_token``); execution recomputes both and refuses with
``CabinetNoticeDriftError`` (adapter: HTTP 409) when either changed.

Deliberately NOT a campaign: campaigns hard-filter on marketing consent and
write marketing-scope suppression state, which would silently drop most of a
cabinet from an outage/maintenance notice.

Transaction: preview is read-only; send owns its commit (the adapter never
commits — same owner-managed split as the customer bulk-message command).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.notification import CommunicationIntentRecord, NotificationChannel
from app.services import communication_intents
from app.services.customer_notification_policy import (
    CustomerNotificationPolicyCandidate,
    CustomerNotificationPolicyCohortQuery,
    evaluate_bulk_customer_notification_policy,
)
from app.services.events.dispatcher import emit_event
from app.services.events.types import EventType
from app.services.network.outage_impact import CabinetAudience, resolve_fdh_audience

logger = logging.getLogger(__name__)

EVENT_TYPE = "cabinet_service_notice"
CATEGORY = "service"
SUBJECT_MAX_LENGTH = 200
BODY_MAX_LENGTH = 5000


class RecipientDisposition(str, Enum):
    """Why one distinct customer will or will not receive the notice."""

    eligible = "eligible"
    missing_email = "missing_email"
    suppressed = "suppressed"
    unresolved = "unresolved"


class CabinetNoticeValidationError(ValueError):
    """Invalid draft or confirmation input (adapter: HTTP 400)."""


class CabinetNoticeDriftError(RuntimeError):
    """Audience or content changed between preview and confirm (HTTP 409)."""


@dataclass(frozen=True)
class CabinetNoticeDraft:
    """Operator-authored notice content for one cabinet. Free-form on
    purpose: cabinet notices are situational one-offs (cause, ETA); the
    preview shows verbatim what sends and the impact token hashes it."""

    fdh_id: UUID
    subject: str
    body: str


@dataclass(frozen=True)
class CabinetNoticeRecipient:
    """One distinct customer in the preview, with their disposition."""

    subscriber_id: UUID | None
    subscription_ids: tuple[UUID, ...]
    name: str | None
    email: str | None
    disposition: RecipientDisposition
    reason_code: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CabinetNoticePreview:
    """What a confirmed send would do. Counts partition ``total_customers``
    exactly: eligible + missing_email + suppressed + unresolved."""

    fdh_id: UUID
    fdh_label: str
    subject: str
    body: str
    total_subscriptions: int
    total_customers: int
    eligible: int
    missing_email: int
    suppressed: int
    unresolved: int
    scope_token: str
    impact_token: str
    recipients: tuple[CabinetNoticeRecipient, ...]


@dataclass(frozen=True)
class CabinetNoticeConfirmation:
    """The operator's explicit restatement of the previewed impact."""

    confirmed: bool
    expected_count: int | None
    expected_scope_token: str | None
    expected_impact_token: str | None


@dataclass(frozen=True)
class CabinetNoticeResult:
    fdh_id: UUID
    queued: int
    deduplicated: int
    suppressed: int
    intent_ids: tuple[UUID, ...]
    scope_token: str


def _normalize_text(value: str | None) -> str:
    """CRLF->LF + trim so a browser form round-trip can never change the
    impact token of unchanged content."""
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _validated_draft(draft: CabinetNoticeDraft) -> CabinetNoticeDraft:
    subject = _normalize_text(draft.subject)
    body = _normalize_text(draft.body)
    if not subject:
        raise CabinetNoticeValidationError("Subject is required")
    if not body:
        raise CabinetNoticeValidationError("Message body is required")
    if len(subject) > SUBJECT_MAX_LENGTH:
        raise CabinetNoticeValidationError(
            f"Subject exceeds {SUBJECT_MAX_LENGTH} characters"
        )
    if len(body) > BODY_MAX_LENGTH:
        raise CabinetNoticeValidationError(
            f"Message body exceeds {BODY_MAX_LENGTH} characters"
        )
    return CabinetNoticeDraft(fdh_id=draft.fdh_id, subject=subject, body=body)


def _group_recipients(
    db: Session,
    audience: CabinetAudience,
    *,
    now: datetime,
) -> tuple[CabinetNoticeRecipient, ...]:
    """Collapse audience members to one recipient per distinct customer.

    The FDH resolver's service-address arm means one subscriber can own more
    than one affected subscription — the notice contract is one deduplicated
    message per customer, so grouping happens BEFORE policy evaluation.
    Members without a subscriber are surfaced as ``unresolved`` (a repairable
    data gap), never silently dropped.
    """
    grouped: dict[UUID, list] = {}
    unresolved: list[CabinetNoticeRecipient] = []
    for member in audience.members:
        if member.subscriber_id is None:
            unresolved.append(
                CabinetNoticeRecipient(
                    subscriber_id=None,
                    subscription_ids=(member.subscription_id,),
                    name=member.subscriber_name,
                    email=member.email,
                    disposition=RecipientDisposition.unresolved,
                    reason_code="no_subscriber",
                    reason="Subscription has no resolvable subscriber",
                )
            )
            continue
        grouped.setdefault(member.subscriber_id, []).append(member)

    pending: list[tuple[UUID, tuple[UUID, ...], str | None, str]] = []
    no_email: list[CabinetNoticeRecipient] = []
    for subscriber_id, members in grouped.items():
        subscription_ids = tuple(sorted({m.subscription_id for m in members}, key=str))
        name = next((m.subscriber_name for m in members if m.subscriber_name), None)
        email = next((m.email for m in members if m.email), None)
        if not email:
            no_email.append(
                CabinetNoticeRecipient(
                    subscriber_id=subscriber_id,
                    subscription_ids=subscription_ids,
                    name=name,
                    email=None,
                    disposition=RecipientDisposition.missing_email,
                    reason_code="missing_email",
                    reason="Customer has no email address on file",
                )
            )
            continue
        pending.append((subscriber_id, subscription_ids, name, email))

    decisions = {}
    if pending:
        result = evaluate_bulk_customer_notification_policy(
            db,
            CustomerNotificationPolicyCohortQuery(
                candidates=tuple(
                    CustomerNotificationPolicyCandidate(
                        subscriber_id=subscriber_id, recipient=email
                    )
                    for subscriber_id, _subs, _name, email in pending
                ),
                channel=NotificationChannel.email,
                category=CATEGORY,
                event_type=EVENT_TYPE,
                evaluated_at=now,
            ),
        )
        decisions = {decision.candidate.key: decision for decision in result.decisions}

    evaluated: list[CabinetNoticeRecipient] = []
    for subscriber_id, subscription_ids, name, email in pending:
        decision = decisions.get((subscriber_id, email))
        if decision is None or decision.allowed:
            evaluated.append(
                CabinetNoticeRecipient(
                    subscriber_id=subscriber_id,
                    subscription_ids=subscription_ids,
                    name=name,
                    email=email,
                    disposition=RecipientDisposition.eligible,
                )
            )
        else:
            evaluated.append(
                CabinetNoticeRecipient(
                    subscriber_id=subscriber_id,
                    subscription_ids=subscription_ids,
                    name=name,
                    email=email,
                    disposition=RecipientDisposition.suppressed,
                    reason_code=decision.reason_code,
                    reason=decision.reason,
                )
            )

    return tuple(
        sorted(
            [*evaluated, *no_email, *unresolved],
            key=lambda recipient: (
                str(recipient.subscriber_id or ""),
                str(recipient.email or ""),
            ),
        )
    )


def _impact_token(
    *,
    scope_token: str,
    subject: str,
    body: str,
    recipients: tuple[CabinetNoticeRecipient, ...],
) -> str:
    """Bind content + every recipient's disposition to the preview.

    Mirrors the customer bulk-message impact token: membership alone is not
    enough — an edited body, a newly suppressed address, or a customer gaining
    an email between preview and confirm must all invalidate the confirmation.
    """
    payload = {
        "scope_token": scope_token,
        "subject": subject,
        "body": body,
        "channel": NotificationChannel.email.value,
        "recipients": sorted(
            (
                str(recipient.subscriber_id or ""),
                str(recipient.email or ""),
                recipient.disposition.value,
                recipient.reason_code or "",
            )
            for recipient in recipients
        ),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def preview_cabinet_notice(
    db: Session,
    draft: CabinetNoticeDraft,
    *,
    now: datetime | None = None,
) -> CabinetNoticePreview:
    """Resolve the exact audience and classify every distinct customer.

    Read-only: nothing is queued, nothing is written.
    """
    current = now or datetime.now(UTC)
    validated = _validated_draft(draft)
    audience = resolve_fdh_audience(db, str(validated.fdh_id))
    recipients = _group_recipients(db, audience, now=current)
    counts = dict.fromkeys(RecipientDisposition, 0)
    for recipient in recipients:
        counts[recipient.disposition] += 1
    return CabinetNoticePreview(
        fdh_id=audience.fdh_id,
        fdh_label=audience.fdh_label,
        subject=validated.subject,
        body=validated.body,
        total_subscriptions=len(audience.subscription_ids),
        total_customers=len(recipients),
        eligible=counts[RecipientDisposition.eligible],
        missing_email=counts[RecipientDisposition.missing_email],
        suppressed=counts[RecipientDisposition.suppressed],
        unresolved=counts[RecipientDisposition.unresolved],
        scope_token=audience.scope_token,
        impact_token=_impact_token(
            scope_token=audience.scope_token,
            subject=validated.subject,
            body=validated.body,
            recipients=recipients,
        ),
        recipients=recipients,
    )


def _content_fingerprint(subject: str, body: str) -> str:
    return hashlib.sha256(f"{subject}\0{body}".encode()).hexdigest()[:16]


def _dedupe_key(fdh_id: UUID, subscriber_id: UUID, fingerprint: str) -> str:
    return f"cabinet-notice:{fdh_id}:{subscriber_id}:{fingerprint}"


def send_cabinet_notice(
    db: Session,
    draft: CabinetNoticeDraft,
    confirmation: CabinetNoticeConfirmation,
    *,
    actor: str | None,
    now: datetime | None = None,
) -> CabinetNoticeResult:
    """Queue one deduplicated service notice per eligible customer.

    Recomputes the preview fresh and refuses with ``CabinetNoticeDriftError``
    when membership, content, or any recipient disposition changed since the
    operator previewed. The per-customer ``dedupe_key`` (cabinet + customer +
    content fingerprint) makes identical re-sends a durable no-op at the
    intent layer. OWNS its commit (like the customer bulk-message command):
    the calling adapter must not commit — the intents, the dispatch event and
    the audit trail land atomically here or not at all.
    """
    if not confirmation.confirmed:
        raise CabinetNoticeValidationError("Send requires explicit confirmation")
    if (
        confirmation.expected_count is None
        or not confirmation.expected_scope_token
        or not confirmation.expected_impact_token
    ):
        raise CabinetNoticeValidationError("Preview the notice before confirming")

    preview = preview_cabinet_notice(db, draft, now=now)
    # Drift beats every other refusal: an audience that shrank to zero after
    # preview is a conflict the operator must see, not a validation error.
    drifted = (
        confirmation.expected_count != preview.eligible
        or not hmac.compare_digest(
            confirmation.expected_scope_token, preview.scope_token
        )
        or not hmac.compare_digest(
            confirmation.expected_impact_token, preview.impact_token
        )
    )
    if drifted:
        raise CabinetNoticeDriftError(
            "The cabinet audience, message content, or recipient impact "
            "changed after preview. Review the updated preview before "
            "confirming again."
        )
    if preview.eligible == 0:
        raise CabinetNoticeValidationError("No eligible recipients — nothing to send")

    fingerprint = _content_fingerprint(preview.subject, preview.body)
    queued = 0
    deduplicated = 0
    suppressed = 0
    intent_ids: list[UUID] = []
    for recipient in preview.recipients:
        if recipient.disposition is not RecipientDisposition.eligible:
            continue
        if recipient.subscriber_id is None or recipient.email is None:
            continue  # unreachable by construction; belt-and-braces
        dedupe_key = _dedupe_key(preview.fdh_id, recipient.subscriber_id, fingerprint)
        already_sent = (
            db.query(CommunicationIntentRecord.id)
            .filter(CommunicationIntentRecord.dedupe_key == dedupe_key)
            .first()
        )
        if already_sent is not None:
            deduplicated += 1
            continue
        result = communication_intents.submit(
            db,
            communication_intents.CommunicationIntent(
                subscriber_id=recipient.subscriber_id,
                event_type=EVENT_TYPE,
                category=CATEGORY,
                subject=preview.subject,
                body=preview.body,
                communication_class=communication_intents.CommunicationClass.transactional,
                # Explicit email-only slice: pin the channel AND the exact
                # address so contact fan-out cannot multiply the blast.
                channels=(NotificationChannel.email,),
                recipients={NotificationChannel.email: recipient.email},
                include_reseller=False,
                dedupe_key=dedupe_key,
                metadata={
                    "fdh_id": str(preview.fdh_id),
                    "fdh_label": preview.fdh_label,
                    "scope_token": preview.scope_token,
                    "impact_token": preview.impact_token,
                    "subscription_ids": [
                        str(subscription_id)
                        for subscription_id in recipient.subscription_ids
                    ],
                    "actor": actor,
                },
            ),
        )
        intent_ids.append(result.intent_id)
        if result.queued:
            queued += 1
        else:
            suppressed += 1

    emit_event(
        db,
        EventType.cabinet_notice_dispatched,
        {
            "fdh_id": str(preview.fdh_id),
            "fdh_label": preview.fdh_label,
            "scope_token": preview.scope_token,
            "queued": queued,
            "deduplicated": deduplicated,
            "suppressed": suppressed,
            "eligible": preview.eligible,
            "actor": actor,
        },
        actor=actor,
    )
    logger.info(
        "cabinet_notice_dispatched fdh=%s queued=%s deduplicated=%s suppressed=%s",
        preview.fdh_id,
        queued,
        deduplicated,
        suppressed,
        extra={
            "event": "cabinet_notice_dispatched",
            "fdh_id": str(preview.fdh_id),
            "queued": queued,
            "deduplicated": deduplicated,
            "suppressed": suppressed,
            "actor": actor,
        },
    )
    db.commit()
    return CabinetNoticeResult(
        fdh_id=preview.fdh_id,
        queued=queued,
        deduplicated=deduplicated,
        suppressed=suppressed,
        intent_ids=tuple(intent_ids),
        scope_token=preview.scope_token,
    )
