"""Canonical SOT declarations for regulatory reporting."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="regulatory_reporting",
    services=(
        SOTService(
            name="compliance.ncc_complaints_reporting",
            module="app.services.ncc_complaints_report",
            owns=("NCC complaints report projection",),
            depends_on=("customer.accounts", "support.ticket_lifecycle"),
            notes=(
                "Projects native support and subscriber facts into the NCC filing "
                "vocabulary. Approved internal operational tickets are excluded; "
                "unknown customer identity, classification, SLA, or location stays "
                "explicit."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="NCC complaints report projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "typed NCC report query",
                            "native support ticket facts and operational provenance",
                            "native subscriber facts",
                            "NCC filing vocabulary",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed NCC report query",
                        owner="compliance.ncc_complaints_reporting",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="inclusive UTC complaint-created window",
                    ),
                    AuthorityInput(
                        name="native support ticket facts and operational provenance",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "tickets, comments, stored NCC classification, and "
                            "SLA timestamps, including approved internal-source evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="native subscriber facts",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="subscriber identity and captured NCC geography",
                    ),
                    AuthorityInput(
                        name="NCC filing vocabulary",
                        owner="external:ncc",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "versioned NCC workbook columns, categories, geography, "
                            "and validation rules"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "The caller owns a read session; the query never flushes "
                        "or commits."
                    ),
                    locking="Committed native facts require no mutation lock.",
                    idempotency=(
                        "The same committed facts and UTC window produce the same "
                        "ordered rows."
                    ),
                    retries=(
                        "Bounded read retries are safe; invalid windows fail before "
                        "querying."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=("compliance.ncc_complaints_reporting.invalid_query",),
                    mapping_owner=(
                        "NCC report web, pack, and scheduled-delivery adapters"
                    ),
                    fail_closed_on=("invalid or ambiguous reporting window",),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUT_OVER,
                    old_owner="dotmac_crm NCC complaints report projection",
                    new_owner="compliance.ncc_complaints_reporting",
                    verification=(
                        "native row, validation, workbook, route, and pack tests"
                    ),
                    cutover_gate="CRM versus native bounded-window comparison",
                    fallback_retirement=(
                        "CRM report route retires under the CRM web retirement gate"
                    ),
                ),
                steward="regulatory compliance",
                design_refs=(
                    "docs/designs/NCC_WEEKLY_REPORT_DELIVERY.md",
                    "docs/designs/CRM_REPORT_DATA_FLOW_GUIDE.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_ncc_complaints_report.py",
                    "tests/test_ncc_workbook.py",
                ),
            ),
        ),
    ),
    entrypoints=("app.web.admin.reports", "app.services.ncc_regulatory_pack"),
    rule=(
        "NCC filings project only authoritative native facts and expose every "
        "missing required value; report adapters never infer or write business state."
    ),
)
