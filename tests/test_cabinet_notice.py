"""Cabinet service notices: audience tokens, preview counts, drift, dedupe.

Mirrors the customer bulk-message drift contract (test_customer_bulk_actions):
preview binds membership + content + dispositions; execution refuses on drift;
one deduplicated message per distinct customer; marketing consent is
irrelevant to a service communication.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    FdhCabinet,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
)
from app.models.notification import (
    CommunicationIntentRecord,
    CommunicationSuppression,
    Notification,
    NotificationChannel,
    SuppressionReason,
    SuppressionScope,
)
from app.models.subscriber import Subscriber
from app.services.network.cabinet_notice import (
    CabinetNoticeConfirmation,
    CabinetNoticeDraft,
    CabinetNoticeDriftError,
    CabinetNoticeValidationError,
    RecipientDisposition,
    preview_cabinet_notice,
    send_cabinet_notice,
)
from app.services.network.outage_impact import resolve_fdh_audience


def _subscriber(db, *, email, marketing_opt_in=True):
    row = Subscriber(
        first_name="Cab",
        last_name=uuid.uuid4().hex[:8],
        email=email,
        marketing_opt_in=marketing_opt_in,
    )
    db.add(row)
    db.flush()
    return row


def _cabinet(db, name="FDH Alpha"):
    fdh = FdhCabinet(name=name, code=f"FDH-{uuid.uuid4().hex[:6]}")
    db.add(fdh)
    db.flush()
    splitter = Splitter(
        name=f"SPL-{uuid.uuid4().hex[:6]}", fdh_id=fdh.id, splitter_ratio="1:8"
    )
    db.add(splitter)
    db.flush()
    return fdh, splitter


def _attach_customer(db, splitter, subscriber, offer_id, *, port_number):
    port = SplitterPort(splitter_id=splitter.id, port_number=port_number)
    db.add(port)
    db.flush()
    db.add(
        SplitterPortAssignment(
            splitter_port_id=port.id,
            subscriber_id=subscriber.id,
            active=True,
        )
    )
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer_id,
        status=SubscriptionStatus.active,
    )
    db.add(subscription)
    db.flush()
    return subscription


def _draft(fdh, subject="Planned maintenance", body="We are on it.\nBack by 18:00."):
    return CabinetNoticeDraft(fdh_id=fdh.id, subject=subject, body=body)


def _confirmation(preview):
    return CabinetNoticeConfirmation(
        confirmed=True,
        expected_count=preview.eligible,
        expected_scope_token=preview.scope_token,
        expected_impact_token=preview.impact_token,
    )


# ---------------------------------------------------------------------------
# Audience + tokens
# ---------------------------------------------------------------------------


def test_audience_returns_exact_subscriptions_and_stable_token(
    db_session, catalog_offer
):
    fdh, splitter = _cabinet(db_session)
    sub_a = _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email="a@example.test"),
        catalog_offer.id,
        port_number=1,
    )
    sub_b = _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email="b@example.test"),
        catalog_offer.id,
        port_number=2,
    )

    audience = resolve_fdh_audience(db_session, fdh)

    assert set(audience.subscription_ids) == {sub_a.id, sub_b.id}
    # Deterministic: same membership, same token, regardless of call order.
    assert audience.scope_token == resolve_fdh_audience(db_session, fdh).scope_token


def test_audience_token_changes_when_membership_changes(db_session, catalog_offer):
    fdh, splitter = _cabinet(db_session)
    _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email="a@example.test"),
        catalog_offer.id,
        port_number=1,
    )
    before = resolve_fdh_audience(db_session, fdh).scope_token

    _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email="b@example.test"),
        catalog_offer.id,
        port_number=2,
    )

    assert resolve_fdh_audience(db_session, fdh).scope_token != before


def test_audience_unknown_cabinet_raises(db_session):
    with pytest.raises(ValueError):
        resolve_fdh_audience(db_session, str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Preview counts partition the audience
# ---------------------------------------------------------------------------


def test_preview_counts_partition_total_customers(db_session, catalog_offer):
    fdh, splitter = _cabinet(db_session)
    _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email="ok@example.test"),
        catalog_offer.id,
        port_number=1,
    )
    _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email=""),
        catalog_offer.id,
        port_number=2,
    )
    hard_bounced = _subscriber(db_session, email="bounced@example.test")
    _attach_customer(
        db_session, splitter, hard_bounced, catalog_offer.id, port_number=3
    )
    db_session.add(
        CommunicationSuppression(
            channel=NotificationChannel.email,
            address="bounced@example.test",
            scope=SuppressionScope.all,
            reason=SuppressionReason.bounce,
        )
    )
    db_session.flush()

    preview = preview_cabinet_notice(db_session, _draft(fdh))

    assert preview.total_customers == 3
    assert preview.eligible == 1
    assert preview.missing_email == 1
    assert preview.suppressed == 1
    assert preview.unresolved == 0
    assert (
        preview.eligible
        + preview.missing_email
        + preview.suppressed
        + preview.unresolved
        == preview.total_customers
    )
    suppressed_rows = [
        r
        for r in preview.recipients
        if r.disposition is RecipientDisposition.suppressed
    ]
    assert suppressed_rows[0].reason_code == "communication_suppression"


def test_marketing_unsubscribe_does_not_block_service_notice(db_session, catalog_offer):
    """The reason this is not a campaign: a marketing unsubscribe and
    marketing_opt_in=False must NOT remove a customer from an outage notice."""
    fdh, splitter = _cabinet(db_session)
    customer = _subscriber(
        db_session, email="optout@example.test", marketing_opt_in=False
    )
    _attach_customer(db_session, splitter, customer, catalog_offer.id, port_number=1)
    db_session.add(
        CommunicationSuppression(
            channel=NotificationChannel.email,
            address="optout@example.test",
            scope=SuppressionScope.marketing,
            reason=SuppressionReason.unsubscribe,
        )
    )
    db_session.flush()

    preview = preview_cabinet_notice(db_session, _draft(fdh))

    assert preview.eligible == 1
    assert preview.suppressed == 0


def test_multi_subscription_customer_collapses_to_one_recipient(
    db_session, catalog_offer
):
    fdh, splitter = _cabinet(db_session)
    customer = _subscriber(db_session, email="two@example.test")
    _attach_customer(db_session, splitter, customer, catalog_offer.id, port_number=1)
    second = Subscription(
        subscriber_id=customer.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add(second)
    db_session.flush()

    preview = preview_cabinet_notice(db_session, _draft(fdh))

    assert preview.total_subscriptions == 2
    assert preview.total_customers == 1
    assert preview.eligible == 1
    eligible = preview.recipients[0]
    assert len(eligible.subscription_ids) == 2


def test_preview_validates_draft(db_session, catalog_offer):
    fdh, _splitter = _cabinet(db_session)
    with pytest.raises(CabinetNoticeValidationError):
        preview_cabinet_notice(db_session, _draft(fdh, subject="  "))
    with pytest.raises(CabinetNoticeValidationError):
        preview_cabinet_notice(db_session, _draft(fdh, body=""))


def test_crlf_content_produces_same_impact_token(db_session, catalog_offer):
    fdh, splitter = _cabinet(db_session)
    _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email="crlf@example.test"),
        catalog_offer.id,
        port_number=1,
    )

    unix = preview_cabinet_notice(db_session, _draft(fdh, body="line1\nline2"))
    windows = preview_cabinet_notice(db_session, _draft(fdh, body="line1\r\nline2"))

    assert unix.impact_token == windows.impact_token


# ---------------------------------------------------------------------------
# Send: confirmation, drift, dedupe
# ---------------------------------------------------------------------------


def test_send_requires_confirmation_and_expected_values(db_session, catalog_offer):
    fdh, splitter = _cabinet(db_session)
    _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email="c@example.test"),
        catalog_offer.id,
        port_number=1,
    )
    preview = preview_cabinet_notice(db_session, _draft(fdh))

    with pytest.raises(CabinetNoticeValidationError):
        send_cabinet_notice(
            db_session,
            _draft(fdh),
            CabinetNoticeConfirmation(
                confirmed=False,
                expected_count=preview.eligible,
                expected_scope_token=preview.scope_token,
                expected_impact_token=preview.impact_token,
            ),
            actor="ops@dotmac.ng",
        )
    with pytest.raises(CabinetNoticeValidationError):
        send_cabinet_notice(
            db_session,
            _draft(fdh),
            CabinetNoticeConfirmation(
                confirmed=True,
                expected_count=None,
                expected_scope_token=None,
                expected_impact_token=None,
            ),
            actor="ops@dotmac.ng",
        )


def test_send_queues_one_email_per_customer(db_session, catalog_offer):
    fdh, splitter = _cabinet(db_session)
    customer = _subscriber(db_session, email="send@example.test")
    _attach_customer(db_session, splitter, customer, catalog_offer.id, port_number=1)
    preview = preview_cabinet_notice(db_session, _draft(fdh))

    result = send_cabinet_notice(
        db_session, _draft(fdh), _confirmation(preview), actor="ops@dotmac.ng"
    )

    assert result.queued == 1
    assert result.deduplicated == 0
    notifications = db_session.query(Notification).all()
    assert len(notifications) == 1
    row = notifications[0]
    assert row.event_type == "cabinet_service_notice"
    assert row.category == "service"
    assert row.channel == NotificationChannel.email
    assert row.recipient == "send@example.test"
    intent = db_session.get(CommunicationIntentRecord, result.intent_ids[0])
    assert intent.communication_class == "transactional"
    assert intent.include_reseller is False
    assert intent.metadata_["fdh_id"] == str(fdh.id)
    assert intent.metadata_["scope_token"] == preview.scope_token


def test_membership_drift_after_preview_conflicts(db_session, catalog_offer):
    fdh, splitter = _cabinet(db_session)
    _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email="first@example.test"),
        catalog_offer.id,
        port_number=1,
    )
    preview = preview_cabinet_notice(db_session, _draft(fdh))

    # Audience grows between preview and confirm.
    _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email="late@example.test"),
        catalog_offer.id,
        port_number=2,
    )

    with pytest.raises(CabinetNoticeDriftError):
        send_cabinet_notice(
            db_session, _draft(fdh), _confirmation(preview), actor="ops@dotmac.ng"
        )
    assert db_session.query(Notification).count() == 0


def test_content_drift_after_preview_conflicts(db_session, catalog_offer):
    fdh, splitter = _cabinet(db_session)
    _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email="content@example.test"),
        catalog_offer.id,
        port_number=1,
    )
    preview = preview_cabinet_notice(db_session, _draft(fdh, body="original"))

    with pytest.raises(CabinetNoticeDriftError):
        send_cabinet_notice(
            db_session,
            _draft(fdh, body="edited after preview"),
            _confirmation(preview),
            actor="ops@dotmac.ng",
        )
    assert db_session.query(Notification).count() == 0


def test_disposition_drift_after_preview_conflicts(db_session, catalog_offer):
    # Same membership, same content — but a recipient became hard-suppressed
    # after preview. The impact token must catch it.
    fdh, splitter = _cabinet(db_session)
    _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email="flip@example.test"),
        catalog_offer.id,
        port_number=1,
    )
    preview = preview_cabinet_notice(db_session, _draft(fdh))

    db_session.add(
        CommunicationSuppression(
            channel=NotificationChannel.email,
            address="flip@example.test",
            scope=SuppressionScope.all,
            reason=SuppressionReason.complaint,
        )
    )
    db_session.flush()

    with pytest.raises(CabinetNoticeDriftError):
        send_cabinet_notice(
            db_session, _draft(fdh), _confirmation(preview), actor="ops@dotmac.ng"
        )


def test_identical_resend_is_a_durable_noop(db_session, catalog_offer):
    fdh, splitter = _cabinet(db_session)
    _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email="again@example.test"),
        catalog_offer.id,
        port_number=1,
    )
    draft = _draft(fdh)
    first_preview = preview_cabinet_notice(db_session, draft)
    first = send_cabinet_notice(
        db_session, draft, _confirmation(first_preview), actor="ops@dotmac.ng"
    )
    assert first.queued == 1

    second_preview = preview_cabinet_notice(db_session, draft)
    second = send_cabinet_notice(
        db_session, draft, _confirmation(second_preview), actor="ops@dotmac.ng"
    )

    assert second.queued == 0
    assert second.deduplicated == 1
    assert db_session.query(Notification).count() == 1


def test_send_refuses_when_nothing_is_eligible(db_session, catalog_offer):
    fdh, splitter = _cabinet(db_session)
    _attach_customer(
        db_session,
        splitter,
        _subscriber(db_session, email=""),
        catalog_offer.id,
        port_number=1,
    )
    preview = preview_cabinet_notice(db_session, _draft(fdh))
    assert preview.eligible == 0

    with pytest.raises(CabinetNoticeValidationError):
        send_cabinet_notice(
            db_session, _draft(fdh), _confirmation(preview), actor="ops@dotmac.ng"
        )


# ---------------------------------------------------------------------------
# Adapter surface
# ---------------------------------------------------------------------------


def test_routes_are_registered_with_expected_methods():
    from app.web.admin.network_monitoring import router

    methods_by_path: dict[str, set[str]] = {}
    for route in router.routes:
        if route.path.endswith("/cabinet-notice"):
            methods_by_path.setdefault(route.path, set()).update(route.methods or ())
    assert "/network/cabinet-notice" in methods_by_path
    assert "GET" in methods_by_path["/network/cabinet-notice"]
    assert "POST" in methods_by_path["/network/cabinet-notice"]


def test_console_template_compiles():
    from app.web.admin.network_monitoring import templates

    templates.get_template("admin/network/cabinet_notice.html")


def test_entry_buttons_are_wired():
    outage_impact = open("templates/admin/network/outage_impact.html").read()
    cabinet_detail = open(
        "templates/admin/network/fiber/fdh-cabinet-detail.html"
    ).read()
    assert "/admin/network/cabinet-notice?fdh_id=" in outage_impact
    assert "/admin/network/cabinet-notice?fdh_id=" in cabinet_detail


def test_cabinet_notice_is_at_most_once_on_reclaim():
    from app.tasks.notifications import _reclaim_policy

    class _Row:
        category = "service"
        event_type = "cabinet_service_notice"

    assert _reclaim_policy(_Row()) == "at_most_once"
