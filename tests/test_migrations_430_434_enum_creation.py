"""Pin the single PostgreSQL enum-creation path in migrations 430-434."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from sqlalchemy.dialects import postgresql

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic" / "versions"

ENUMS_BY_MIGRATION = {
    "430_billing_contract_obligation_identity.py": {
        "_AUTHORITY": "billingrecordauthority",
        "_SOURCE_KIND": "billingcontractsourcekind",
        "_VERSION_STATUS": "billingcontractversionstatus",
        "_RATE_BASIS": "ratebasis",
        "_INTERVAL_UNIT": "intervalunit",
        "_COLLECTION_TIMING": "collectiontiming",
        "_ALIGNMENT": "cadencealignment",
        "_END_OF_MONTH": "endofmonthrule",
        "_PRORATION": "prorationpolicy",
        "_CHARGE_COMPONENT": "chargecomponent",
        "_ACCOUNTING_TREATMENT": "accountingtreatment",
        "_OBLIGATION_STATE": "obligationstate",
        "_OBLIGATION_RESOLUTION": "obligationresolutionkind",
    },
    "431_customer_subledger_postings.py": {
        "_AUTHORITY": "billingrecordauthority",
        "_COMMAND_KIND": "postingcommandkind",
        "_EFFECT_KIND": "positioneffectkind",
    },
    "432_owner_output_receipts.py": {
        "_OUTCOME": "receiptoutcome",
    },
    "433_durable_timers_collections_cases.py": {
        "_AUTHORITY": "billingrecordauthority",
        "_TIMER_STATUS": "timerstatus",
        "_REASON": "collectionsreason",
        "_CASE_STATE": "collectionscasestate",
    },
    "434_sales_funding_erp_exports.py": {
        "_AUTHORITY": "billingrecordauthority",
        "_GATE_STATE": "fundinggatestate",
        "_ERP_FLOW": "erpbillingflow",
        "_ERP_STATUS": "erpexportstatus",
    },
}

EXPLICIT_OWNERS = {
    "431_customer_subledger_postings.py": (
        "_COMMAND_KIND",
        "_EFFECT_KIND",
    ),
    "432_owner_output_receipts.py": ("_OUTCOME",),
    "433_durable_timers_collections_cases.py": (
        "_TIMER_STATUS",
        "_REASON",
        "_CASE_STATE",
    ),
    "434_sales_funding_erp_exports.py": (
        "_GATE_STATE",
        "_ERP_FLOW",
        "_ERP_STATUS",
    ),
}


def _load_migration(filename: str) -> ModuleType:
    path = VERSIONS / filename
    module_name = f"test_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_explicitly_managed_enums_disable_automatic_table_creation() -> None:
    for filename, expected in ENUMS_BY_MIGRATION.items():
        migration = _load_migration(filename)
        source = Path(migration.__file__).read_text(encoding="utf-8")

        assert "sa.Enum(" not in source
        assert source.count("postgresql.ENUM(") == len(expected)
        for attribute, type_name in expected.items():
            enum_type = getattr(migration, attribute)
            assert isinstance(enum_type, postgresql.ENUM)
            assert enum_type.name == type_name
            assert enum_type.create_type is False


def test_explicit_enum_create_and_drop_ownership_is_unchanged() -> None:
    migration_430 = _load_migration("430_billing_contract_obligation_identity.py")
    expected_430 = ENUMS_BY_MIGRATION["430_billing_contract_obligation_identity.py"]
    assert migration_430._ALL_ENUMS == tuple(
        getattr(migration_430, attribute) for attribute in expected_430
    )
    source_430 = Path(migration_430.__file__).read_text(encoding="utf-8")
    assert "enum_type.create(bind, checkfirst=True)" in source_430
    assert "enum_type.drop(bind, checkfirst=True)" in source_430

    for filename, owner_attributes in EXPLICIT_OWNERS.items():
        migration = _load_migration(filename)
        source = Path(migration.__file__).read_text(encoding="utf-8")
        bind_expression = "op.get_bind()" if filename.startswith("432_") else "bind"
        for attribute in owner_attributes:
            assert f"{attribute}.create({bind_expression}, checkfirst=True)" in source
            assert f"{attribute}.drop({bind_expression}, checkfirst=True)" in source
        if "_AUTHORITY" in ENUMS_BY_MIGRATION[filename]:
            assert "_AUTHORITY.create(" not in source
            assert "_AUTHORITY.drop(" not in source
