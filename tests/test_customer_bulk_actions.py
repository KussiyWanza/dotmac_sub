from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationTemplate,
)
from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.models.support import Ticket
from app.services import web_customer_actions
from app.services.whatsapp_notification_templates import (
    parse_provider_template_body,
    sync_whatsapp_registry_templates,
)
from app.web.admin import customers as customers_web

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_customer_bulk_message_preview_does_not_dispatch_delivery(monkeypatch):
    preview_result = {
        "success": True,
        "preview": True,
        "matched_count": 2,
        "queued_count": 2,
    }
    dispatch_calls = []

    monkeypatch.setattr(
        customers_web.web_customer_actions_service,
        "queue_bulk_message_from_payload",
        lambda *, db, payload: preview_result,
    )
    monkeypatch.setattr(
        customers_web,
        "_kick_notification_delivery",
        lambda result: dispatch_calls.append(result) or result,
    )

    result = customers_web.bulk_send_customer_message(
        request=None,
        data={"preview_only": True},
        db=object(),
    )

    assert result is preview_result
    assert dispatch_calls == []


def _confirmed_selected_scope(db_session, *customer_ids: str) -> dict[str, object]:
    selection: dict[str, object] = {
        "mode": "selected",
        "ids": list(customer_ids),
    }
    resolved = web_customer_actions.resolve_bulk_customer_scope(
        db_session,
        {"selection": selection},
    )
    return {
        **selection,
        "expected_count": resolved.matched_count,
        "expected_scope_token": resolved.scope_token,
    }


def _previewed_message_payload(
    db_session,
    *,
    customer_ids: tuple[str, ...],
    channel: str,
    template_id: str,
    template_variables: dict[str, object] | None = None,
) -> dict[str, object]:
    base_payload: dict[str, object] = {
        "selection": {
            "mode": "selected",
            "ids": list(customer_ids),
        },
        "channel": channel,
        "template_id": template_id,
        "template_variables": template_variables or {},
    }
    preview = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        {**base_payload, "preview_only": True},
    )
    return {
        **base_payload,
        "selection": {
            **base_payload["selection"],
            "expected_count": preview["matched_count"],
            "expected_scope_token": preview["scope_token"],
        },
        "expected_impact_token": preview["impact_token"],
        "confirmed": True,
    }


def test_customer_whatsapp_template_lookup_fetch_handles_non_json_errors():
    template = (REPO_ROOT / "templates/admin/customers/index.html").read_text()

    assert "'Accept': 'application/json'" in template
    assert "const raw = await response.text();" in template
    assert "JSON.parse(raw)" in template
    assert (
        "Could not load WhatsApp template details (HTTP ${response.status})."
        in template
    )


def test_customer_send_email_action_opens_template_modal_for_email_channel():
    table_template = (REPO_ROOT / "templates/admin/customers/_table.html").read_text()
    page_template = (REPO_ROOT / "templates/admin/customers/index.html").read_text()
    detail_template = (REPO_ROOT / "templates/admin/customers/detail.html").read_text()

    assert "mailto:" not in table_template
    assert "customer-send-message" in table_template
    assert "channel: 'email'" in table_template
    assert "const { id, type, channel } = e.detail;" in page_template
    assert "this.resetSendMessageForm(channel);" in page_template
    assert "@click.prevent.stop=\"openSendMessageModal('email')\"" in detail_template
    assert "detailUrl" in detail_template
    assert "closeSendMessageModal" in detail_template
    assert '@change="handleTemplateSelection()"' in detail_template
    assert "this.sendMessageForm.templateId = '';" in detail_template
    assert "preview_only: true" in detail_template
    assert "Send ${selectedChannelMeta().label} template" in detail_template
    assert "whatsappTemplateVariables" in detail_template
    assert "template.provider_template_name || template.code || template.name" in (
        detail_template
    )
    assert "!hasRequiredWhatsAppMappings()" in detail_template
    assert "template_variables: this.sendMessageForm.templateVariables || {}" in (
        detail_template
    )
    assert "confirmed: true" in detail_template
    assert "mailto:" not in detail_template


