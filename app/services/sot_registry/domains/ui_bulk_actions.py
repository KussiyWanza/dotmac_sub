"""Canonical SOT declarations for the ui_bulk_actions domain."""

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
    domain="ui_bulk_actions",
    services=(
        SOTService(
            name="ui.bulk_action_contracts",
            module="app.services.bulk_actions",
            owns=(
                "bulk selection mode normalization",
                "bulk action capability presentation",
                "bulk preview and confirmation declarations",
                "bulk execution-mode presentation",
            ),
            depends_on=("ui.list_contracts",),
            notes=(
                "These are read-side interaction contracts. Domain command "
                "owners re-check permission, eligibility, scope, and impact "
                "when executing a mutation."
            ),
        ),
        SOTService(
            name="ui.customer_bulk_action_projection",
            module="app.services.web_customer_bulk_actions",
            owns=(
                "admin customer bulk action visibility",
                "admin customer bulk selection presentation",
                "admin customer filtered-selection promotion",
            ),
            depends_on=(
                "ui.bulk_action_contracts",
                "ui.customer_list_projection",
            ),
        ),
        SOTService(
            name="ui.invoice_bulk_action_projection",
            module="app.services.web_billing_invoice_bulk_actions",
            owns=(
                "admin invoice bulk action visibility",
                "admin invoice page-selection presentation",
                "admin invoice bulk eligibility presentation",
                "admin invoice exact-scope review form presentation",
            ),
            depends_on=(
                "ui.bulk_action_contracts",
                "ui.invoice_list_projection",
                "financial.invoices",
            ),
            notes=(
                "app.services.web_billing_invoice_bulk remains the command "
                "eligibility, preview, mutation, audit, and outcome owner. "
                "Mutation and PDF actions submit explicit page IDs to a "
                "server-rendered shared review form; client JavaScript collects "
                "selection only."
            ),
        ),
        SOTService(
            name="ui.support_ticket_bulk_action_projection",
            module="app.services.web_support_ticket_bulk_actions",
            owns=(
                "admin support-ticket bulk action visibility",
                "admin support-ticket page-selection presentation",
                "admin support-ticket row eligibility presentation",
            ),
            depends_on=(
                "ui.bulk_action_contracts",
                "ui.support_ticket_list_projection",
                "support.ticket_bulk_commands",
            ),
            notes=(
                "Selection is page-only. The command owner previews exact "
                "membership, proposed changes, and eligibility before execution."
            ),
            contract=ServiceContract(
                concerns=tuple(
                    ConcernContract(
                        name=name,
                        role=OwnerRole.POLICY,
                        input_names=(
                            "bulk interaction contract",
                            "support Ticket list projection",
                            "support Ticket bulk preview",
                        ),
                    )
                    for name in (
                        "admin support-ticket bulk action visibility",
                        "admin support-ticket page-selection presentation",
                        "admin support-ticket row eligibility presentation",
                    )
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="bulk interaction contract",
                        owner="ui.bulk_action_contracts",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="page-only selection and preview/confirmation UI contract",
                    ),
                    AuthorityInput(
                        name="support Ticket list projection",
                        owner="ui.support_ticket_list_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="current visible page membership and row identifiers",
                    ),
                    AuthorityInput(
                        name="support Ticket bulk preview",
                        owner="support.ticket_bulk_commands",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="exact eligible/skipped membership and signed scope token",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="visibility, selection, and row eligibility are read-only UI data",
                    locking="not applicable; command owner rechecks all eligibility",
                    idempotency="same page and preview facts yield the same actions",
                    retries="rebuild from current list and bulk preview",
                ),
                errors=ErrorContract(
                    domain_codes=("support_ticket_bulk_projection_invalid",),
                    mapping_owner="admin support Ticket template/JSON adapters",
                    fail_closed_on=(
                        "missing page membership",
                        "missing permission",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="support Ticket template and JavaScript bulk eligibility logic",
                    new_owner="ui.support_ticket_bulk_action_projection",
                    verification="bulk action projection, template, and browser tests",
                    cutover_gate="UI renders typed server-declared actions and preview facts",
                    fallback_retirement="client-side lifecycle eligibility decisions are absent",
                ),
                steward="support product UI",
                design_refs=(
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/designs/SUPPORT_UX_POLISH_AUDIT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_support_ticket_bulk_actions.py",
                    "tests/test_support_ticket_list_ui_contract.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.web.admin.customers",
        "app.web.admin.billing_invoice_bulk",
        "app.web.admin.billing_invoices",
        "app.web.admin.support_tickets",
        "app.services.web_customer_actions",
        "app.services.web_billing_invoice_bulk",
        "app.services.web_support_ticket_bulk",
        "templates.admin.billing.invoices",
        "templates.admin.customers",
        "templates.admin.support.tickets",
    ),
    rule="No selection means no bulk action. Page select-all selects only the "
    "visible page; all-filtered scope requires an explicit promotion. "
    "Adapters submit selected IDs or a canonical filtered query, and "
    "command owners resolve the scope again, require impact preview and "
    "confirmation, reject membership or eligibility drift, and report "
    "structured outcomes.",
)
