"""Pin `collection_accounts` as the single owner of a Dotmac receiving account.

Before this boundary existed the same bank account lived in four places: the
`billing.direct_bank_transfer_accounts` JSON settings blob, the legacy singular
`direct_bank_transfer_*` keys, the `company_bank_*` company-info fields used as an
invoice fallback, and this table. Presentment read the settings while attribution
read the table, so the account shown to a customer was a dict parsed from a string
with no identity to record on the resulting payment — which is why "which of our
accounts received this money?" could not be answered.

These tests fail if a new reader reintroduces one of the retired copies.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import sot_relationships
from tests.architecture.source_index import (
    class_names,
    identifier_names,
    python_files,
    python_nodes,
    string_constants,
)

APP = Path(__file__).resolve().parents[2] / "app"

# The retired settings key. AST string constants make this independent of
# single/double quotes and ignore identifiers such as
# `enabled_direct_bank_transfer_accounts`.
RETIRED_ACCOUNTS_KEY = "direct_bank_transfer_accounts"

ALLOWED_ACCOUNTS_KEY_FILES = {
    # Declares the frozen rollback snapshot until the contract migration.
    "services/settings_spec.py",
}

# Company-info bank fields: retired as an invoice fallback because they could
# print a different account from the one presentment offered.
COMPANY_BANK_FIELDS = ("company_bank_name", "company_bank_account")

ALLOWED_COMPANY_BANK_FILES = {
    "services/web_system_company_info.py",
}


def _python_files() -> list[Path]:
    return list(python_files(APP))


def _constant_offenders(needle: str, allowed: set[str]) -> list[str]:
    offenders = []
    for path in _python_files():
        rel = path.relative_to(APP).as_posix()
        if rel in allowed:
            continue
        if needle in string_constants(path):
            offenders.append(rel)
    return offenders


def test_retired_transfer_accounts_setting_has_no_new_readers() -> None:
    offenders = _constant_offenders(RETIRED_ACCOUNTS_KEY, ALLOWED_ACCOUNTS_KEY_FILES)
    assert not offenders, (
        "Customer-facing bank accounts must resolve through "
        "app.services.billing.collection_account_directory, not the retired "
        f"{RETIRED_ACCOUNTS_KEY!r} setting. Offending modules: {offenders}"
    )


def test_company_info_bank_fields_are_not_a_payment_source() -> None:
    for field in COMPANY_BANK_FIELDS:
        offenders = _constant_offenders(field, ALLOWED_COMPANY_BANK_FILES)
        assert not offenders, (
            f"{field!r} is company metadata, not a collection account. Invoice "
            "and portal bank details come from collection_accounts. Offending "
            f"modules: {offenders}"
        )


def test_sub_does_not_model_a_chart_of_accounts() -> None:
    """Sub carries accounting *mapping* codes; an accounting app owns the ledger.

    `collection_accounts.accounting_code` / `payment_channels.accounting_code` are
    free-form external references. Journals, account categories and balances
    belong to whichever accounting system is integrated, and sub takes no runtime
    dependency on a sibling app to reach them.
    """
    banned = {"ChartOfAccounts", "JournalEntry", "AccountCategory"}
    offenders = []
    for path in _python_files():
        for name in sorted(class_names(path) & banned):
            offenders.append(f"{path.relative_to(APP).as_posix()}: {name}")
    assert not offenders, (
        "Sub must not model a chart of accounts; it carries accounting_code "
        f"mappings only. Found: {offenders}"
    )


def test_customer_bank_detail_consumers_delegate_to_the_owner_reader() -> None:
    required_calls = {
        "services/customer_portal_flow_payments.py": "enabled_transfer_accounts",
        "services/invoice_bank_details.py": "primary_transfer_account",
        "services/web_system_config.py": "enabled_transfer_accounts",
    }
    for relative, required_call in required_calls.items():
        path = APP / relative
        calls = {
            node.func.attr
            for node in python_nodes(path)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "collection_account_directory"
        }
        assert required_call in calls, (
            f"{relative} must delegate bank-detail reads to "
            "collection_account_directory"
        )


def test_collection_account_and_gateway_presentment_owners_are_registered() -> None:
    account_owner = sot_relationships.owning_service_for(
        "collection-account identity and lifecycle"
    )
    gateway_owner = sot_relationships.owning_service_for(
        "ordered customer gateway presentment policy"
    )

    assert account_owner is not None
    assert account_owner.name == "financial.collection_accounts"
    assert account_owner.module == "app.services.billing.collection_accounts"
    assert gateway_owner is not None
    assert gateway_owner.name == "financial.payment_routing"


def test_gateway_presentment_does_not_read_the_attribution_registry() -> None:
    path = APP / "services/payment_routing.py"
    constants = string_constants(path)
    names = identifier_names(path)

    assert "payment_channels" not in constants
    assert "collection_accounts" not in constants
    assert "PaymentChannel" not in names
    assert "CollectionAccount" not in names
