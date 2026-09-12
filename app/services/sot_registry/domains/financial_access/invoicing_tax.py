"""financial_access SOT declarations: invoicing tax."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="financial.payment_gateway_finance",
        module="app.services.payment_gateway_finance",
        owns=(
            "gateway finance provider identity bootstrap",
            "gateway settlement-channel bootstrap",
            "Integrator installation provider mapping",
            "active gateway finance identity resolution",
        ),
        notes=(
            "This flush-only participant ensures finance attribution "
            "identities during connector setup and owns the operator-approved "
            "opaque Integrator source mapping. It does not decide gateway "
            "availability or presentment."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="gateway finance provider identity bootstrap",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "payment gateway connector manifest",
                        "payment gateway installation setup",
                    ),
                    canonical_writer="financial.payment_gateway_finance",
                ),
                ConcernContract(
                    name="gateway settlement-channel bootstrap",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "payment gateway connector manifest",
                        "payment gateway installation setup",
                    ),
                    canonical_writer="financial.payment_gateway_finance",
                ),
                ConcernContract(
                    name="Integrator installation provider mapping",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "operator-approved Integrator installation identity",
                        "canonical payment provider identity",
                    ),
                    canonical_writer="financial.payment_gateway_finance",
                ),
                ConcernContract(
                    name="active gateway finance identity resolution",
                    role=OwnerRole.RESOLVER,
                    input_names=("canonical payment provider identity",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="payment gateway connector manifest",
                    owner="integration.registry",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "deployed Paystack or Flutterwave connector "
                        "identity and capability declaration"
                    ),
                ),
                AuthorityInput(
                    name="payment gateway installation setup",
                    owner="integration.installations",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "operator-approved connector setup transaction "
                        "with provider type and complete capability bundle"
                    ),
                ),
                AuthorityInput(
                    name="operator-approved Integrator installation identity",
                    owner="integration.installations",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "opaque external installation UUID submitted by an "
                        "authenticated admin integration adapter"
                    ),
                ),
                AuthorityInput(
                    name="canonical payment provider identity",
                    owner="financial.payment_gateway_finance",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked local PaymentProvider row",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "integration.installations supplies the transaction; "
                    "this participant only flushes finance identities, source "
                    "mappings, and their evidence events."
                ),
                locking=(
                    "Provider type, canonical provider/channel names, and the "
                    "target provider row are locked before unique constraints "
                    "arbitrate concurrent setup or source mapping."
                ),
                idempotency=(
                    "An existing unique provider identity and its first "
                    "provider-linked channel replay without another row or event; "
                    "an unchanged Integrator mapping emits no second event."
                ),
                retries=(
                    "The installation coordinator retries only after a "
                    "complete rollback."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.payment_gateway_finance.provider_identity_ambiguous",
                    "financial.payment_gateway_finance.channel_identity_ambiguous",
                    "financial.payment_gateway_finance.provider_name_conflict",
                    "financial.payment_gateway_finance.channel_name_conflict",
                    "financial.payment_gateway_finance.provider_not_found",
                    "financial.payment_gateway_finance.provider_identity_mismatch",
                    "financial.payment_gateway_finance.provider_inactive",
                    "financial.payment_gateway_finance.integrator_installation_conflict",
                ),
                mapping_owner=(
                    "payment gateway admin adapters and verified-payment coordinators"
                ),
                retryable_codes=(),
                fail_closed_on=(
                    "multiple provider identities",
                    "multiple provider-linked settlement channels",
                    "canonical provider or channel name collision",
                    "inactive or checkout-mismatched provider identity",
                    "unknown provider or duplicate Integrator installation mapping",
                ),
            ),
            events=EventContract(
                event_types=(
                    "payment_gateway.finance_identity_ensured",
                    "payment_gateway.integrator_mapping_changed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries provider type, canonical finance identity, "
                    "and opaque Integrator installation identifiers without secrets."
                ),
                replay=(
                    "Provider and channel rows rebuild attribution; event "
                    "replay never enables a connector."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "billing and integration admin provider CRUD plus "
                    "payment-provider mutation API"
                ),
                new_owner="financial.payment_gateway_finance",
                verification=(
                    "Gateway setup, idempotency, secret-reference, routing, opaque "
                    "source mapping, and architecture tests."
                ),
                cutover_gate=(
                    "Connector setup is the only caller that can create "
                    "Paystack or Flutterwave finance identities; the authenticated "
                    "admin integration adapter is the only source-mapping writer."
                ),
                fallback_retirement=(
                    "Legacy provider CRUD routes, templates, service "
                    "methods, and mutation API are removed."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/designs/PAYMENT_GATEWAY_CONTROL_PLANE_SOT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_web_integrations_payment_gateways.py",
                "tests/test_payment_routing.py",
                "tests/test_paystack_outside_window_recovery.py",
                "tests/test_integrator_payment_provider_mapping.py",
                "tests/architecture/test_sot_manifest_contracts.py",
                "tests/architecture/test_integrator_settlement_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="financial.payments",
        module="app.services.billing.payments",
        owns=(
            "payment document lifecycle",
            "payment intent and observation lifecycle",
            "confirmed payment settlement preview and evidence",
            "payment creation and settlement idempotency and audit",
            "exact settlement allocation and unallocated-credit links",
            "confirmed payment funding-change outbox event",
            "settled account-credit allocation preview and confirmation",
            "exact invoice-credit and account-credit-consumption links",
            "native unallocated-credit reconciliation transactions",
            "historical payment settlement evidence reconciliation",
            "payment settlement access-reconciliation handoff",
            "payment-originated ledger postings",
            "cash-first verified provider settlement evidence",
            "settlement-aware customer receipt application summary",
            "payment allocation reconciliation exception lifecycle",
            "payment refund eligibility and preview",
            "payment refund confirmation and exact ledger evidence",
            "payment refund idempotency and audit evidence",
            "historical payment refund evidence reconciliation",
            "payment refund access-reconciliation handoff",
            "payment reversal eligibility and preview",
            "payment reversal confirmation and exact ledger evidence",
            "payment reversal idempotency and audit evidence",
            "normalized provider reversal evidence",
            "historical payment reversal evidence reconciliation",
            "payment reversal access-reconciliation handoff",
        ),
        depends_on=(
            "financial.ledger",
            "financial.billing_accounts",
            "events.dispatcher",
        ),
    ),
    SOTService(
        name="financial.import_payment_batch_reversals",
        module="app.services.financial_import_batch_reversals",
        owns=(
            "payment import creation provenance",
            "imported-payment batch reversal eligibility and preview",
            "locked imported-payment batch reversal confirmation",
            "batch reversal idempotency and actor audit evidence",
            "exact import-row-to-settlement-to-reversal ledger links",
            "imported-payment reversal access-reconciliation handoff",
        ),
        depends_on=(
            "financial.payments",
            "customer.financial_position",
        ),
        notes=(
            "Only payments structurally proven to have been created by "
            "one durable apply run can be reversed. Reused or historical "
            "rows without provenance are never inferred from JSON, "
            "external IDs, amounts, or memos. Confirmation composes the "
            "payment reversal owner and keeps every source and result row."
        ),
    ),
    SOTService(
        name="financial.invoice_discounts",
        module="app.services.invoice_discounts",
        owns=(
            "Invoice discount current state and pricing",
            "Invoice discount append-only revision history",
        ),
        depends_on=(
            "auth.staff_provisioning",
            "events.dispatcher",
            "financial.invoices",
            "sales.service",
        ),
        notes=(
            "This flush-only participant prices one mutually exclusive percentage "
            "or fixed discount against the original Invoice subtotal, recalculates "
            "tax on the discounted base, and appends immutable revision evidence. "
            "Administrative draft authoring and Quote deposit creation supply the "
            "transaction. Quote-inherited discounts retain their source identity "
            "and cannot be changed or applied twice. The history page is a direct "
            "read of this canonical evidence, not a second writable projection."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="Invoice discount current state and pricing",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed Invoice discount request",
                        "canonical Invoice subtotal and lifecycle",
                        "canonical staff actor state",
                        "optional canonical source Quote discount",
                    ),
                    canonical_writer="financial.invoice_discounts",
                ),
                ConcernContract(
                    name="Invoice discount append-only revision history",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed Invoice discount request",
                        "canonical Invoice subtotal and lifecycle",
                        "canonical staff actor state",
                        "optional canonical source Quote discount",
                    ),
                    canonical_writer="financial.invoice_discounts",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="typed Invoice discount request",
                    owner="financial.invoice_discounts",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed percentage or fixed value, optional reason, source, "
                        "source Quote identity, actor, command identity, and timestamp"
                    ),
                ),
                AuthorityInput(
                    name="canonical Invoice subtotal and lifecycle",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked Invoice and active InvoiceLine rows",
                ),
                AuthorityInput(
                    name="canonical staff actor state",
                    owner="auth.staff_provisioning",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked active SystemUser addressed by the session actor",
                ),
                AuthorityInput(
                    name="optional canonical source Quote discount",
                    owner="sales.service",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Quote discount type, value, actual amount, reason, actor, "
                        "and structural Quote-to-deposit-Invoice identity"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "The administrative Invoice draft or Quote deposit coordinator "
                    "supplies the transaction; this participant mutates current "
                    "discount fields, recalculates totals, appends one history row, "
                    "and only flushes."
                ),
                locking=(
                    "The coordinator locks the customer account and Invoice before "
                    "this participant locks the active staff actor; source Quote "
                    "evidence is resolved before Invoice construction."
                ),
                idempotency=(
                    "A command UUID is unique in history; unchanged requested state "
                    "converges without another revision, and inherited source identity "
                    "rejects a second or different discount."
                ),
                retries=(
                    "Callers retry only their whole owning transaction after rollback."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.invoice_discounts.value_invalid",
                    "financial.invoice_discounts.type_invalid",
                    "financial.invoice_discounts.subtotal_invalid",
                    "financial.invoice_discounts.exceeds_subtotal",
                    "financial.invoice_discounts.reason_invalid",
                    "financial.invoice_discounts.actor_not_eligible",
                    "financial.invoice_discounts.invoice_mismatch",
                    "financial.invoice_discounts.quote_source_invalid",
                    "financial.invoice_discounts.quote_inheritance_invalid",
                    "financial.invoice_discounts.inherited_locked",
                    "financial.invoice_discounts.invoice_not_editable",
                    "financial.invoice_discounts.page_invalid",
                    "financial.invoice_discounts.page_size_invalid",
                    "financial.invoice_discounts.date_range_invalid",
                ),
                mapping_owner="administrative billing web adapters",
                retryable_codes=(),
                fail_closed_on=(
                    "discount greater than subtotal or invalid percentage",
                    "missing or inactive staff actor",
                    "issued manual discount mutation",
                    "missing Quote source evidence or double discount",
                ),
            ),
            events=EventContract(
                event_types=(
                    "invoice.discount_applied",
                    "invoice.discount_changed",
                    "invoice.discount_removed",
                    "invoice.discount_inherited",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 identifies the Invoice, revision, action, source, "
                    "discount type/value/amount, currency, and resulting total "
                    "without customer contact details."
                ),
                replay=(
                    "The unique command UUID and canonical fingerprint suppress "
                    "duplicate history and event staging."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="no first-class Invoice discount owner or history",
                new_owner="financial.invoice_discounts",
                verification=(
                    "Manual draft, tax recalculation, immutable history, Quote "
                    "inheritance, double-discount guard, reporting, migration, and "
                    "architecture tests."
                ),
                cutover_gate=(
                    "All new Invoice discounts are first-class current state plus "
                    "append-only history and are written only inside an owning "
                    "Invoice creation transaction."
                ),
                fallback_retirement=(
                    "No metadata or Line Item discount writer is accepted as an "
                    "Invoice discount source."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/INVOICE_DISCOUNT_HISTORY.md",
            ),
            test_refs=(
                "tests/test_invoice_discounts.py",
                "tests/test_quote_deposits.py",
                "tests/architecture/test_invoice_discount_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.invoice_draft_authoring",
        module="app.services.invoice_draft_authoring",
        owns=(
            "administrative invoice draft authoring coordination",
            "administrative proforma conversion coordination",
        ),
        depends_on=(
            "control.settings_spec",
            "customer.accounts",
            "financial.invoices",
            "financial.invoice_discounts",
            "financial.tax_configuration",
            "events.dispatcher",
        ),
        notes=(
            "This coordinator is the only administrative writer for a "
            "complete draft invoice aggregate. It admits a typed header and "
            "line set, locks the account before the invoice, and commits the "
            "document, totals, audit, idempotency evidence, and outbox event "
            "once. It also owns locked, idempotent administrative proforma "
            "conversion and derives the final status only after canonical "
            "account credit is applied. Generic conversion fails closed for "
            "prepaid accounts or prepaid-linked lines so that the reviewed "
            "prepaid reconciliation owner retains documentary adoption, "
            "settlement, entitlement, and billing-anchor decisions. Void, "
            "write-off, settlement, and repair remain with their named "
            "financial owners."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="administrative invoice draft authoring coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated administrative draft command",
                        "canonical customer account",
                        "canonical invoice draft aggregate",
                        "canonical invoice tax rates",
                    ),
                ),
                ConcernContract(
                    name="administrative proforma conversion coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated administrative proforma conversion command",
                        "canonical customer account",
                        "canonical invoice draft aggregate",
                        "canonical invoice numbering policy",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated administrative draft command",
                    owner="financial.invoice_draft_authoring",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed header, complete line set, actor, scope, "
                        "reason, correlation, and idempotency context"
                    ),
                ),
                AuthorityInput(
                    name=("authenticated administrative proforma conversion command"),
                    owner="financial.invoice_draft_authoring",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed invoice identity plus actor, scope, reason, "
                        "correlation, and idempotency context"
                    ),
                ),
                AuthorityInput(
                    name="canonical customer account",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked Subscriber account row",
                ),
                AuthorityInput(
                    name="canonical invoice draft aggregate",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked Invoice row and active InvoiceLine rows with "
                        "owner-derived totals"
                    ),
                ),
                AuthorityInput(
                    name="canonical invoice tax rates",
                    owner="financial.tax_configuration",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="referenced TaxRate rows",
                ),
                AuthorityInput(
                    name="canonical invoice numbering policy",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="billing invoice number settings and sequence",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Create, update, or convert enters execute_owner_command "
                    "once on a transaction-free session; header, active lines, "
                    "totals or credit allocation, idempotency reservation, "
                    "audit, and outbox event commit or roll back together."
                ),
                locking=(
                    "The customer account is locked first, followed by the "
                    "invoice and its active lines. New drafts reserve the "
                    "account-scoped idempotency result in the same transaction. "
                    "Conversion uses the same account-then-invoice order before "
                    "deriving its final status from current allocation evidence."
                ),
                idempotency=(
                    "Create hashes the caller key and replays the same invoice "
                    "identifier. Update replaces the complete desired draft "
                    "state under locks, so an identical retry converges without "
                    "duplicating lines. Conversion durably binds its deterministic "
                    "key to one invoice and replays without another transition."
                ),
                retries=(
                    "Adapters retry only the whole command after rollback. "
                    "Stale, issued, cross-account, duplicate-number, or changed "
                    "line evidence fails closed."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.invoice_draft_authoring.account_not_found",
                    "financial.invoice_draft_authoring.currency_invalid",
                    "financial.invoice_draft_authoring.invoice_number_conflict",
                    "financial.invoice_draft_authoring.line_required",
                    "financial.invoice_draft_authoring.line_invalid",
                    "financial.invoice_draft_authoring.tax_rate_not_found",
                    "financial.invoice_draft_authoring.line_not_found",
                    "financial.invoice_draft_authoring.idempotency_conflict",
                    "financial.invoice_draft_authoring.invoice_not_found",
                    "financial.invoice_draft_authoring.account_mismatch",
                    "financial.invoice_draft_authoring.invoice_not_editable",
                    "financial.invoice_draft_authoring.invoice_not_proforma",
                    "financial.invoice_draft_authoring.prepaid_reconciliation_required",
                    "financial.invoice_draft_authoring.conversion_rejected",
                    "financial.invoice_draft_authoring.currency_mismatch",
                    "financial.invoice_draft_authoring.discount_actor_required",
                    "financial.invoice_draft_authoring.invalid_command_context",
                    "financial.invoice_draft_authoring.command_contract_violation",
                    "financial.invoice_draft_authoring.nested_owner_command",
                    "financial.invoice_draft_authoring.active_caller_transaction",
                    "financial.invoice_draft_authoring.nested_transaction_completion",
                ),
                mapping_owner="administrative billing web adapters",
                retryable_codes=(),
                fail_closed_on=(
                    "missing account, tax rate, or line evidence",
                    "empty draft or duplicate invoice number",
                    "non-draft, cross-account, or changed-currency update",
                    "non-proforma or concurrently changed conversion target",
                    "prepaid account or prepaid-linked proforma conversion",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=("invoice_created", "invoice_sent"),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 includes canonical invoice, account, status, "
                    "amount, due-date, currency, and proforma evidence."
                ),
                replay=(
                    "Draft-created and conversion events rebuild projections "
                    "and audit views; notification policy suppresses customer "
                    "delivery until explicit issue or send."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "administrative web form header commit followed by "
                    "independent invoice-line commits, plus unlocked web "
                    "proforma conversion from a stale invoice snapshot"
                ),
                new_owner="financial.invoice_draft_authoring",
                verification=(
                    "Atomic rollback, create and conversion replay, concurrent "
                    "payment/conversion status preservation, draft-only update, "
                    "proforma, event payload, adapter, manifest, and architecture "
                    "tests."
                ),
                cutover_gate=(
                    "Administrative create, edit, and proforma conversion "
                    "adapters invoke only the typed owner command on a "
                    "transaction-free session."
                ),
                fallback_retirement=(
                    "The web adapter no longer commits invoice headers or "
                    "iterates independent line create/update/delete writers, "
                    "and no longer converts from an unlocked stale snapshot."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/INVOICE_DRAFT_AUTHORING.md",
            ),
            test_refs=(
                "tests/test_invoice_draft_authoring.py",
                "tests/test_web_billing_invoice_forms.py",
                "tests/integration/test_proforma_conversion_concurrency.py",
                "tests/architecture/test_invoice_draft_authoring_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.advance_renewal_invoicing",
        module="app.services.advance_renewal_invoicing",
        owns=(
            "per-subscription advance renewal timer",
            "idempotent advance renewal invoice and notification request",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "auth.permission_gate",
            "communications.intents",
            "control.settings_spec",
            "events.dispatcher",
            "financial.billing_automation",
            "financial.invoices",
            "financial.prepaid_service_coverage",
            "financial.prepaid_service_renewals",
            "runtime.durable_timers",
        ),
        notes=(
            "The feature is disabled with no notice-day value until an "
            "operator explicitly configures both controls. Invoice issue "
            "time never becomes service-period start; the exact current "
            "coverage boundary owns the future period. Invoice creation "
            "does not advance next_billing_at."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="per-subscription advance renewal timer",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "explicit renewal notice configuration",
                        "canonical subscription lifecycle and billing anchor",
                    ),
                    canonical_writer="financial.advance_renewal_invoicing",
                ),
                ConcernContract(
                    name=(
                        "idempotent advance renewal invoice and notification request"
                    ),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "authenticated advance renewal command",
                        "explicit renewal notice configuration",
                        "canonical subscription lifecycle and billing anchor",
                        "authoritative prepaid coverage evidence",
                        "canonical recurring charge preview",
                        "canonical future-period invoice",
                    ),
                    canonical_writer="financial.advance_renewal_invoicing",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated advance renewal command",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "GenerateAdvanceRenewalInvoiceCommand with system "
                        "CommandContext and deterministic period identity"
                    ),
                ),
                AuthorityInput(
                    name="explicit renewal notice configuration",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "billing.renewal_invoice_notice_enabled and nullable "
                        "billing.renewal_invoice_notice_days"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscription lifecycle and billing anchor",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked active Subscription and next_billing_at projection",
                ),
                AuthorityInput(
                    name="authoritative prepaid coverage evidence",
                    owner="financial.prepaid_service_coverage",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="active funded entitlement or explicit service grant interval",
                ),
                AuthorityInput(
                    name="canonical recurring charge preview",
                    owner="financial.billing_automation",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="typed prepaid or postpaid exact-period charge preview",
                ),
                AuthorityInput(
                    name="canonical future-period invoice",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="issued Invoice and uniquely keyed subscription-period lines",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Each subscription command enters execute_owner_command once; "
                    "invoice, lines, audit, and event are staged atomically."
                ),
                locking=(
                    "Locks the subscription, rechecks the notice date, and relies "
                    "on unique active billing_line_key arbitration."
                ),
                idempotency=(
                    "Subscription, exact period, and component form the durable key; "
                    "matching replay returns the existing invoice."
                ),
                retries="The scheduler retries a complete owner command after rollback.",
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.advance_renewal_invoicing.configuration_unavailable",
                    "financial.advance_renewal_invoicing.coverage_ambiguous",
                    "financial.advance_renewal_invoicing.coverage_anchor_drift",
                    "financial.advance_renewal_invoicing.currency_conflict",
                    "financial.advance_renewal_invoicing.invoice_drift",
                    "financial.advance_renewal_invoicing.missing_renewal_boundary",
                    "financial.advance_renewal_invoicing.outside_notice_date",
                    "financial.advance_renewal_invoicing.period_drift",
                    "financial.advance_renewal_invoicing.subscription_not_eligible",
                    "financial.advance_renewal_invoicing.terminal_subscription",
                    *owner_command_boundary_error_codes(
                        "financial.advance_renewal_invoicing"
                    ),
                ),
                mapping_owner="billing lifecycle event adapter",
                fail_closed_on=(
                    "missing or invalid configuration",
                    "ambiguous coverage or anchor drift",
                    "future-period invoice conflict",
                ),
            ),
            events=EventContract(
                event_types=("subscription.renewal_invoice_ready",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Additive payload evolution within schema version 1.",
                replay="Communication intent deduplicates by event and channel.",
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="none; additive explicitly disabled capability",
                new_owner="financial.advance_renewal_invoicing",
                verification="Date, idempotency, notification, PDF, and architecture tests.",
                cutover_gate="Operator supplies notice days and explicitly enables it.",
                fallback_retirement="No implicit day or generic expiry-task fallback exists.",
            ),
            steward="billing operations",
            design_refs=(
                "docs/designs/ADVANCE_RENEWAL_INVOICING.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_advance_renewal_invoicing.py",
                "tests/architecture/test_advance_renewal_invoicing_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="financial.invoices",
        module="app.services.billing.invoices",
        owns=(
            "invoice document lifecycle",
            "atomic invoice and invoice-line construction",
            "invoice status transitions",
            "invoice adjustment and reversal postings",
            "automation invoice creation and draft issuance",
            "issued invoice due-date provenance and immutability",
            "automation invoice-line construction and source-fact replay",
            "usage-charge invoice and invoice-line construction",
            "overdue invoice state and observation event",
            "unfunded prepaid invoice return-to-draft eligibility",
            "invoice-originated ledger postings",
            "invoice receivable settlement summary",
            "invoice customer notification eligibility and request event",
            "invoice void eligibility preview and confirmation",
            "invoice write-off eligibility preview and confirmation",
            "exact invoice closure ledger evidence",
            "invoice closure idempotency and audit evidence",
            "historical invoice closure evidence reconciliation",
            "invoice settlement access-reconciliation handoff",
        ),
        depends_on=(
            "financial.ledger",
            "financial.billing_accounts",
            "financial.subscription_billing_grants",
            "financial.subscription_billing_treatments",
        ),
        notes=(
            "Native issuance accepts one typed InvoiceIssuanceInput containing "
            "the issue instant, due instant, DueDateBasis, source reference, "
            "and policy version. Unknown/unverified provenance is an explicit "
            "historical observation state: it remains reportable but cannot "
            "drive overdue or Collections. Legacy NULL provenance is a bounded "
            "migration state measured by billing health, not a valid new "
            "issuance outcome."
        ),
    ),
    SOTService(
        name="financial.credit_notes",
        module="app.services.billing.credit_notes",
        owns=(
            "credit-note lifecycle",
            "credit-note issuance and void preview/confirmation",
            "credit-note funding and void ledger evidence",
            "historical credit-note funding reconciliation",
            "credit-note application eligibility and preview",
            "credit-note application idempotency",
            "credit-note application-to-ledger evidence",
            "funded credit-note application consumption evidence",
            "credit-note application reversal preview and confirmation",
            "credit-note application reversal ledger evidence",
            "credit-note ledger-posting requests",
            "referral reward account credits",
        ),
        depends_on=("financial.ledger", "financial.invoices"),
        notes=(
            "Issuing a note that names an invoice applies it in the same "
            "transaction. A credit note reduces the receivable rather than "
            "holding a customer balance, so an issued note left unapplied "
            "overstates AR and keeps ageing and dunning a balance that is no "
            "longer owed. Manual application and application-on-issue share "
            "one flush-only staging participant so their evidence cannot "
            "drift, and the application key is derived from the issue command "
            "so replaying the issue replays the same application. A named "
            "paid invoice with zero receivable retains the value as account "
            "credit. An explicitly reversible owner workflow may also hold an "
            "open-invoice credit so its exact note remains voidable; that hold "
            "is fingerprinted as a typed disposition reason. Proforma, "
            "inactive, void, written-off, and open invoices without a positive "
            "receivable fail closed. Direct and draft issuance use the same "
            "typed disposition and staged participant."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="credit-note lifecycle",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed credit-note command",
                        "canonical credit-note document evidence",
                    ),
                    canonical_writer="financial.credit_notes",
                ),
                ConcernContract(
                    name="credit-note issuance and void preview/confirmation",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed credit-note command",
                        "canonical credit-note document evidence",
                        "canonical invoice receivable state",
                        "canonical ledger reversal protocol",
                    ),
                    canonical_writer="financial.credit_notes",
                ),
                ConcernContract(
                    name="credit-note funding and void ledger evidence",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed credit-note command",
                        "canonical credit-note document evidence",
                        "canonical ledger reversal protocol",
                    ),
                    canonical_writer="financial.credit_notes",
                ),
                ConcernContract(
                    name="historical credit-note funding reconciliation",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed credit-note command",
                        "canonical credit-note document evidence",
                        "canonical ledger reversal protocol",
                    ),
                    canonical_writer="financial.credit_notes",
                ),
                ConcernContract(
                    name="credit-note application eligibility and preview",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed credit-note command",
                        "canonical credit-note document evidence",
                        "canonical invoice receivable state",
                    ),
                    canonical_writer="financial.credit_notes",
                ),
                ConcernContract(
                    name="credit-note application idempotency",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed credit-note command",
                        "canonical credit-note application evidence",
                    ),
                    canonical_writer="financial.credit_notes",
                ),
                ConcernContract(
                    name="credit-note application-to-ledger evidence",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed credit-note command",
                        "canonical credit-note application evidence",
                        "canonical invoice receivable state",
                        "canonical ledger reversal protocol",
                    ),
                    canonical_writer="financial.credit_notes",
                ),
                ConcernContract(
                    name="funded credit-note application consumption evidence",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed credit-note command",
                        "canonical credit-note application evidence",
                        "canonical ledger reversal protocol",
                    ),
                    canonical_writer="financial.credit_notes",
                ),
                ConcernContract(
                    name="credit-note application reversal preview and confirmation",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "reviewed credit-note application reversal command",
                        "canonical credit-note application evidence",
                        "canonical invoice receivable state",
                        "canonical ledger reversal protocol",
                    ),
                    canonical_writer="financial.credit_notes",
                ),
                ConcernContract(
                    name="credit-note application reversal ledger evidence",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "reviewed credit-note application reversal command",
                        "canonical credit-note application evidence",
                        "canonical invoice receivable state",
                        "canonical ledger reversal protocol",
                    ),
                    canonical_writer="financial.credit_notes",
                ),
                ConcernContract(
                    name="credit-note ledger-posting requests",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed credit-note command",
                        "canonical credit-note document evidence",
                        "canonical ledger reversal protocol",
                    ),
                    canonical_writer="financial.credit_notes",
                ),
                ConcernContract(
                    name="referral reward account credits",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed credit-note command",
                        "canonical referral reward authorization",
                        "canonical credit-note document evidence",
                        "canonical ledger reversal protocol",
                    ),
                    canonical_writer="financial.credit_notes",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="typed credit-note command",
                    owner="financial.credit_notes",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed create, issue, draft-issue, void, application, "
                        "funding-reconciliation, system, and referral reward "
                        "commands accepted by the owner service"
                    ),
                ),
                AuthorityInput(
                    name="reviewed credit-note application reversal command",
                    owner="financial.credit_notes",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed application id, memo, preview fingerprint, and "
                        "idempotency key captured by the admin adapter"
                    ),
                ),
                AuthorityInput(
                    name="canonical credit-note application evidence",
                    owner="financial.credit_notes",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "credit-note application row, signed reversal row, "
                        "preview fingerprint, and funded-consumption evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical invoice receivable state",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked invoice aggregate and recomputed settlement "
                        "amounts after the credit application is reversed"
                    ),
                ),
                AuthorityInput(
                    name="canonical ledger reversal protocol",
                    owner="financial.ledger",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "ledger reversal entries linked by reversal_of_entry_id "
                        "to the original application and consumption entries"
                    ),
                ),
                AuthorityInput(
                    name="canonical credit-note document evidence",
                    owner="financial.credit_notes",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "credit-note row, lines, first issuance timestamp, "
                        "funding ledger link, void ledger link, preview "
                        "fingerprints, and audit evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical referral reward authorization",
                    owner="referrals.program",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "typed referral reward command and idempotency evidence "
                        "that authorizes account-credit issuance"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "CreditNotes.reverse_application_with_evidence owns the "
                    "lock, idempotency reservation, ledger reversals, signed "
                    "application evidence, recalculation, audit staging, and "
                    "commit."
                ),
                locking=(
                    "Locks the account, credit note, original application, and "
                    "invoice before confirming the preview fingerprint."
                ),
                idempotency=(
                    "credit_note_application_reversal scope binds the key to "
                    "the reversal application evidence and preview fingerprint."
                ),
                retries=(
                    "Replay returns the recorded reversal when the key and "
                    "fingerprint match; stale or conflicting evidence fails "
                    "closed."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes("financial.credit_notes"),
                    "financial.credit_notes.already_void",
                    "financial.credit_notes.application_already_reversed",
                    "financial.credit_notes.currency_mismatch",
                    "financial.credit_notes.funding_evidence_missing",
                    "financial.credit_notes.idempotency_conflict",
                    "financial.credit_notes.invoice_balance_exceeded",
                    "financial.credit_notes.invalid_application",
                    "financial.credit_notes.invalid_credit_note_state",
                    "financial.credit_notes.invalid_ledger_evidence",
                    "financial.credit_notes.invalid_preview_fingerprint",
                    "financial.credit_notes.invoice_not_found",
                    "financial.credit_notes.note_not_found",
                    "financial.credit_notes.ownership_mismatch",
                    "financial.credit_notes.referral_reward_invalid",
                    *owner_command_boundary_error_codes(
                        "financial.credit_notes.application_reversal"
                    ),
                ),
                mapping_owner="admin billing invoice adapter",
                fail_closed_on=(
                    "missing ledger evidence",
                    "stale preview fingerprint",
                    "already reversed application",
                    "invoice or credit-note ownership mismatch",
                ),
            ),
            events=EventContract(
                event_types=(
                    "credit_note.issued",
                    "credit_note.applied",
                    "credit_note.application_reversed",
                    "credit_note.voided",
                    "credit_note.funding_reconciled",
                    "credit_note.referral_reward_issued",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries bounded credit-note, invoice, ledger, "
                    "amount, currency, fingerprint, and idempotency evidence "
                    "without customer contact details or free-form documents."
                ),
                replay=(
                    "Owner-command idempotency and linked ledger/application "
                    "rows make replay observable without creating duplicate "
                    "credit documents, applications, reversals, or rewards."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                new_owner="financial.credit_notes",
                old_owner=(
                    "legacy billing credit-note CRUD, direct ledger postings, "
                    "manual referral account credits, and ad-hoc invoice "
                    "application updates"
                ),
                verification=(
                    "credit-note lifecycle, issue/apply/void/funding, referral "
                    "reward, reversal, and billing money action template tests"
                ),
                cutover_gate=(
                    "admin UI and API writes post only through typed "
                    "financial.credit_notes owner commands; reversal is gated "
                    "behind billing:invoice:update"
                ),
                fallback_retirement=(
                    "direct invoice, credit-note, referral-credit, and ledger "
                    "mutation paths are removed or retained only as adapters "
                    "around the owner command"
                ),
            ),
            steward="finance operations",
            design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
            test_refs=(
                "tests/test_credit_notes.py",
                "tests/test_billing_money_action_templates.py",
            ),
        ),
    ),
    SOTService(
        name="financial.tax_configuration",
        module="app.services.billing.tax",
        owns=("configurable tax-rate records", "tax-rate activation lifecycle"),
    ),
    SOTService(
        name="financial.billing_tax_resolution",
        module="app.services.billing_tax_resolution",
        owns=(
            "compatibility subscription VAT treatment policy",
            "bounded subscription VAT treatment resolution",
            "recorded-percent active tax-rate identity resolution",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "control.settings_spec",
            "customer.accounts",
            "financial.customer_tax_policies",
            "financial.tax_configuration",
            "service_intent.catalog_policy",
        ),
        notes=(
            "This read-only containment owner gives recurring, prepaid, and "
            "explicit subscription first-invoice billing one deterministic legacy "
            "VAT precedence while dotmac-tax adoption is "
            "in progress. Customer exemption wins before address, account, catalog, "
            "or configured defaults. Rate identity, percentage, and application "
            "come only from owned records and settings; no VAT code or percentage "
            "is built into a caller. It owns neither statutory tax policy nor "
            "custom-tax determination and is retired when dotmac-tax cuts over."
            " Its recorded-percent adapter returns an identity only when exactly "
            "one active TaxRate reproduces the supplied commercial percentage."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="compatibility subscription VAT treatment policy",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical subscription tax scope",
                        "canonical customer VAT exemption policy",
                        "active legacy tax-rate records",
                        "catalog compatibility VAT fields",
                        "configured compatibility VAT defaults",
                    ),
                ),
                ConcernContract(
                    name="bounded subscription VAT treatment resolution",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical subscription tax scope",
                        "canonical customer VAT exemption policy",
                        "active legacy tax-rate records",
                        "catalog compatibility VAT fields",
                        "configured compatibility VAT defaults",
                    ),
                ),
                ConcernContract(
                    name="recorded-percent active tax-rate identity resolution",
                    role=OwnerRole.RESOLVER,
                    input_names=("active legacy tax-rate records",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical subscription tax scope",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Subscription identity, account, service-address, and offer "
                        "references supplied in a bounded billing cohort"
                    ),
                ),
                AuthorityInput(
                    name="canonical customer VAT exemption policy",
                    owner="financial.customer_tax_policies",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "versioned CustomerTaxPolicy vat_exempt decision for the "
                        "canonical customer account"
                    ),
                ),
                AuthorityInput(
                    name="active legacy tax-rate records",
                    owner="financial.tax_configuration",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active TaxRate identifiers and percentages retained until "
                        "the dotmac-tax read switch"
                    ),
                ),
                AuthorityInput(
                    name="catalog compatibility VAT fields",
                    owner="service_intent.catalog_policy",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "CatalogOffer with_vat and vat_percent compatibility values "
                        "retained only for the staged tax migration"
                    ),
                ),
                AuthorityInput(
                    name="configured compatibility VAT defaults",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "database-authoritative billing default_tax_rate_id and "
                        "default_tax_application settings"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "The billing owner supplies the session and bounded cohort; "
                    "resolution reads canonical evidence without writes, flush, "
                    "commit, or rollback."
                ),
                locking=(
                    "No row lock is taken for a read projection. A money-writing "
                    "caller resolves again inside its current authoritative unit "
                    "of work."
                ),
                idempotency=(
                    "The same visible subscription scope, customer policy, active "
                    "rates, catalog compatibility values, and settings produce the "
                    "same typed result and provenance."
                ),
                retries=(
                    "Transient database reads may be retried; missing or inactive "
                    "legacy configuration remains an explicit unconfigured or "
                    "exempt result."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner="recurring and prepaid billing adapters",
                retryable_codes=(),
                fail_closed_on=(),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "duplicated or omitted VAT precedence in billing_automation, "
                    "prepaid_service_renewals, and the explicit subscription "
                    "first-invoice adapter"
                ),
                new_owner="financial.billing_tax_resolution",
                verification=(
                    "customer-exemption precedence, bounded query, recurring, "
                    "prepaid, explicit first-invoice, and architecture boundary tests"
                ),
                cutover_gate=(
                    "Recurring invoice, prepaid renewal, and explicit subscription "
                    "first-invoice paths consume the typed resolver and no longer "
                    "derive or omit VAT independently."
                ),
                fallback_retirement=(
                    "Caller-local address, account, catalog, and default VAT "
                    "helpers are removed; this compatibility owner itself retires "
                    "after dotmac-tax shadow parity and read cutover."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/PLAN_FAMILY_ARCHITECTURE.md",
            ),
            test_refs=(
                "tests/test_billing_tax_resolution.py",
                "tests/test_billing_automation_services.py",
                "tests/test_prepaid_threshold_resolver.py",
                "tests/test_web_catalog_subscriptions.py",
                "tests/architecture/test_billing_tax_resolution_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="financial.payment_proofs",
        module="app.services.payment_proofs",
        owns=(
            "payment-proof review lifecycle",
            "proof-backed payment request",
            "duplicate payment-proof correction lifecycle",
            "payment-proof reviewer notification request lifecycle",
        ),
        depends_on=(
            "auth.permission_gate",
            "customer.accounts",
            "financial.account_credit_deposits",
            "financial.billing_accounts",
            "financial.consolidated_payments",
            "financial.payments",
            "financial.tax_accounting",
            "financial.topup_intents",
            "communications.intents",
            "communications.notification_service",
            "communications.staff_notifications",
            "events.dispatcher",
            "observability.audit_log",
        ),
        notes=(
            "The proof owner records submitted transfer evidence and review "
            "state, then composes the canonical payment, WHT lifecycle, staff "
            "work-item, audit, customer-intent, and event participants in one "
            "owner-managed transaction. Customer-entered WHT is admitted only "
            "through a server-issued invoice intent snapshot; consolidated "
            "arbitrary credit fails closed for automatic WHT. Subscriber proof "
            "verification also rejects a reference already represented by an "
            "account Payment after taking the shared account lock. HTTP adapters "
            "only map typed results and domain errors."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="payment-proof review lifecycle",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "payment-proof command context",
                        "submitted transfer evidence",
                        "canonical payment-proof record",
                        "payment-proof lifecycle protocol",
                        "canonical direct-transfer top-up intent protocol",
                    ),
                    canonical_writer="financial.payment_proofs",
                ),
                ConcernContract(
                    name="proof-backed payment request",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "payment-proof command context",
                        "canonical payment-proof record",
                        "canonical subscriber account target",
                        "canonical reseller billing-account target",
                        "canonical subscriber payment settlement protocol",
                        "canonical consolidated settlement protocol",
                        "canonical deposit intent evidence",
                        "canonical withholding-tax recognition protocol",
                    ),
                    canonical_writer="financial.payment_proofs",
                ),
                ConcernContract(
                    name="duplicate payment-proof correction lifecycle",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "payment-proof command context",
                        "canonical payment-proof record",
                        "canonical duplicate-proof correction evidence",
                        "canonical subscriber payment reversal protocol",
                    ),
                    canonical_writer="financial.payment_proofs",
                ),
                ConcernContract(
                    name=("payment-proof reviewer notification request lifecycle"),
                    role=OwnerRole.EVENT_POLICY,
                    input_names=(
                        "canonical payment-proof record",
                        "canonical proof-review audience",
                        "payment-proof lifecycle protocol",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="payment-proof command context",
                    owner="financial.payment_proofs",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed command, correlation, actor, scope, reason, and "
                        "optional idempotency evidence supplied by the adapter"
                    ),
                ),
                AuthorityInput(
                    name="submitted transfer evidence",
                    owner="external:bank-transfer-submitter",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "receipt file, claimed net cash, currency, bank, reference, "
                        "transfer timestamp, and optional receipt-side evidence; WHT "
                        "money fields remain server-owned when admitted"
                    ),
                ),
                AuthorityInput(
                    name="canonical payment-proof record",
                    owner="financial.payment_proofs",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked PaymentProof identity, target, evidence, status, "
                        "review result, and resulting Payment link"
                    ),
                ),
                AuthorityInput(
                    name="canonical duplicate-proof correction evidence",
                    owner="financial.payment_proofs",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "append-only PaymentProofCorrection linking the duplicate "
                        "and retained original proofs to the exact payment reversal, "
                        "ledger evidence, actor, reason, preview fingerprint, and "
                        "idempotency key"
                    ),
                ),
                AuthorityInput(
                    name="payment-proof lifecycle protocol",
                    owner="financial.payment_proofs",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed submitted, verified, and rejected transitions, "
                        "proof/payment duplicate-reference policy, amount/WHT "
                        "validation, and versioned event vocabulary"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscriber account target",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "exact subscriber account selected by the authenticated "
                        "customer or reseller adapter"
                    ),
                ),
                AuthorityInput(
                    name="canonical reseller billing-account target",
                    owner="financial.billing_accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "exact reseller BillingAccount selected through the "
                        "canonical reseller/account ownership boundary"
                    ),
                ),
                AuthorityInput(
                    name="canonical direct-transfer top-up intent protocol",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "locked pending intent validation plus participant staging "
                        "of the submitted status, exact proof/configured-bank link, "
                        "terminal reviewed-proof completion/rejection projection, "
                        "versioned intent events, and immutable invoice WHT snapshot "
                        "metadata when present"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscriber payment settlement protocol",
                    owner="financial.payments",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "participant subscriber payment creation, allocation, "
                        "account locking, and settlement provenance contract"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscriber payment reversal protocol",
                    owner="financial.payments",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "locked reversal eligibility, exact preview fingerprint, "
                        "idempotent payment status transition, ledger evidence, "
                        "invoice consequences, audit, and funding-change event"
                    ),
                ),
                AuthorityInput(
                    name="canonical consolidated settlement protocol",
                    owner="financial.consolidated_payments",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "participant reseller billing-account settlement, locking, "
                        "allocation, idempotency, and provenance contract"
                    ),
                ),
                AuthorityInput(
                    name="canonical deposit intent evidence",
                    owner="financial.account_credit_deposits",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "TopupIntent account, reference, purpose, provider, and "
                        "settlement eligibility evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical withholding-tax recognition protocol",
                    owner="financial.tax_accounting",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "participant WHT receivable source creation and initial "
                        "official-timeline evidence staging contract"
                    ),
                ),
                AuthorityInput(
                    name="canonical proof-review audience",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "active staff principals granted billing:proof:verify, "
                        "resolved by communications.staff_notifications"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Each submit, verify, reject, or duplicate-correction command "
                    "starts on a clean "
                    "adapter session and commits proof state, any direct-transfer "
                    "intent link or terminal resolution, canonical payment, "
                    "tax-owner WHT source evidence, review work items, audit rows, "
                    "customer delivery intents, correction/reversal evidence, and "
                    "outbox events exactly once at the public owner boundary."
                ),
                locking=(
                    "Direct-transfer submission locks the exact TopupIntent before "
                    "creating proof evidence. Review commands select the "
                    "PaymentProof FOR UPDATE before rechecking submitted state, "
                    "then lock the credited subscriber or billing account through "
                    "its canonical settlement owner. Duplicate correction locks "
                    "both proof identities in UUID order before the payment owner "
                    "locks the subscriber and duplicate payment."
                ),
                idempotency=(
                    "A locked pending direct-transfer intent accepts one proof "
                    "link. A locked proof can leave submitted state once; payment and "
                    "consolidated-settlement provenance keys bind the resulting "
                    "money movement to the proof identity. Duplicate submitted "
                    "references remain explicit review evidence, not silent replay."
                    " One correction row is permitted per duplicate proof and per "
                    "payment reversal; a stable idempotency key replays only the "
                    "same proof pair and preview fingerprint."
                ),
                retries=(
                    "Adapters may retry only after a transient transaction failure. "
                    "A completed proof returns already_reviewed, while duplicate "
                    "transfer evidence and invalid decisions fail closed."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.payment_proofs.unsupported_file_type",
                    "financial.payment_proofs.file_too_large",
                    "financial.payment_proofs.empty_file",
                    "financial.payment_proofs.file_not_found",
                    "financial.payment_proofs.invalid_target",
                    "financial.payment_proofs.file_required",
                    "financial.payment_proofs.invalid_amount",
                    "financial.payment_proofs.amount_non_positive",
                    "financial.payment_proofs.invalid_withholding_tax",
                    "financial.payment_proofs.not_found",
                    "financial.payment_proofs.already_reviewed",
                    "financial.payment_proofs.invalid_verified_amount",
                    "financial.payment_proofs.verified_amount_non_positive",
                    "financial.payment_proofs.duplicate_transfer_reference",
                    "financial.payment_proofs.deposit_settlement_rejected",
                    "financial.payment_proofs.prepaid_funding_baseline_missing",
                    "financial.payment_proofs.billing_account_not_found",
                    "financial.payment_proofs.withholding_tax_basis_unavailable",
                    "financial.payment_proofs.verified_amount_conflict",
                    "financial.payment_proofs.verified_net_exceeds_gross",
                    "financial.payment_proofs.rejection_reason_required",
                    "financial.payment_proofs.correction_reason_required",
                    "financial.payment_proofs.correction_reason_too_long",
                    "financial.payment_proofs.correction_same_proof",
                    "financial.payment_proofs.correction_original_not_found",
                    "financial.payment_proofs.already_corrected",
                    "financial.payment_proofs.correction_original_was_corrected",
                    "financial.payment_proofs.correction_duplicate_not_verified",
                    "financial.payment_proofs.correction_original_not_verified",
                    "financial.payment_proofs.correction_unsupported_target",
                    "financial.payment_proofs.correction_account_mismatch",
                    "financial.payment_proofs.correction_currency_mismatch",
                    "financial.payment_proofs.correction_amount_mismatch",
                    "financial.payment_proofs.correction_payment_missing",
                    "financial.payment_proofs.correction_original_payment_inactive",
                    "financial.payment_proofs.correction_reversal_unavailable",
                    "financial.payment_proofs.correction_idempotency_key_required",
                    "financial.payment_proofs.correction_actor_required",
                    "financial.payment_proofs.correction_idempotency_conflict",
                    "financial.payment_proofs.correction_stale_preview",
                    "financial.payment_proofs.correction_reversal_evidence_missing",
                    "financial.payment_proofs.invalid_command_context",
                    "financial.payment_proofs.command_contract_violation",
                    "financial.payment_proofs.nested_owner_command",
                    "financial.payment_proofs.active_caller_transaction",
                    "financial.payment_proofs.nested_transaction_completion",
                ),
                mapping_owner="app.api.payment_proof_errors",
                retryable_codes=(),
                fail_closed_on=(
                    "missing or malformed transfer evidence",
                    "non-submitted or concurrently reviewed proof state",
                    "duplicate verified transfer references",
                    "customer-entered WHT for arbitrary customer or consolidated "
                    "bank-transfer proofs",
                    "missing or conflicting server-owned WHT snapshot values",
                    "failure to stage an eligible payment, tax source, review work "
                    "item, top-up intent link/resolution, audit, notification, or event "
                    "consequence",
                    "missing reviewed prepaid funding opening evidence for a "
                    "deposit-backed proof that would apply customer credit",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=(
                    "payment_proof.submitted",
                    "payment_proof.verified",
                    "payment_proof.rejected",
                    "payment_proof.corrected",
                    "withholding_tax.receivable_recorded",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Additive payload evolution within schema version 1; event "
                    "names and identifiers remain stable."
                ),
                replay=(
                    "The transactional event-store row is replayable by the event "
                    "dispatcher; replay never re-enters a proof command or reposts "
                    "the underlying payment."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "payment-proof helpers with direct commits, FastAPI exceptions, "
                    "request-shaped audit calls, best-effort nested savepoints, and "
                    "primitive dictionaries at the domain boundary"
                ),
                new_owner="financial.payment_proofs",
                verification=(
                    "Submission, direct-transfer intent linkage and terminal "
                    "resolution, duplicate, subscriber settlement, consolidated/WHT, "
                    "reviewer notification, duplicate-correction reversal evidence, "
                    "customer notification, route, locking, rollback, typed-result, "
                    "and architecture tests."
                ),
                cutover_gate=(
                    "Every API, admin web, customer portal, reseller portal, and "
                    "test caller supplies CommandContext on a clean session and "
                    "serializes only PaymentProofResult; direct transfer supplies a "
                    "typed intent/bank evidence command."
                ),
                fallback_retirement=(
                    "Service HTTPException inheritance, Request parameters, helper "
                    "commit/rollback, nested savepoints, swallowed audit/delivery "
                    "failures, and caller-visible primitive command dictionaries "
                    "are removed."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                "docs/designs/PAYMENT_PROOF_DUPLICATE_CORRECTION.md",
            ),
            test_refs=(
                "tests/test_payment_proofs.py",
                "tests/test_payment_proofs_reseller_wht.py",
                "tests/test_payment_proof_reviewer_notifications.py",
                "tests/test_reseller_proof_double_credit.py",
                "tests/test_payment_proof_admin_routes.py",
                "tests/test_payment_proof_duplicate_correction.py",
                "tests/architecture/test_payment_proof_reviewer_notification_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.manual_payment_recording",
        module="app.services.manual_payment_recording",
        owns=(
            "administrative manual-payment duplicate-risk preview",
            "locked administrative manual-payment confirmation",
        ),
        depends_on=(
            "customer.accounts",
            "events.dispatcher",
            "financial.payment_proofs",
            "financial.payments",
            "observability.audit_log",
        ),
        notes=(
            "This coordinator requires a unique account-scoped reference for a "
            "confirmed staff-entered payment, blocks an existing payment or "
            "submitted proof with that reference, and presents bounded same-amount "
            "payment/proof evidence for explicit acknowledgement. Confirmation "
            "locks the account, rebuilds both financial and duplicate-control "
            "previews, and delegates all money effects to financial.payments."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="administrative manual-payment duplicate-risk preview",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "administrative manual-payment command evidence",
                        "canonical subscriber account",
                        "canonical payment documents",
                        "canonical payment-proof records",
                    ),
                ),
                ConcernContract(
                    name="locked administrative manual-payment confirmation",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "administrative manual-payment command evidence",
                        "canonical subscriber account",
                        "canonical payment documents",
                        "canonical payment-proof records",
                        "canonical payment creation participant protocol",
                        "canonical audit protocol",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="administrative manual-payment command evidence",
                    owner="financial.manual_payment_recording",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed account, amount, currency, status, payment reference, "
                        "financial preview fingerprint, duplicate-control fingerprint, "
                        "risk acknowledgement, actor, scope, reason, and idempotency"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscriber account",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked Subscriber account identity",
                ),
                AuthorityInput(
                    name="canonical payment documents",
                    owner="financial.payments",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "account-scoped Payment identity, amount, currency, status, "
                        "reference, settlement timestamp, and active lifecycle"
                    ),
                ),
                AuthorityInput(
                    name="canonical payment-proof records",
                    owner="financial.payment_proofs",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "account-scoped submitted PaymentProof identity, claimed "
                        "amount, currency, reference, and transfer timestamp"
                    ),
                ),
                AuthorityInput(
                    name="canonical payment creation participant protocol",
                    owner="financial.payments",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only locked payment preview, idempotency reservation, "
                        "settlement, ledger, audit, and payment.received event contract"
                    ),
                ),
                AuthorityInput(
                    name="canonical audit protocol",
                    owner="observability.audit_log",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="transactional staff decision and duplicate-risk evidence",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Confirmation enters one owner command on a transaction-free "
                    "adapter session and commits the payment, settlement, ledger, "
                    "duplicate-control audit, and outbox events atomically."
                ),
                locking=(
                    "The Subscriber account is locked before duplicate payment and "
                    "proof evidence is rebuilt; the payment participant follows the "
                    "same account-first order before reserving idempotency."
                ),
                idempotency=(
                    "The payment-creation key binds one exact request and result. "
                    "An exact replay returns that payment without re-evaluating it as "
                    "a duplicate or staging another audit/event."
                ),
                retries=(
                    "Exact replay is safe. Stale financial or duplicate evidence "
                    "requires a new preview; reference conflicts and missing "
                    "acknowledgement fail closed."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.manual_payment_recording.reference_required",
                    "financial.manual_payment_recording.reference_already_recorded",
                    "financial.manual_payment_recording.reference_has_submitted_proof",
                    "financial.manual_payment_recording.invalid_scope",
                    "financial.manual_payment_recording.idempotency_conflict",
                    "financial.manual_payment_recording.stale_payment_preview",
                    "financial.manual_payment_recording.stale_duplicate_evidence",
                    "financial.manual_payment_recording.duplicate_risk_acknowledgement_required",
                    *owner_command_boundary_error_codes(
                        "financial.manual_payment_recording"
                    ),
                ),
                mapping_owner="admin billing web and API adapters",
                retryable_codes=(),
                fail_closed_on=(
                    "missing confirmed-payment reference",
                    "existing same-account reference",
                    "submitted proof carrying the same reference",
                    "stale financial or duplicate evidence",
                    "unacknowledged same-amount payment or proof evidence",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=("manual_payment.recorded",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries payment/account identities, control "
                    "fingerprint, acknowledgement state, and bounded evidence IDs "
                    "without bank references or customer identity data."
                ),
                replay=(
                    "Payment idempotency returns the existing result and emits no "
                    "second manual-payment event."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "generic admin payment form with a sixty-second amount-only "
                    "double-click guard and no payment-proof awareness"
                ),
                new_owner="financial.manual_payment_recording",
                verification=(
                    "reference, payment collision, submitted-proof, same-amount "
                    "warning, acknowledgement, stale-preview, replay, route, "
                    "template, registry, and architecture tests"
                ),
                cutover_gate=(
                    "Admin web and payment-creation API preview/confirm adapters use "
                    "only this coordinator for confirmed manual settlement."
                ),
                fallback_retirement=(
                    "The admin form no longer calls generic payment confirmation or "
                    "relies on the sixty-second amount-only check as its control."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/designs/MANUAL_PAYMENT_DUPLICATE_CONTROL.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
            ),
            test_refs=(
                "tests/test_manual_payment_recording.py",
                "tests/test_billing_money_action_templates.py",
                "tests/architecture/test_manual_payment_recording_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="financial.tax_accounting",
        module="app.services.tax_accounting",
        owns=(
            "tax report semantics",
            "output-tax invoice projection",
            "withholding-tax receivable projection",
            "tax report period and currency aggregation",
            "credit-note tax recognition point",
            "withholding-tax receivable source records",
            "withholding-tax lifecycle",
            "withholding-tax official timeline",
            "net output-tax liability projection",
        ),
        depends_on=(
            "events.dispatcher",
            "financial.credit_notes",
            "financial.invoices",
            "financial.payments",
            "financial.tax_configuration",
            "observability.audit_log",
        ),
        notes=(
            "Issued output tax less issued credit-note tax adjustments is "
            "the source-document liability, not cash collected, and "
            "currencies remain separate. This owner also enforces legal "
            "pending/certified/reclaimed/written-off WHT transitions and an "
            "immutable evidence timeline. Dotmac ERP exclusively owns tax "
            "account mappings, balanced journals, tax transactions, and "
            "financial statements; Sub exports line tax treatment and WHT "
            "facts through bounded sync feeds and has no local posting path."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="tax report semantics",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical invoice tax source documents",
                        "canonical credit-note tax source documents",
                        "canonical WHT source records",
                        "canonical tax-application configuration",
                    ),
                ),
                ConcernContract(
                    name="output-tax invoice projection",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical invoice tax source documents",
                        "canonical tax-application configuration",
                    ),
                ),
                ConcernContract(
                    name="withholding-tax receivable projection",
                    role=OwnerRole.RESOLVER,
                    input_names=("canonical WHT source records",),
                ),
                ConcernContract(
                    name="tax report period and currency aggregation",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "typed tax report filter",
                        "canonical invoice tax source documents",
                        "canonical credit-note tax source documents",
                        "canonical WHT source records",
                    ),
                ),
                ConcernContract(
                    name="credit-note tax recognition point",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical credit-note tax source documents",
                        "canonical tax-application configuration",
                    ),
                ),
                ConcernContract(
                    name="withholding-tax receivable source records",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "verified proof-backed WHT evidence",
                        "canonical payment settlement evidence",
                        "WHT command context",
                    ),
                    canonical_writer="financial.tax_accounting",
                ),
                ConcernContract(
                    name="withholding-tax lifecycle",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical WHT source records",
                        "WHT lifecycle protocol",
                        "WHT command context",
                    ),
                    canonical_writer="financial.tax_accounting",
                ),
                ConcernContract(
                    name="withholding-tax official timeline",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical WHT source records",
                        "WHT lifecycle protocol",
                        "WHT command context",
                    ),
                    canonical_writer="financial.tax_accounting",
                ),
                ConcernContract(
                    name="net output-tax liability projection",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical invoice tax source documents",
                        "canonical credit-note tax source documents",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical invoice tax source documents",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active non-proforma invoice issue timestamp, lifecycle, "
                        "currency, tax total, and gross total"
                    ),
                ),
                AuthorityInput(
                    name="canonical credit-note tax source documents",
                    owner="financial.credit_notes",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active issued credit-note recognition timestamp, lifecycle, "
                        "currency, tax adjustment, and gross credit"
                    ),
                ),
                AuthorityInput(
                    name="verified proof-backed WHT evidence",
                    owner="financial.payment_proofs",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "typed subscriber-or-billing-account target, reseller, "
                        "payment, proof, authoritative gross, net cash, WHT, VAT-"
                        "exclusive basis, VAT, source invoice, policy version, "
                        "currency, actor, and correlation evidence admitted by the "
                        "payment-proof coordinator"
                    ),
                ),
                AuthorityInput(
                    name="canonical payment settlement evidence",
                    owner="financial.payments",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "canonical settled Payment identity linked to the proof-backed "
                        "WHT receivable and ERP sync freshness marker"
                    ),
                ),
                AuthorityInput(
                    name="canonical WHT source records",
                    owner="financial.tax_accounting",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked WithholdingTaxRecord amount, currency, source links, "
                        "status, certificate, and temporal evidence"
                    ),
                ),
                AuthorityInput(
                    name="WHT lifecycle protocol",
                    owner="financial.tax_accounting",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "pending/certified/reclaimed/written-off transition graph, "
                        "certificate requirement, write-off reason, replay, and append-"
                        "only timeline rules"
                    ),
                ),
                AuthorityInput(
                    name="WHT command context",
                    owner="financial.tax_accounting",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed command, actor, scope, reason, idempotency, command, "
                        "correlation, and causation evidence"
                    ),
                ),
                AuthorityInput(
                    name="typed tax report filter",
                    owner="financial.tax_accounting",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "validated inclusive report date range, WHT lifecycle filter, "
                        "search text, and bounded pagination"
                    ),
                ),
                AuthorityInput(
                    name="canonical tax-application configuration",
                    owner="financial.tax_configuration",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "canonical source-document tax application and recognition "
                        "classification; ERP account mappings remain external"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "transition_withholding_tax enters execute_owner_command once on a "
                    "transaction-free session and atomically commits the locked record, "
                    "append-only timeline, payment freshness marker, audit, and event. "
                    "Proof-backed receivable creation is a flush-only participant of "
                    "financial.payment_proofs; report and operator queries are read-only."
                ),
                locking=(
                    "Lifecycle transitions lock the exact WHT record first and its linked "
                    "Payment second. Proof-backed creation is serialized by the payment "
                    "owner and the unique payment-to-WHT constraint."
                ),
                idempotency=(
                    "Receivable creation replays only exact evidence for the unique "
                    "Payment. Lifecycle replay returns the existing target state and "
                    "rejects conflicting certificate evidence."
                ),
                retries=(
                    "Adapters retry the complete command with the same record, target, "
                    "evidence, and context. Participant creation retries only with its "
                    "wider payment-proof command and never commits independently."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.tax_accounting.actor_required",
                    "financial.tax_accounting.certificate_required",
                    "financial.tax_accounting.certification_required",
                    "financial.tax_accounting.currency_invalid",
                    "financial.tax_accounting.date_filter_invalid",
                    "financial.tax_accounting.filter_invalid",
                    "financial.tax_accounting.illegal_transition",
                    "financial.tax_accounting.pagination_invalid",
                    "financial.tax_accounting.receivable_conflict",
                    "financial.tax_accounting.receivable_invalid",
                    "financial.tax_accounting.record_id_invalid",
                    "financial.tax_accounting.record_not_found",
                    "financial.tax_accounting.replay_conflict",
                    "financial.tax_accounting.target_status_invalid",
                    "financial.tax_accounting.write_off_reason_required",
                    "financial.tax_accounting.invalid_command_context",
                    "financial.tax_accounting.command_contract_violation",
                    "financial.tax_accounting.nested_owner_command",
                    "financial.tax_accounting.active_caller_transaction",
                    "financial.tax_accounting.nested_transaction_completion",
                ),
                mapping_owner="tax report and admin billing web adapters",
                retryable_codes=(),
                fail_closed_on=(
                    "missing or malformed currency, date, filter, page, actor, or record "
                    "identity",
                    "non-positive, dual-target, or unreconciled gross/net/WHT source "
                    "evidence",
                    "illegal, unexplained, uncertified, or conflicting lifecycle evidence",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=(
                    "withholding_tax.receivable_recorded",
                    "withholding_tax.status_changed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries canonical WHT, payment/proof/account, lifecycle, "
                    "money, currency, time, command, correlation, and causation evidence "
                    "without certificate content or customer PII."
                ),
                replay=(
                    "Events project committed WHT facts and ERP sync eligibility only; "
                    "they never create money or re-enter a lifecycle command."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="output-tax invoice projection",
                    input_names=(
                        "canonical invoice tax source documents",
                        "canonical tax-application configuration",
                    ),
                    writer="financial.tax_accounting",
                    freshness="computed from committed source documents on every query",
                    stale_behavior=(
                        "invalid dates, currencies, or source evidence fail closed"
                    ),
                    drift_signal=(
                        "report totals or row counts disagree with the same bounded "
                        "invoice-source query"
                    ),
                    rebuild_operation="build_tax_report recomputes the full bounded view",
                    repair_owner="financial.tax_accounting",
                ),
                ProjectionContract(
                    name="withholding-tax receivable projection",
                    input_names=("canonical WHT source records",),
                    writer="financial.tax_accounting",
                    freshness="computed from committed WHT records on every query",
                    stale_behavior=(
                        "unknown lifecycle or malformed money/currency evidence fails closed"
                    ),
                    drift_signal=(
                        "projection count or currency/status totals disagree with the "
                        "canonical WHT source query"
                    ),
                    rebuild_operation="build_tax_report recomputes WHT rows and totals",
                    repair_owner="financial.tax_accounting",
                ),
                ProjectionContract(
                    name="net output-tax liability projection",
                    input_names=(
                        "canonical invoice tax source documents",
                        "canonical credit-note tax source documents",
                    ),
                    writer="financial.tax_accounting",
                    freshness="computed per currency from committed source documents",
                    stale_behavior="currencies remain separate and invalid evidence fails",
                    drift_signal=(
                        "net liability differs from invoice output tax less recognized "
                        "credit-note tax adjustments for the same currency and period"
                    ),
                    rebuild_operation="build_tax_report recomputes per-currency liability",
                    repair_owner="financial.tax_accounting",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "tax report and admin route dictionaries plus a WHT helper with a "
                    "caller-selected commit flag and route-owned audit"
                ),
                new_owner="financial.tax_accounting",
                verification=(
                    "Typed report/operator outcomes, participant creation, transition, "
                    "replay, rollback, audit/event, PostgreSQL row-lock concurrency, "
                    "manifest, and adapter-boundary tests."
                ),
                cutover_gate=(
                    "The admin adapter submits only a typed command on a transaction-free "
                    "session; proof review uses only typed flush-only WHT staging."
                ),
                fallback_retirement=(
                    "The transition commit flag, route rollback/audit, primitive report "
                    "bags, public lifecycle initializer, and duplicate WHT event staging "
                    "are absent."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_tax_accounting.py",
                "tests/test_payment_proofs_reseller_wht.py",
                "tests/integration/test_tax_accounting_concurrency.py",
                "tests/architecture/test_tax_accounting_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.billing_profile",
        module="app.services.billing_profile",
        owns=(
            "prepaid/postpaid profile resolution",
            "billing-mode transition policy",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "customer.accounts",
            "service_intent.catalog_policy",
        ),
        notes=(
            "Resolves account and collectible-subscription evidence into one "
            "typed billing profile. Missing, mixed, or contradictory evidence "
            "is explicit; callers do not guess from local defaults."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="prepaid/postpaid profile resolution",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical account billing mode",
                        "canonical collectible subscription billing modes",
                        "billing profile protocol",
                    ),
                ),
                ConcernContract(
                    name="billing-mode transition policy",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical account billing mode",
                        "canonical collectible subscription billing modes",
                        "canonical offer billing mode",
                        "billing profile protocol",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical account billing mode",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Subscriber billing_mode captured on the account",
                ),
                AuthorityInput(
                    name="canonical collectible subscription billing modes",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Subscription billing_mode values in the canonical "
                        "collectible lifecycle-status set"
                    ),
                ),
                AuthorityInput(
                    name="canonical offer billing mode",
                    owner="service_intent.catalog_policy",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="CatalogOffer billing_mode for the requested service",
                ),
                AuthorityInput(
                    name="billing profile protocol",
                    owner="financial.billing_profile",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed source and reason vocabulary, collectible status "
                        "semantics, and deterministic mismatch precedence"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Caller creates and closes the session; resolution and policy "
                    "read canonical billing evidence without writes or transaction "
                    "completion."
                ),
                locking=(
                    "No row lock for read projections. A command or remediation "
                    "caller re-resolves against its locked/current source records "
                    "before applying a billing-mode transition."
                ),
                idempotency=(
                    "The same visible account, collectible subscription, offer, "
                    "and requested-mode evidence produces the same typed outcome."
                ),
                retries=(
                    "Transient reads may be retried. Missing, mixed, or conflicting "
                    "billing evidence remains a deterministic domain failure."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.billing_profile.account_billing_mode_missing",
                    "financial.billing_profile.account_offer_billing_mode_mismatch",
                    "financial.billing_profile.billing_mode_unresolved",
                    "financial.billing_profile.mixed_collectible_subscription_billing_modes",
                    "financial.billing_profile.offer_not_found",
                    "financial.billing_profile.requested_billing_mode_mismatch",
                    "financial.billing_profile.subscriber_not_found",
                ),
                mapping_owner=(
                    "catalog, account, cleanup, collections, and reporting adapters"
                ),
                fail_closed_on=(
                    "missing account or offer evidence",
                    "mixed collectible subscription modes",
                    "missing canonical account mode",
                    "account, subscription, offer, or requested-mode contradiction",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "primitive string outcomes, ValueError failures, caller-local "
                    "billing-mode fallback, and cleanup-specific mode comparison"
                ),
                new_owner="financial.billing_profile",
                verification=(
                    "Resolution, mixed-mode, missing-mode, transition, catalog-write, "
                    "cleanup revalidation, grace-policy, and architecture tests."
                ),
                cutover_gate=(
                    "Account, catalog, cleanup, collections, access, and reporting "
                    "callers consume the canonical typed profile or transition."
                ),
                fallback_retirement=(
                    "Raw string reasons, generic ValueError boundary failures, "
                    "caller-local grace defaults, and duplicated cleanup mode-set "
                    "decisions are removed."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/audits/BILLING_SOT_AUDIT_2026-07-12.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_billing_profile.py",
                "tests/test_shared_policy_services.py",
                "tests/test_billing_cleanup_remediation.py",
                "tests/architecture/test_billing_profile_boundary.py",
            ),
        ),
    ),
)