def test_customer_bulk_actions_sync_selection_from_checked_rows_before_submit():
    page_template = (REPO_ROOT / "templates/admin/customers/index.html").read_text()

    assert "syncSelectionFromDom()" in page_template
    assert "applySelectionToDom()" in page_template
    assert (
        "document.querySelectorAll('#customers-table [data-customer-checkbox]:checked')"
        in page_template
    )
    assert (
        "document.querySelectorAll('#customers-table [data-customer-checkbox]')"
        in page_template
    )
    assert "this.syncSelectionFromDom();" in page_template
    assert "this.applySelectionToDom();" in page_template
    assert (
        "this.clearSelection();"
        not in page_template.split("htmx:afterSwap", 1)[1].split("});", 1)[0]
    )
    assert (
        "this.selectedIds.push({ id: customer.id, type: customer.type });"
        in page_template
    )
    assert (
        "this.selectedIds.filter((item) => !visibleIds.has(item.id))" in page_template
    )
    assert "Matched ${matched} customer(s)." in page_template
    assert "skipped due to missing contact details" in page_template
    assert "excluded because they have open tickets" in page_template
    assert "suppressed by preferences, dedupe, or other template conditions" in (
        page_template
    )


def test_bulk_update_customers_requires_explicit_filtered_scope_preview_and_confirmation(
    db_session,
):
    matched = Subscriber(
        first_name="Ada",
        last_name="Scope",
        email="ada-scope@example.com",
        user_type=UserType.customer,
        is_active=True,
        billing_enabled=True,
    )
    other = Subscriber(
        first_name="Ben",
        last_name="Other",
        email="ben-other@example.com",
        user_type=UserType.customer,
        is_active=True,
        billing_enabled=True,
    )
    db_session.add_all([matched, other])
    db_session.commit()

    payload = {
        "selection": {
            "mode": "filtered",
            "filters": {"search": "ada-scope@example.com"},
        },
        "updates": {
            "account_state": "inactive",
            "billing_enabled": False,
            "payment_method": "bank_transfer",
        },
    }
    preview = web_customer_actions.bulk_update_customers_from_payload(
        db_session,
        {**payload, "preview_only": True},
    )

    db_session.refresh(matched)
    assert preview["preview"] is True
    assert preview["matched_count"] == 1
    assert matched.is_active is True

    result = web_customer_actions.bulk_update_customers_from_payload(
        db_session,
        {
            **payload,
            "selection": {
                **payload["selection"],
                "expected_count": preview["matched_count"],
                "expected_scope_token": preview["scope_token"],
            },
            "confirmed": True,
        },
    )

    db_session.refresh(matched)
    db_session.refresh(other)

    assert result["scope"] == "filtered"
    assert result["updated_count"] == 1
    assert matched.is_active is False
    assert matched.billing_enabled is False
    assert matched.payment_method == "bank_transfer"
    assert other.is_active is True
    assert other.billing_enabled is True


def test_bulk_update_customers_does_not_fall_through_to_filtered_scope(db_session):
    with pytest.raises(HTTPException, match="Select at least one record"):
        web_customer_actions.bulk_update_customers_from_payload(
            db_session,
            {
                "customer_ids": [],
                "filters": {"status": "active"},
                "updates": {"billing_enabled": False},
            },
        )


def test_bulk_update_rejects_scope_membership_drift_after_preview(db_session):
    customer = Subscriber(
        first_name="Scope",
        last_name="Changed",
        email="scope-changed@example.com",
        user_type=UserType.customer,
        is_active=True,
        billing_enabled=True,
    )
    db_session.add(customer)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        web_customer_actions.bulk_update_customers_from_payload(
            db_session,
            {
                "selection": {
                    "mode": "selected",
                    "ids": [str(customer.id)],
                    "expected_count": 1,
                    "expected_scope_token": "stale-preview-token",
                },
                "updates": {"billing_enabled": False},
                "confirmed": True,
            },
        )

    db_session.refresh(customer)
    assert exc.value.status_code == 409
    assert "scope changed after preview" in exc.value.detail
    assert customer.billing_enabled is True


