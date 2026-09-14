from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.services.dotmac_erp.expense_form_contracts import (
    ExpenseDestinationMode,
    VerifiedExpenseDestination,
    VerifyExpenseDestination,
)


def test_override_requires_all_transient_bank_inputs() -> None:
    with pytest.raises(ValidationError):
        VerifyExpenseDestination(
            requested_by_email="tech@example.com",
            source_claim_id=uuid4(),
            mode=ExpenseDestinationMode.EXPENSE_OVERRIDE,
            bank_code="058",
            account_number="0123456789",
        )


def test_profile_mode_refuses_client_supplied_account_number() -> None:
    with pytest.raises(ValidationError):
        VerifyExpenseDestination(
            requested_by_email="tech@example.com",
            source_claim_id=uuid4(),
            mode=ExpenseDestinationMode.ERP_PROFILE,
            account_number="0123456789",
        )


def test_verified_destination_contract_contains_only_masked_account() -> None:
    result = VerifiedExpenseDestination(
        destination_token="enc:" + "x" * 32,
        mode=ExpenseDestinationMode.EXPENSE_OVERRIDE,
        bank_code="058",
        bank_name="Guaranty Trust Bank",
        masked_account_number="******6789",
        verified_beneficiary_name="Field Technician",
        verified_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )

    payload = result.model_dump(mode="json")
    assert payload["masked_account_number"] == "******6789"
    assert "account_number" not in payload
