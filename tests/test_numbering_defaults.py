from decimal import Decimal

from app.models.domain_settings import SettingDomain
from app.schemas.billing import CreditNoteCreate, InvoiceCreate
from app.schemas.settings import DomainSettingUpdate
from app.schemas.subscriber import SubscriberAccountCreate, SubscriberCreate
from app.services import billing as billing_service
from app.services import settings_api, settings_spec
from app.services import subscriber as subscriber_service


def test_invoice_number_defaults_use_settings(db_session, subscriber_account):
    settings_api.upsert_billing_setting(
        db_session,
        "invoice_number_prefix",
        DomainSettingUpdate(value_text="INVX-"),
    )
    settings_api.upsert_billing_setting(
        db_session,
        "invoice_number_padding",
        DomainSettingUpdate(value_text="4"),
    )
    settings_api.upsert_billing_setting(
        db_session,
        "invoice_number_start",
        DomainSettingUpdate(value_text="42"),
    )
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
        ),
    )
    assert invoice.invoice_number == "INVX-0042"


def test_project_number_defaults_match_imported_series(db_session):
    assert (
        settings_spec.resolve_value(
            db_session, SettingDomain.projects, "project_number_enabled"
        )
        is True
    )
    assert (
        settings_spec.resolve_value(
            db_session, SettingDomain.projects, "project_number_prefix"
        )
        == "PROJ-"
    )
    assert (
        settings_spec.resolve_value(
            db_session, SettingDomain.projects, "project_number_padding"
        )
        == 4
    )


def test_credit_note_number_defaults_use_settings(db_session, subscriber_account):
    settings_api.upsert_billing_setting(
        db_session,
        "credit_note_number_prefix",
        DomainSettingUpdate(value_text="CRX-"),
    )
    settings_api.upsert_billing_setting(
        db_session,
        "credit_note_number_padding",
        DomainSettingUpdate(value_text="3"),
    )
    settings_api.upsert_billing_setting(
        db_session,
        "credit_note_number_start",
        DomainSettingUpdate(value_text="7"),
    )
    credit_note = billing_service.credit_notes.create(
        db_session,
        CreditNoteCreate(
            account_id=subscriber_account.id,
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
        ),
    )
    assert credit_note.credit_number == "CRX-007"


def test_subscriber_account_number_derives_from_subscriber_number(db_session, person):
    settings_api.upsert_subscriber_setting(
        db_session,
        "subscriber_number_prefix",
        DomainSettingUpdate(value_text="SUBX-"),
    )
    settings_api.upsert_subscriber_setting(
        db_session,
        "subscriber_number_padding",
        DomainSettingUpdate(value_text="4"),
    )
    settings_api.upsert_subscriber_setting(
        db_session,
        "subscriber_number_start",
        DomainSettingUpdate(value_text="12"),
    )
    settings_api.upsert_subscriber_setting(
        db_session,
        "account_number_prefix",
        DomainSettingUpdate(value_text="ACX-"),
    )
    subscriber = subscriber_service.subscribers.create(
        db_session,
        SubscriberCreate(person_id=person.id),
    )
    assert subscriber.account_number == "ACX-0012"
    account = subscriber_service.accounts.create(
        db_session,
        SubscriberAccountCreate(subscriber_id=subscriber.id),
    )
    assert subscriber.subscriber_number == "SUBX-0012"
    assert account.account_number == "ACX-0012"