def test_queue_bulk_message_from_selected_scope_renders_template_and_skips_missing_recipient(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.customer_notification_policy.channel_disabled_in_config",
        lambda _db, _channel: False,
    )
    monkeypatch.setattr(
        "app.services.notification.channel_disabled_in_config",
        lambda _db, _channel: False,
    )
    reachable = Subscriber(
        first_name="Rita",
        last_name="Reachable",
        email="rita@example.com",
        phone="+2348011111111",
        account_number="AC-1001",
        user_type=UserType.customer,
        is_active=True,
    )
    missing_phone = Subscriber(
        first_name="Sam",
        last_name="NoPhone",
        email="sam@example.com",
        phone=None,
        account_number="AC-1002",
        user_type=UserType.customer,
        is_active=True,
    )
    template = NotificationTemplate(
        name="Outage SMS",
        code="outage_sms",
        channel=NotificationChannel.sms,
        body="Hello {customer_name} on {account_number}",
        is_active=True,
    )
    db_session.add_all([reachable, missing_phone, template])
    db_session.commit()

    result = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        _previewed_message_payload(
            db_session,
            customer_ids=(str(reachable.id), str(missing_phone.id)),
            channel="sms",
            template_id=str(template.id),
        ),
    )

    assert result["scope"] == "selected"
    assert result["matched_count"] == 2
    assert result["created_count"] == 1
    assert result["queued_count"] == 1
    assert len(result["skipped"]) == 1

    notification = db_session.get(Notification, result["notification_ids"][0])
    assert notification is not None
    assert notification.recipient == "+2348011111111"
    assert notification.body == "Hello Rita Reachable on AC-1001"


def test_queue_bulk_email_backfills_common_template_aliases(db_session):
    customer = Subscriber(
        first_name="Chidinma",
        last_name="Onyemachi",
        email="chidinma@example.com",
        account_number="ACC-005069",
        user_type=UserType.customer,
        is_active=True,
    )
    offer = CatalogOffer(
        name="Dotmac Fiber 50Mbps",
        code="fiber_50",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    subscription = Subscription(
        subscriber=customer,
        offer=offer,
        status=SubscriptionStatus.active,
    )
    template = NotificationTemplate(
        name="Manual Email",
        code="manual_email",
        channel=NotificationChannel.email,
        subject="Hello {subscriber_name}",
        body=(
            "Dear {subscriber_name}, "
            "your {{offer_name}} plan on {account_number} is managed by {{company_name}}."
        ),
        is_active=True,
    )
    db_session.add_all([customer, offer, subscription, template])
    db_session.commit()

    result = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        _previewed_message_payload(
            db_session,
            customer_ids=(str(customer.id),),
            channel="email",
            template_id=str(template.id),
        ),
    )

    notification = db_session.get(Notification, result["notification_ids"][0])
    assert notification is not None
    assert notification.subject == "Hello Chidinma Onyemachi"
    assert "Dear Chidinma Onyemachi" in notification.body
    assert "Dotmac Fiber 50Mbps" in notification.body
    assert "ACC-005069" in notification.body
    assert "{{" not in notification.body
    assert "{subscriber_name}" not in notification.body


def test_queue_bulk_email_rejects_unavailable_template_variables(db_session):
    customer = Subscriber(
        first_name="Ada",
        last_name="Blocked",
        email="ada-blocked@example.com",
        user_type=UserType.customer,
        is_active=True,
    )
    template = NotificationTemplate(
        name="Bad Manual Email",
        code="bad_manual_email",
        channel=NotificationChannel.email,
        subject="Hello {customer_name}",
        body="This should not send: {{unknown_value}}",
        is_active=True,
    )
    db_session.add_all([customer, template])
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        web_customer_actions.queue_bulk_message_from_payload(
            db_session,
            {
                "selection": _confirmed_selected_scope(db_session, str(customer.id)),
                "channel": "email",
                "template_id": str(template.id),
                "confirmed": True,
            },
        )

    assert exc.value.status_code == 400
    assert "{unknown_value}" in exc.value.detail


