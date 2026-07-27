"""Owner of the job-scoped conversation between a customer and their technician.

The customer talks to the technician who is on their way, in the selfcare
portal, and nowhere else. ``team_inbox`` stays the owner of customer↔staff
messaging — this service only decides *when* such a conversation exists and
*who* holds it. It is the only writer of the work-order↔conversation link.

**It opens on departure, not on assignment.** A technician can hold several
assigned jobs at once, and opening a chat per assignment would put every one of
those customers in front of them simultaneously while they can only be at one.
A technician is en route to one job at a time, so departure is the point where
exactly one customer has something worth saying — and it is still before
arrival, which is when a wrong address is still worth correcting.

Consequences of that choice:

* Before departure there is no conversation, so a customer question goes to the
  support queue as it does today. That is the right home for it anyway: an
  agent can amend the work order, whereas a correction typed at a technician
  lives only in a chat message and never reaches the record.
* Departing for another job closes the previous chat. ``arrive_movement`` falls
  back to the latest open ``en_route`` movement, so a technician who never taps
  Arrive would otherwise accumulate live chats; closing on the next departure
  makes "one live chat per technician" hold by construction.

The link is ``channel_type='field_job'`` plus ``external_thread_id`` set to the
work order's ``public_id``, which rides the existing
``ix_inbox_conversations_external_thread`` index and needs no migration.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamSource,
)
from app.models.work_order import WorkOrder
from app.services import service_team_lifecycle
from app.services.team_inbox_assignment import assign_conversation_to_agent

FIELD_JOB_CHANNEL = InboxChannelType.field_job.value

#: Why a job has no chat. Returned rather than raised so the caller — a field
#: transition — never fails a real job movement because chat could not open.
OpenOutcome = str

OPENED: OpenOutcome = "opened"
NO_SUBSCRIBER: OpenOutcome = "no_subscriber"
NO_TEAM: OpenOutcome = "no_team"

#: Set when a visit ended with the customer's message unanswered. It is the one
#: thing that pulls a job chat into the triage queue it is otherwise kept out of.
QUEUE_FOLLOWUP_KEY = "field_job_queue_followup"


def _thread_id(work_order: WorkOrder) -> str:
    return str(work_order.public_id)


def conversation_for(db: Session, work_order: WorkOrder) -> InboxConversation | None:
    """Return the job's conversation, open or resolved."""
    return (
        db.query(InboxConversation)
        .filter(InboxConversation.channel_type == FIELD_JOB_CHANNEL)
        .filter(InboxConversation.external_thread_id == _thread_id(work_order))
        .one_or_none()
    )


def is_open(conversation: InboxConversation) -> bool:
    """True while the visit is live and either side may still send.

    A closed job chat keeps its history readable; it just takes no new
    messages, which is the same rule ``send_inbox_reply`` already applies to a
    resolved conversation.
    """
    return conversation.status != InboxConversationStatus.resolved.value


def _service_team_id(db: Session, system_user_id: uuid.UUID) -> uuid.UUID | None:
    """The technician's team, which the assignment row requires (NOT NULL).

    Technician profiles carry a staff-principal identifier while canonical
    memberships are Party-backed. The lifecycle owner resolves that boundary
    and fails closed on missing or ambiguous membership.
    """
    resolution = service_team_lifecycle.resolve_staff_service_team(
        db,
        system_user_id,
    )
    if resolution.kind is not service_team_lifecycle.ServiceTeamResolutionKind.resolved:
        return None
    return resolution.team_id


def _open_conversations_for_technician(
    db: Session, person_id: uuid.UUID
) -> list[InboxConversation]:
    """Live job chats currently assigned to this technician."""
    return (
        db.query(InboxConversation)
        .join(InboxConversation.assignments)
        .filter(InboxConversation.channel_type == FIELD_JOB_CHANNEL)
        .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
        .filter(InboxConversation.assignments.any(person_id=person_id, is_active=True))
        .all()
    )


