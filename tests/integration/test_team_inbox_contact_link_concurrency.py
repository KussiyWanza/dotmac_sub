"""PostgreSQL concurrency contract for reviewed Team Inbox contact routes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

from sqlalchemy.orm import sessionmaker

from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.team_inbox import InboxContactLink, InboxConversation
from app.services import team_inbox_commands, team_inbox_contact_links
from app.services.owner_commands import CommandContext
from app.services.subscriber import _default_reseller_id


def test_same_endpoint_links_converge_on_one_active_route(engine) -> None:
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex
    contact = str(uuid4().int)[:15]
    with session_factory() as setup:
        subscriber = Subscriber(
            first_name="Concurrent",
            last_name="Contact",
            email=f"team-inbox-contact-{suffix}@example.com",
            status=SubscriberStatus.active,
            is_active=True,
            reseller_id=_default_reseller_id(setup),
        )
        setup.add(subscriber)
        setup.flush()
        conversations = (
            InboxConversation(
                channel_type="facebook_messenger",
                contact_address=contact,
                external_thread_id=f"facebook_messenger:{suffix}:one",
                metadata_={"contact_resolution": {"status": "unmatched"}},
            ),
            InboxConversation(
                channel_type="facebook_messenger",
                contact_address=contact,
                external_thread_id=f"facebook_messenger:{suffix}:two",
                metadata_={"contact_resolution": {"status": "unmatched"}},
            ),
        )
        setup.add_all(conversations)
        setup.commit()
        subscriber_id = subscriber.id
        conversation_ids = tuple(row.id for row in conversations)

    ready = Barrier(2)

    def link(conversation_id: UUID) -> team_inbox_commands.ContactLinkOutcome:
        with session_factory() as session:
            ready.wait(timeout=5)
            return team_inbox_commands.link_contact(
                session,
                team_inbox_commands.LinkContactCommand(
                    context=CommandContext.system(
                        actor="pytest:team-inbox-contact-concurrency",
                        scope="team-inbox:contact-link",
                        reason="prove exact endpoint write serialization",
                    ),
                    conversation_id=conversation_id,
                    target=team_inbox_contact_links.ContactLinkTarget(
                        target_type=(
                            team_inbox_contact_links.ContactLinkTargetType.subscriber
                        ),
                        target_id=subscriber_id,
                    ),
                    actor_person_id=None,
                ),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(link, conversation_ids))

    assert len({outcome.contact_link_id for outcome in outcomes}) == 1
    with session_factory() as check:
        assert (
            check.query(InboxContactLink)
            .filter(
                InboxContactLink.channel_type == "facebook_messenger",
                InboxContactLink.normalized_contact == contact,
                InboxContactLink.is_active.is_(True),
            )
            .count()
            == 1
        )
        linked_conversations = (
            check.query(InboxConversation)
            .filter(InboxConversation.id.in_(conversation_ids))
            .all()
        )
        assert {row.subscriber_id for row in linked_conversations} == {subscriber_id}