def test_queue_bulk_email_allows_placeholder_shaped_customer_values(db_session):
    customer = Subscriber(
        first_name="New",
        last_name="Nigcomsat {MD}",
        display_name="New Nigcomsat {MD}",
        email="nigcomsat-md@example.com",
        user_type=UserType.customer,
        is_active=True,
    )
    template = NotificationTemplate(
        name="Manual Email",
        code="manual_email_braced_value",
        channel=NotificationChannel.email,
        subject="Account guide",
        body="Dear {customer_name}, your guide is ready.",
        is_active=True,
    )
    db_session.add_all([customer, template])
    db_session.commit()

    result = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        _previewed_message_payload(
            db_session,
            customer_ids=(str(customer.id),),
            channel="email",
            template_id=str(template.id),
        ),
    )

    assert result["queued_count"] == 1
    notification = db_session.get(Notification, result["notification_ids"][0])
    assert notification is not None
    assert notification.body == "Dear New Nigcomsat {MD}, your guide is ready."


def test_queue_bulk_message_preview_batches_large_customer_policy_cohort(
    db_session,
    monkeypatch,
):
    customers = [
        Subscriber(
            first_name="Preview",
            last_name=str(index),
            email=f"preview-{index}@example.com",
            user_type=UserType.customer,
            is_active=True,
        )
        for index in range(120)
    ]
    template = NotificationTemplate(
        name="Large Preview Email",
        code="large_preview_email",
        channel=NotificationChannel.email,
        subject="Account guide",
        body="Dear {customer_name}, your guide is ready.",
        conditions={
            "all": [
                {
                    "field": "customer_has_open_ticket",
                    "operator": "=",
                    "value": False,
                }
            ]
        },
        is_active=True,
    )
    db_session.add_all([*customers, template])
    db_session.commit()
    customer_ids = [str(customer.id) for customer in customers]
    monkeypatch.setattr(
        web_customer_actions,
        "get_customer_financial_position",
        lambda *_args, **_kwargs: pytest.fail(
            "preview must not load unused billing template variables"
        ),
    )

    select_statements = 0

    def _count_statement(_conn, _cursor, statement, *_args, **_kwargs):
        nonlocal select_statements
        if statement.lstrip().upper().startswith("SELECT"):
            select_statements += 1

    event.listen(
        db_session.get_bind(),
        "before_cursor_execute",
        _count_statement,
    )
    try:
        result = web_customer_actions.queue_bulk_message_from_payload(
            db_session,
            {
                "selection": {
                    "mode": "selected",
                    "ids": customer_ids,
                },
                "channel": "email",
                "template_id": str(template.id),
                "preview_only": True,
            },
        )
    finally:
        event.remove(
            db_session.get_bind(),
            "before_cursor_execute",
            _count_statement,
        )

    assert result["matched_count"] == 120
    assert result["queued_count"] == 120
    assert result["suppressed_count"] == 0
    assert result["notification_ids"] == []
    assert result["render_sample_count"] == 3
    assert len(result["recipient_summary"]) == 10
    assert result["recipient_summary"][0]["recipient"].endswith("@example.com")
    assert "***" in result["recipient_summary"][0]["recipient"]
    assert len(result["impact_token"]) == 64
    assert select_statements < 35


