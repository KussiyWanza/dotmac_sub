from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models.billing import (
    InvoiceDueDateBasis,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import DunningAction
from app.schemas.billing import InvoiceCreate
from app.schemas.collections import (
    DunningActionLogCreate,
    DunningCaseCreate,
)
from app.services import billing as billing_service
from app.services import collections as collections_service
from tests.prepaid_funding_helpers import materialize_test_prepaid_opening_balance


def _materialize_funding(db_session, account, amount: str = "0.00") -> datetime:
    position_at = datetime.now(UTC) - timedelta(seconds=1)
    materialize_test_prepaid_opening_balance(
        db_session,
        account.id,
        amount,
        position_at=position_at,
    )
    return position_at


def test_dunning_case_and_action_log(db_session, subscriber_account):
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(account_id=subscriber_account.id),
    )
    log = collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.notify,
            outcome="queued",
        ),
    )
    items = collections_service.dunning_action_logs.list(
        db_session,
        case_id=case.id,
        invoice_id=None,
        payment_id=None,
        order_by="executed_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert items[0].id == log.id


def test_dunning_case_list_invalid_status(db_session):
    with pytest.raises(HTTPException) as exc:
        collections_service.dunning_cases.list(
            db_session,
            account_id=None,
            status="not_valid",
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
    assert exc.value.status_code == 400


def test_prepaid_balance_ignores_imported_deposit_after_final_reconstruction(
    db_session, subscriber_account
):
    """Imported deposit is no longer an enforcement balance source."""
    from decimal import Decimal as D

    from app.services.collections._core import _resolve_prepaid_available_balance

    subscriber_account.splynx_customer_id = 25313
    subscriber_account.deposit = D("31965.11")
    db_session.commit()
    _materialize_funding(db_session, subscriber_account)

    issued_at = datetime.now(UTC)
    billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            status=InvoiceStatus.issued,
            total=D("87500.00"),
            balance_due=D("87500.00"),
            issued_at=issued_at,
            due_at=issued_at,
            due_date_basis=InvoiceDueDateBasis.prepaid_service_period,
            due_date_basis_ref="test:prepaid-balance-period",
            due_date_policy_version="test-v1",
        ),
    )
    db_session.commit()

    bal = _resolve_prepaid_available_balance(db_session, str(subscriber_account.id))
    assert bal == D("-87500.00")


def test_prepaid_balance_negative_imported_deposit_is_ignored(
    db_session, subscriber_account
):
    from decimal import Decimal as D

    from app.services.collections._core import _resolve_prepaid_available_balance

    subscriber_account.splynx_customer_id = 9875
    subscriber_account.deposit = D("-2112500.00")
    db_session.commit()
    _materialize_funding(db_session, subscriber_account)
    bal = _resolve_prepaid_available_balance(db_session, str(subscriber_account.id))
    assert bal == D("0.00")


def test_prepaid_balance_native_event_projects_after_reconstruction(
    db_session, subscriber_account
):
    """Native account (no authoritative deposit) keeps the ledger model."""
    from decimal import Decimal as D

    from app.services.collections._core import _resolve_prepaid_available_balance

    subscriber_account.splynx_customer_id = None
    subscriber_account.deposit = None
    _materialize_funding(db_session, subscriber_account)
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=D("500.00"),
            currency="NGN",
            memo="native top-up",
        )
    )
    db_session.commit()
    bal = _resolve_prepaid_available_balance(db_session, str(subscriber_account.id))
    assert bal == D("500.00")