def open_for_departure(
    db: Session,
    *,
    work_order: WorkOrder,
    profile: TechnicianProfile,
    now: datetime | None = None,
) -> tuple[InboxConversation | None, OpenOutcome]:
    """Open (or reuse) this job's chat and make it the technician's only live one.

    Never raises: a job movement is the real event, and a chat that cannot open
    must not roll it back.
    """
    moment = now or datetime.now(UTC)
    if work_order.subscriber_id is None:
        return None, NO_SUBSCRIBER

    team_id = _service_team_id(db, profile.person_id)
    if team_id is None:
        # No team means no assignment row is possible, and an unassigned job
        # chat is one nobody holds — better to have none at all.
        return None, NO_TEAM

    for other in _open_conversations_for_technician(db, profile.person_id):
        if other.external_thread_id != _thread_id(work_order):
            close(db, conversation=other, reason="departed_for_another_job", now=moment)

    conversation = conversation_for(db, work_order)
    if conversation is None:
        conversation = InboxConversation(
            channel_type=FIELD_JOB_CHANNEL,
            external_thread_id=_thread_id(work_order),
            subscriber_id=work_order.subscriber_id,
            subject=work_order.title or "Technician visit",
            status=InboxConversationStatus.open.value,
            first_message_at=None,
        )
        db.add(conversation)
        db.flush()
    else:
        conversation.status = InboxConversationStatus.open.value

    metadata = dict(conversation.metadata_ or {})
    metadata["work_order_id"] = str(work_order.id)
    metadata["work_order_public_id"] = _thread_id(work_order)
    metadata["opened_at"] = moment.isoformat()
    metadata.pop("closed_at", None)
    metadata.pop("closed_reason", None)
    conversation.metadata_ = metadata

    result = assign_conversation_to_agent(
        db,
        conversation=conversation,
        service_team_id=team_id,
        person_id=profile.person_id,
        source=InboxTeamSource.manual.value,
        reason="field job departure",
        now=moment,
    )
    if result.kind != "assigned":
        # A no-op assignment would leave a live chat nobody holds. Reported,
        # not raised — the departure itself is still valid.
        return None, NO_TEAM

    db.flush()
    return conversation, OPENED


def record_customer_message(
    db: Session,
    *,
    conversation: InboxConversation,
    body: str,
    author_name: str | None,
    now: datetime | None = None,
) -> dict:
    """Write the customer's side of a job chat.

    Returns an inert snapshot rather than the ORM row: the caller commits
    through ``team_inbox_commands`` and must not touch a committed object.

    Inbound because it is the customer speaking, which is also what lets the
    completion fallback tell that they are still owed an answer. It lives in
    the inbox family so the portal adapter never writes an inbox row itself.
    """
    moment = now or datetime.now(UTC)
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=FIELD_JOB_CHANNEL,
        direction=InboxMessageDirection.inbound.value,
        body=body,
        external_thread_id=conversation.external_thread_id,
        to_addresses=[],
        cc_addresses=[],
        received_at=moment,
        metadata_={
            "channel_type": FIELD_JOB_CHANNEL,
            "author_name": author_name,
        },
    )
    db.add(message)
    conversation.last_message_at = moment
    if conversation.first_message_at is None:
        conversation.first_message_at = moment
    db.flush()

    # Snapshot inside the transaction. Reading an ORM attribute after the
    # caller's commit opens a fresh one, so hand back inert values.
    return {
        "id": str(message.id),
        "conversation_id": str(conversation.id),
        "body": message.body,
        "author_name": author_name,
        "created_at": message.created_at,
    }


def close(
    db: Session,
    *,
    conversation: InboxConversation,
    reason: str,
    now: datetime | None = None,
) -> None:
    """Resolve a job chat, leaving its history readable."""
    moment = now or datetime.now(UTC)
    conversation.status = InboxConversationStatus.resolved.value
    metadata = dict(conversation.metadata_ or {})
    metadata["closed_at"] = moment.isoformat()
    metadata["closed_reason"] = reason
    conversation.metadata_ = metadata
    db.flush()


def close_for_work_order(
    db: Session,
    *,
    work_order: WorkOrder,
    reason: str,
    now: datetime | None = None,
) -> InboxConversation | None:
    """Close a job chat when the visit ends, unless the customer is still owed
    an answer.

    A job chat is deliberately kept out of the triage queue, which also means
    no unreplied sweep is watching it. If the visit ends with the customer's
    message unanswered, resolving here would silently drop it — so it goes to
    the queue instead, and the exclusion lets it through.
    """
    conversation = conversation_for(db, work_order)
    if conversation is None:
        return None
    if _awaiting_reply(db, conversation):
        return _hand_to_queue(db, conversation=conversation, now=now)
    close(db, conversation=conversation, reason=reason, now=now)
    return conversation


def _awaiting_reply(db: Session, conversation: InboxConversation) -> bool:
    """True when the newest exchanged message is the customer's."""
    latest = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(
            InboxMessage.direction.in_(
                (
                    InboxMessageDirection.inbound.value,
                    InboxMessageDirection.outbound.value,
                )
            )
        )
        .order_by(InboxMessage.created_at.desc())
        .first()
    )
    return (
        latest is not None and latest.direction == InboxMessageDirection.inbound.value
    )


def _hand_to_queue(
    db: Session,
    *,
    conversation: InboxConversation,
    now: datetime | None = None,
) -> InboxConversation:
    """Leave the chat open and make the triage queue show it."""
    moment = now or datetime.now(UTC)
    conversation.status = InboxConversationStatus.open.value
    for assignment in conversation.assignments:
        if assignment.is_active:
            # The technician has left the job; holding them to it would hide
            # the message behind someone who is no longer working it.
            assignment.is_active = False
    metadata = dict(conversation.metadata_ or {})
    metadata[QUEUE_FOLLOWUP_KEY] = True
    metadata["queued_at"] = moment.isoformat()
    conversation.metadata_ = metadata
    db.flush()
    return conversation