def test_queue_bulk_message_preview_identifies_single_recipient_safely(db_session):
    customer = Subscriber(
        first_name="Hauwa",
        last_name="Grema Mohammed",
        email="hauwa.gremah@example.com",
        account_number="ACC-1042",
        user_type=UserType.customer,
        is_active=True,
    )
    template = NotificationTemplate(
        name="Invoice Explanation",
        code="invoice_explanation",
        channel=NotificationChannel.email,
        subject="Payment update",
        body="Hello {customer_name}",
        is_active=True,
    )
    db_session.add_all([customer, template])
    db_session.commit()

    preview = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        {
            "selection": {"mode": "selected", "ids": [str(customer.id)]},
            "channel": "email",
            "template_id": str(template.id),
            "preview_only": True,
        },
    )

    assert preview["recipient_summary"] == [
        {
            "id": str(customer.id),
            "name": "Hauwa Grema Mohammed",
            "account_number": "ACC-1042",
            "recipient": "h***@example.com",
            "disposition": "queued",
        }
    ]


def test_queue_bulk_message_rejects_recipient_drift_after_preview(db_session):
    customer = Subscriber(
        first_name="Preview",
        last_name="Recipient",
        email="before@example.com",
        user_type=UserType.customer,
        is_active=True,
    )
    template = NotificationTemplate(
        name="Recipient Drift",
        code="recipient_drift",
        channel=NotificationChannel.email,
        subject="Service update",
        body="Hello {customer_name}",
        is_active=True,
    )
    db_session.add_all([customer, template])
    db_session.commit()
    base_payload = {
        "selection": {"mode": "selected", "ids": [str(customer.id)]},
        "channel": "email",
        "template_id": str(template.id),
    }
    preview = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        {**base_payload, "preview_only": True},
    )

    customer.email = "after@example.com"
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        web_customer_actions.queue_bulk_message_from_payload(
            db_session,
            {
                **base_payload,
                "selection": {
                    **base_payload["selection"],
                    "expected_count": preview["matched_count"],
                    "expected_scope_token": preview["scope_token"],
                },
                "expected_impact_token": preview["impact_token"],
                "confirmed": True,
            },
        )

    assert exc.value.status_code == 409
    assert "recipients, template, or suppression impact changed" in exc.value.detail
    assert db_session.query(Notification).count() == 0


def test_queue_bulk_message_rejects_template_drift_after_preview(db_session):
    customer = Subscriber(
        first_name="Preview",
        last_name="Template",
        email="template-preview@example.com",
        user_type=UserType.customer,
        is_active=True,
    )
    template = NotificationTemplate(
        name="Template Drift",
        code="template_drift",
        channel=NotificationChannel.email,
        subject="Original subject",
        body="Hello {customer_name}",
        is_active=True,
    )
    db_session.add_all([customer, template])
    db_session.commit()
    base_payload = {
        "selection": {"mode": "selected", "ids": [str(customer.id)]},
        "channel": "email",
        "template_id": str(template.id),
    }
    preview = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        {**base_payload, "preview_only": True},
    )

    template.subject = "Changed subject"
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        web_customer_actions.queue_bulk_message_from_payload(
            db_session,
            {
                **base_payload,
                "selection": {
                    **base_payload["selection"],
                    "expected_count": preview["matched_count"],
                    "expected_scope_token": preview["scope_token"],
                },
                "expected_impact_token": preview["impact_token"],
                "confirmed": True,
            },
        )

    assert exc.value.status_code == 409
    assert db_session.query(Notification).count() == 0


def test_queue_bulk_message_preview_reports_disabled_customers_as_suppressed(
    db_session,
):
    customer = Subscriber(
        first_name="Disabled",
        last_name="Customer",
        email="disabled-customer@example.com",
        user_type=UserType.customer,
        status=SubscriberStatus.disabled,
        is_active=False,
    )
    template = NotificationTemplate(
        name="Disabled Preview Email",
        code="disabled_preview_email",
        channel=NotificationChannel.email,
        subject="Account guide",
        body="Dear {customer_name}, your guide is ready.",
        is_active=True,
    )
    db_session.add_all([customer, template])
    db_session.commit()

    result = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        {
            "selection": {
                "mode": "selected",
                "ids": [str(customer.id)],
            },
            "channel": "email",
            "template_id": str(template.id),
            "preview_only": True,
        },
    )

    assert result["queued_count"] == 0
    assert result["suppressed_count"] == 1
    assert result["suppressed"][0]["reason_code"] == "account_status"
    assert db_session.query(Notification).count() == 0


def test_queue_bulk_message_preview_does_not_create_rows(db_session):
    customer = Subscriber(
        first_name="Preview",
        last_name="Only",
        email="preview@example.com",
        user_type=UserType.customer,
        is_active=True,
    )
    template = NotificationTemplate(
        name="Preview Email",
        code="preview_email",
        channel=NotificationChannel.email,
        subject="Hello {customer_name}",
        body="Body for {account_number}",
        is_active=True,
    )
    db_session.add_all([customer, template])
    db_session.commit()

    result = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        {
            "selection": _confirmed_selected_scope(db_session, str(customer.id)),
            "channel": "email",
            "template_id": str(template.id),
            "preview_only": True,
        },
    )

    assert result["preview"] is True
    assert result["created_count"] == 1
    assert result["queued_count"] == 1
    assert result["notification_ids"] == []
    assert db_session.query(Notification).count() == 0


def test_whatsapp_registry_templates_sync_into_notification_templates(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.whatsapp_notification_templates.whatsapp_capability.active_config",
        lambda _db: {
            "templates": [
                {"name": "service_restoration", "language": "en_US"},
                {"name": "closed", "language": "en_US"},
                {"name": "closed", "language": "en"},
            ]
        },
    )

    templates = sync_whatsapp_registry_templates(db_session)
    provider_rows = {
        (
            parse_provider_template_body(template.body)["name"],
            parse_provider_template_body(template.body)["language"],
        ): template
        for template in templates
        if parse_provider_template_body(template.body)
    }

    assert ("service_restoration", "en_US") in provider_rows
    assert ("closed", "en_US") in provider_rows
    assert ("closed", "en") in provider_rows
    assert provider_rows[("service_restoration", "en_US")].channel == (
        NotificationChannel.whatsapp
    )
    assert (
        provider_rows[("closed", "en_US")].code != provider_rows[("closed", "en")].code
    )


def test_queue_bulk_whatsapp_respects_template_conditions(db_session):
    customer = Subscriber(
        first_name="Wale",
        last_name="Ticketed",
        email="wale-ticketed@example.com",
        phone="+2348022222222",
        user_type=UserType.customer,
        is_active=True,
    )
    template = NotificationTemplate(
        name="Service Restoration",
        code="service_restoration",
        channel=NotificationChannel.whatsapp,
        body=(
            '{"__whatsapp_template__": true, "name": "service_restoration", '
            '"language": "en_US", "variables": {}}'
        ),
        conditions={
            "all": [
                {
                    "field": "customer_has_open_ticket",
                    "operator": "=",
                    "value": False,
                }
            ]
        },
        is_active=True,
    )
    ticket = Ticket(
        subscriber_id=customer.id,
        title="Service issue",
        status="open",
        priority="normal",
    )
    db_session.add_all([customer, template])
    db_session.flush()
    ticket.subscriber_id = customer.id
    db_session.add(ticket)
    db_session.commit()

    result = web_customer_actions.queue_bulk_message_from_payload(
        db_session,
        _previewed_message_payload(
            db_session,
            customer_ids=(str(customer.id),),
            channel="whatsapp",
            template_id=str(template.id),
        ),
    )

    assert result["created_count"] == 1
    assert result["queued_count"] == 0
    assert result["suppressed_count"] == 1
    assert result["suppressed"] == [
        {
            "id": str(customer.id),
            "name": "Wale Ticketed",
            "reason_code": "open_ticket",
            "reason": "Customer has an open ticket",
        }
    ]
    notification = db_session.get(Notification, result["notification_ids"][0])
    assert notification is not None
    assert notification.status.value == "canceled"
    assert notification.last_error == "Suppressed by open ticket template condition"
