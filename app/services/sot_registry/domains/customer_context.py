"""Canonical SOT declarations for the customer_context domain."""

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
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="customer_context",
    services=(
        SOTService(
            name="customer.accounts",
            module="app.services.subscriber",
            owns=(
                "Subscriber account creation",
                "transaction-neutral Subscriber account initialization",
                "Reseller record creation",
                "transaction-neutral Reseller record initialization",
            ),
            depends_on=(
                "access.subscription_lifecycle",
                "events.dispatcher",
            ),
            notes=(
                "Cross-domain coordinators may prepare an account through "
                "this owner, but new/cut-over callers must not construct "
                "Subscriber or Reseller rows or decide account lifecycle "
                "state themselves. "
                "Existing direct writers remain shrink-only migration debt."
            ),
        ),
        SOTService(
            name="customer.account_visibility",
            module="app.services.customer_account_visibility",
            owns=("legacy imported Subscriber deletion classification",),
            depends_on=(
                "customer.accounts",
                "access.subscription_lifecycle",
            ),
            notes=(
                "An explicit retained splynx_deleted value is authoritative for "
                "legacy import deletion classification. The canceled/inactive "
                "historical-status compatibility inference runs only when that "
                "value is absent or unrecognized; historical Splynx status never "
                "overrides canonical current lifecycle state."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=("legacy imported Subscriber deletion classification"),
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical Subscriber account record",
                            "canonical Subscriber lifecycle projection",
                            "retained Splynx deletion observation",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical Subscriber account record",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "canonical Subscriber identity and retained legacy "
                            "system provenance"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Subscriber lifecycle projection",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "Subscriber status and active flag projected from "
                            "canonical subscription lifecycle state"
                        ),
                    ),
                    AuthorityInput(
                        name="retained Splynx deletion observation",
                        owner="external:splynx_import",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "splynx_deleted and historical splynx_status values "
                            "retained in Subscriber metadata at migration"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Callers own the session; object and SQL classifiers read "
                        "committed Subscriber facts without mutation or transaction "
                        "completion."
                    ),
                    locking=(
                        "No read locks are required because the classifier does not "
                        "write lifecycle or provenance state."
                    ),
                    idempotency=(
                        "The same account, lifecycle, and retained import evidence "
                        "produce the same deletion classification."
                    ),
                    retries="Read-only classification is safe to retry.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner=(
                        "customer list, reporting, and subscriber query adapters"
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "inline legacy Splynx deletion inference in customer list "
                        "and reporting callers"
                    ),
                    new_owner="customer.account_visibility",
                    verification=(
                        "Object/SQL classifier parity and customer-list visibility "
                        "regression tests"
                    ),
                    cutover_gate=(
                        "All imported-customer visibility consumers use the shared "
                        "classifier and explicit false evidence wins"
                    ),
                    fallback_retirement=(
                        "Only absent or unrecognized deletion observations retain "
                        "the compatibility inference until those imports are "
                        "adjudicated or backfilled"
                    ),
                ),
                steward="customer operations",
                design_refs=(
                    "docs/designs/SPLYNX_RETIREMENT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_subscriber_splynx_soft_delete.py",
                    "tests/test_web_customer_lists.py",
                ),
            ),
        ),
        SOTService(
            name="customer.crm_subscriber_provisioning",
            module="app.services.crm_subscriber_provisioning",
            owns=("authenticated CRM Subscriber provisioning coordination",),
            depends_on=(
                "customer.accounts",
                "observability.audit_log",
                "events.dispatcher",
            ),
            notes=(
                "A separately authenticated CRM command may request one "
                "canonical Subscriber account. The verified CRM customer "
                "webhook remains observation-only and cannot invoke this owner."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=("authenticated CRM Subscriber provisioning coordination"),
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authenticated CRM provisioning command evidence",
                            "retained exact CRM Subscriber provenance",
                            "canonical Subscriber account state",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name=("authenticated CRM provisioning command evidence"),
                        owner="customer.crm_subscriber_provisioning",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "scoped CRM API key, typed customer payload, stable "
                            "Idempotency-Key, actor, command, and correlation "
                            "evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="retained exact CRM Subscriber provenance",
                        owner="customer.crm_subscriber_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "allowlisted crm_person_id, crm_sales_order_id, and "
                            "crm_quote_id retained on the canonical Subscriber"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Subscriber account state",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "locked existing Subscriber or transaction-neutral "
                            "canonical account initialization"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "The command enters execute_owner_command once on a "
                        "transaction-free API session. Exact-provenance reuse or "
                        "canonical account initialization, idempotency evidence, "
                        "audit, and subscriber.created commit or roll back together."
                    ),
                    locking=(
                        "A transaction-scoped advisory lock serializes the exact "
                        "idempotency key; the reservation and any matched Subscriber "
                        "are selected FOR UPDATE. Unique scope/key evidence and "
                        "canonical account constraints arbitrate concurrent winners."
                    ),
                    idempotency=(
                        "A mandatory caller key is fingerprinted against every "
                        "material command field. Exact replay returns the original "
                        "Subscriber; changed evidence fails closed."
                    ),
                    retries=(
                        "CRM may retry transient transport or database failures with "
                        "the same key and payload. Identity ambiguity, changed "
                        "evidence, and canonical conflicts require review."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        ("customer.crm_subscriber_provisioning.invalid_command"),
                        (
                            "customer.crm_subscriber_provisioning."
                            "missing_idempotency_key"
                        ),
                        ("customer.crm_subscriber_provisioning.idempotency_conflict"),
                        ("customer.crm_subscriber_provisioning.ambiguous_identity"),
                        ("customer.crm_subscriber_provisioning.identity_conflict"),
                        (
                            "customer.crm_subscriber_provisioning."
                            "invalid_command_context"
                        ),
                        (
                            "customer.crm_subscriber_provisioning."
                            "command_contract_violation"
                        ),
                        ("customer.crm_subscriber_provisioning.nested_owner_command"),
                        (
                            "customer.crm_subscriber_provisioning."
                            "active_caller_transaction"
                        ),
                        (
                            "customer.crm_subscriber_provisioning."
                            "nested_transaction_completion"
                        ),
                    ),
                    mapping_owner="app.api.crm adapter",
                    fail_closed_on=(
                        "missing or changed idempotency evidence",
                        "ambiguous retained CRM provenance",
                        "conflicting canonical customer state",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=("subscriber.created",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "The existing PII-free subscriber.created schema remains "
                        "authoritative; CRM command evidence is retained in audit "
                        "and idempotency records."
                    ),
                    replay=(
                        "Command replay returns the reserved Subscriber without "
                        "emitting another event or audit record."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUTOVER_READY,
                    old_owner=(
                        "CRM treating the observation-only customer.accepted "
                        "webhook as an account-creation command"
                    ),
                    new_owner="customer.crm_subscriber_provisioning",
                    verification=(
                        "Focused command, API, idempotency, concurrency, audit, "
                        "event, and observation-boundary tests."
                    ),
                    cutover_gate=(
                        "Dotmac CRM calls only the authenticated command endpoint "
                        "for creation and retains the returned canonical identity."
                    ),
                    fallback_retirement=(
                        "CRM no longer interprets an observation consequence as a "
                        "customer-creation result."
                    ),
                ),
                steward="customer operations",
                design_refs=(
                    "docs/PARTY_CUSTOMER_LIFECYCLE.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/CODING_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_crm_subscriber_provisioning.py",
                    "tests/test_crm_api.py",
                    "tests/architecture/test_crm_customer_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="customer.billing_approval",
            module="app.services.account_billing_approval",
            owns=(
                "atomic account billing-approval and lifecycle transition",
                "account billing-approval drift reconciliation",
            ),
            depends_on=(
                "customer.accounts",
                "access.subscription_lifecycle",
                "financial.subscription_billing_treatments",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "Subscriber.billing_enabled is an activation-admission fact, "
                "not an independent runtime switch. Revocation disables "
                "non-terminal service through access.subscription_lifecycle; "
                "re-approval restores only a disable created by this owner. "
                "Explicit billing treatments, not this flag, own complimentary "
                "or sponsored service."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "atomic account billing-approval and lifecycle transition"
                        ),
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "account billing-approval command evidence",
                            "canonical account billing-approval fact",
                            "canonical account lifecycle state",
                            "canonical subscription lifecycle state",
                        ),
                    ),
                    ConcernContract(
                        name="account billing-approval drift reconciliation",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "account billing-approval command evidence",
                            "canonical account billing-approval fact",
                            "canonical account lifecycle state",
                            "canonical subscription lifecycle state",
                            "effective subscription billing treatment",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="account billing-approval command evidence",
                        owner="customer.billing_approval",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed approval, actor, scope, reason, command, "
                            "correlation, and idempotency context"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical account billing-approval fact",
                        owner="customer.billing_approval",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="locked Subscriber.billing_enabled",
                    ),
                    AuthorityInput(
                        name="canonical account lifecycle state",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "locked Subscriber lifecycle override and derived "
                            "account status"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical subscription lifecycle state",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="locked Subscription.status rows",
                    ),
                    AuthorityInput(
                        name="effective subscription billing treatment",
                        owner="financial.subscription_billing_treatments",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "effective, evidence-bound complimentary or sponsored "
                            "billing-treatment decision"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "Each approval change or per-account repair enters "
                        "execute_owner_command once; the approval fact, account "
                        "and subscription transitions, audit, and event evidence "
                        "commit or roll back together."
                    ),
                    locking=(
                        "The Subscriber locks first, followed by its Subscription "
                        "rows in stable UUID order; lifecycle participants reuse "
                        "those locks."
                    ),
                    idempotency=(
                        "An already aligned approval and lifecycle state returns "
                        "unchanged; a billing-owned disable carries an explicit "
                        "source so only its matching re-approval can restore it."
                    ),
                    retries=(
                        "Adapters retry transient transaction failures with the "
                        "same command evidence. Missing accounts, invalid scope, "
                        "and unrelated lifecycle disables fail closed."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "customer.billing_approval.active_caller_transaction",
                        "customer.billing_approval.command_contract_violation",
                        "customer.billing_approval.invalid_command_context",
                        "customer.billing_approval.nested_owner_command",
                        "customer.billing_approval.nested_transaction_completion",
                        "customer.billing_approval.account_not_found",
                        "customer.billing_approval.invalid_scope",
                        "customer.billing_approval.invalid_reason",
                    ),
                    mapping_owner="subscriber API, admin web, and task adapters",
                    fail_closed_on=(
                        "missing account",
                        "missing billing-approval scope or reason",
                        "active unapproved service without an effective treatment",
                        "unrelated administrative lifecycle disable",
                    ),
                ),
                events=EventContract(
                    event_types=("subscriber.billing_approval_changed",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Additive payload fields only within schema version 1."
                    ),
                    replay=(
                        "Replay consumes the committed approval and lifecycle "
                        "outcome; it never re-decides or rewrites source state."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="account billing-approval alignment",
                        input_names=(
                            "canonical account billing-approval fact",
                            "canonical subscription lifecycle state",
                            "effective subscription billing treatment",
                        ),
                        writer="customer.billing_approval",
                        freshness="Reconciled every fifteen minutes.",
                        stale_behavior=(
                            "An unapproved active service is fail-safe drift: an "
                            "effective treatment repairs redundant approval to true; "
                            "otherwise the account is disabled."
                        ),
                        drift_signal=(
                            "Subscriber.billing_enabled=false joined to an active "
                            "Subscription."
                        ),
                        rebuild_operation=(
                            "reconcile_account_billing_approval for the bounded "
                            "drift cohort"
                        ),
                        repair_owner="customer.billing_approval",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "generic Subscriber updates, customer profile forms, and "
                        "bulk actions writing billing_enabled independently"
                    ),
                    new_owner="customer.billing_approval",
                    verification=(
                        "Owner command, activation guard, adapter, reconciliation, "
                        "scheduler permanence, and architecture tests."
                    ),
                    cutover_gate=(
                        "No active account can be excluded from billing without an "
                        "effective treatment or a disabled lifecycle state."
                    ),
                    fallback_retirement=(
                        "Raw update writers and active/unapproved runtime fallback "
                        "are removed."
                    ),
                ),
                steward="customer and billing operations",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                    "docs/adr/0003-permanent-customer-financial-lifecycle.md",
                ),
                test_refs=(
                    "tests/test_account_billing_approval.py",
                    "tests/architecture/test_account_billing_approval_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="customer.identity_scope",
            module="app.services.customer_context",
            owns=(
                "portal/customer principal resolution",
                "allowed account/subscriber scope",
                "customer ownership checks",
            ),
        ),
        SOTService(
            name="customer.profile_commands",
            module="app.services.web_customer_actions",
            owns=(
                "admin customer profile edits",
                "person-to-business customer conversion",
                "approved legacy Subscriber name corrections",
            ),
            depends_on=("customer.identity_scope",),
            notes=(
                "Business conversion is an explicit command. Generic "
                "person edits and form category controls must not change "
                "the customer account type. Approved legacy Subscriber "
                "name corrections remain here until explicit Party cutover."
            ),
        ),
        SOTService(
            name="customer.name_remediation",
            module="app.services.crm_customer_name_repair",
            owns=(
                "July 20 CRM name remediation manifest execution",
                "PII-free CRM name repair manifest generation",
            ),
            depends_on=("customer.profile_commands",),
            notes=(
                "Historical repair is dry-run-first and applies only through "
                "the profile-command owner after exact digest confirmation."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="July 20 CRM name remediation manifest execution",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "CRM identity-change audit evidence",
                            "legacy Subscriber name state",
                        ),
                        canonical_writer="customer.name_remediation",
                    ),
                    ConcernContract(
                        name="PII-free CRM name repair manifest generation",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "CRM identity-change audit evidence",
                            "legacy Subscriber name state",
                        ),
                        canonical_writer="customer.name_remediation",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="CRM identity-change audit evidence",
                        owner="observability.audit_log",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="immutable CRM customer identity update audit events",
                    ),
                    AuthorityInput(
                        name="legacy Subscriber name state",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="locked Party-unbound Subscriber name columns",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "The reviewed manifest is revalidated and its selected "
                        "Subscriber corrections commit atomically."
                    ),
                    locking="Selected Subscriber rows are locked before revalidation.",
                    idempotency=(
                        "The manifest digest marks an exact successfully applied "
                        "remediation replay."
                    ),
                    retries=(
                        "Stale, invalid, or Party-bound rows fail closed and may be "
                        "replanned from immutable evidence."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        *owner_command_boundary_error_codes(
                            "customer.name_remediation"
                        ),
                        "customer.name_remediation.invalid_manifest",
                        "customer.name_remediation.party_bound",
                        "customer.name_remediation.stale_manifest",
                    ),
                    mapping_owner="CRM remediation operations adapters",
                ),
                events=EventContract(
                    event_types=("subscriber.updated",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Existing subscriber update consumers receive the established "
                        "subscriber.updated event shape."
                    ),
                    replay=(
                        "The persisted manifest digest makes an exact replay a no-op."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    new_owner="customer.name_remediation",
                    old_owner="ad-hoc CRM customer name repair scripts",
                    verification=(
                        "Plan, exact replay, Party-bound rejection, and drift "
                        "rollback tests verify the remediation boundary."
                    ),
                    cutover_gate=(
                        "Only an operator-confirmed digest may apply a generated plan."
                    ),
                    fallback_retirement=(
                        "Generic CRM webhooks observe rejected names and do not repair "
                        "Subscriber records."
                    ),
                ),
                steward="customer operations",
                design_refs=("docs/PARTY_CUSTOMER_LIFECYCLE.md",),
                test_refs=("tests/test_crm_customer_name_repair.py",),
            ),
        ),
        SOTService(
            name="customer.name_repairs",
            module="app.services.customer_name_repairs",
            owns=("evidence-bound legacy Subscriber name repair",),
            depends_on=(
                "customer.accounts",
                "party.registry",
                "observability.audit_log",
                "events.dispatcher",
            ),
            notes=(
                "This temporary repair owner corrects only legacy, Party-unbound "
                "Subscriber names from exact immutable incident audit evidence. "
                "Party-bound identities fail closed to party.registry."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="evidence-bound legacy Subscriber name repair",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "approved customer-name repair manifest",
                            "canonical legacy Subscriber name state",
                            "immutable CRM overwrite audit evidence",
                            "canonical Party identity binding",
                        ),
                        canonical_writer="customer.name_repairs",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="approved customer-name repair manifest",
                        owner="customer.name_repairs",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed expected/replacement names, exact source audit "
                            "UUIDs, manifest digest, actor, reason, and named target"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical legacy Subscriber name state",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="locked Party-unbound Subscriber name columns",
                    ),
                    AuthorityInput(
                        name="immutable CRM overwrite audit evidence",
                        owner="observability.audit_log",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=("crm_customer_identity_update old/new field evidence"),
                    ),
                    AuthorityInput(
                        name="canonical Party identity binding",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Subscriber.party_id cutover boundary",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "repair_customer_names enters execute_owner_command once; "
                        "name writes, identity-index rebuilds, audits, and events "
                        "commit or roll back together"
                    ),
                    locking=(
                        "All manifest Subscriber rows lock in stable UUID order; "
                        "current names are rechecked after locking."
                    ),
                    idempotency=(
                        "The SHA-256 manifest digest is the unique completed-batch "
                        "audit identity."
                    ),
                    retries=(
                        "An identical completed digest returns already_applied; "
                        "stale, missing, ambiguous, or Party-bound input fails closed."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "customer.name_repairs.active_caller_transaction",
                        "customer.name_repairs.command_contract_violation",
                        "customer.name_repairs.invalid_command_context",
                        "customer.name_repairs.nested_owner_command",
                        "customer.name_repairs.nested_transaction_completion",
                        "customer.name_repairs.invalid_manifest",
                        "customer.name_repairs.invalid_replacement",
                        "customer.name_repairs.missing_evidence",
                        "customer.name_repairs.invalid_evidence",
                        "customer.name_repairs.missing_subscriber",
                        "customer.name_repairs.party_bound",
                        "customer.name_repairs.stale_manifest",
                    ),
                    mapping_owner=("scripts.one_off.restore_crm_placeholder_identity"),
                    fail_closed_on=(
                        "missing or mismatched immutable audit evidence",
                        "stale current Subscriber name",
                        "Party-bound Subscriber identity",
                        "invalid or repeated repair manifest entry",
                    ),
                ),
                events=EventContract(
                    event_types=("subscriber.updated",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Existing subscriber.updated consumers receive an additive "
                        "changed_fields and remediation-reason payload."
                    ),
                    replay=(
                        "The completed manifest digest suppresses duplicate mutation "
                        "and event staging."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "direct one-off CLI writes and web_customer_actions repair helper"
                    ),
                    new_owner="customer.name_repairs",
                    verification=(
                        "Owner-command, stale-manifest, immutable-evidence, Party-bound, "
                        "idempotency, audit, event, and architecture tests."
                    ),
                    cutover_gate=(
                        "The incident CLI constructs the typed command on a clean "
                        "session and never writes or commits directly."
                    ),
                    fallback_retirement=(
                        "No script, route, webhook, or generic profile helper writes "
                        "the repair outside customer.name_repairs."
                    ),
                ),
                steward="customer operations",
                design_refs=(
                    "docs/PARTY_CUSTOMER_LIFECYCLE.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                ),
                test_refs=(
                    "tests/test_restore_crm_placeholder_identity.py",
                    "tests/architecture/test_crm_customer_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="customer.network_context",
            module="app.services.customer_network_context",
            owns=(
                "customer network footprint",
                "ONT/CPE/IP/session summary",
            ),
            depends_on=("customer.identity_scope", "network.access_path"),
        ),
        SOTService(
            name="customer.financial_position",
            module="app.services.customer_financial_position",
            owns=(
                "distinct invoice-receivable and prepaid-funding summaries",
                "customer-visible financial position",
                "bounded cohort financial projections",
                "currency-typed complete billing headline projection",
            ),
            depends_on=(
                "financial.credit_notes",
                "financial.invoices",
                "financial.ledger",
                "financial.payments",
                "financial.prepaid_funding_reconstruction",
            ),
            notes=(
                "A structurally evidenced PaymentSettlement owns the net "
                "customer value credited by a payment; the gross gateway "
                "charge and provider fee remain cash/accounting evidence and "
                "cannot inflate prepaid funding. Historical payments without "
                "settlement evidence retain their explicit gross-minus-refund "
                "fallback until reviewed reconciliation. "
                "Paid prepaid subscription invoices are non-AR documents but "
                "become exact customer-position service debits only when fully "
                "paid and backed by exact active settlement applications. "
                "An exact direct-renewal adjustment and entitlement for the same "
                "account, subscription, period, amount, and currency takes "
                "precedence so a later documentary invoice cannot debit twice. "
                "After customer-subledger authority activates, the immutable "
                "finance-approved subledger opening becomes the verifier's temporal "
                "baseline. Pre-opening facts retain their reviewed meaning; only "
                "facts crossing the opening instant use current native semantics."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "distinct invoice-receivable and prepaid-funding summaries"
                        ),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "reviewed prepaid reconstruction position",
                            "canonical payment and refund documents",
                            "canonical collectible invoice documents",
                            "canonical paid prepaid consumption documents",
                            "canonical renewal debit evidence",
                            "canonical credit and adjustment evidence",
                        ),
                    ),
                    ConcernContract(
                        name="customer-visible financial position",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "reviewed prepaid reconstruction position",
                            "canonical payment and refund documents",
                            "canonical collectible invoice documents",
                            "canonical paid prepaid consumption documents",
                            "canonical renewal debit evidence",
                            "canonical credit and adjustment evidence",
                        ),
                    ),
                    ConcernContract(
                        name="bounded cohort financial projections",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "reviewed prepaid reconstruction position",
                            "canonical payment and refund documents",
                            "canonical collectible invoice documents",
                            "canonical paid prepaid consumption documents",
                            "canonical renewal debit evidence",
                            "canonical credit and adjustment evidence",
                        ),
                    ),
                    ConcernContract(
                        name="currency-typed complete billing headline projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical payment and refund documents",
                            "canonical collectible invoice documents",
                            "canonical paid prepaid consumption documents",
                            "canonical credit and adjustment evidence",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="reviewed prepaid reconstruction position",
                        owner="financial.prepaid_funding_reconstruction",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "verified_prepaid_funding_balances: active currency-bound "
                            "PrepaidFundingBaseline before subledger activation, then "
                            "the immutable approved CustomerSubledgerOpeningPosition "
                            "plus canonical facts crossing its occurred_at boundary"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical payment and refund documents",
                        owner="financial.payments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active succeeded or refunded Payment plus exact "
                            "PaymentSettlement net customer value and currency when "
                            "present, paid time, refund amount, and exact allocation "
                            "evidence; unreconciled historical payments retain the "
                            "gross Payment amount fallback"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical collectible invoice documents",
                        owner="financial.invoices",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active non-proforma collectible Invoice lifecycle, total, "
                            "currency, issue time, and reviewed closure evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical paid prepaid consumption documents",
                        owner="financial.invoices",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active fully paid positive Invoice with an active exact "
                            "prepaid Subscription line whose total is fully backed by "
                            "active PaymentAllocation and/or CreditNoteApplication "
                            "evidence, plus paid time, period, total, and currency"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical renewal debit evidence",
                        owner="financial.ledger",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "unreversed prepaid_service_renewal AccountAdjustment and "
                            "its exact active debit-backed ServiceEntitlement"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical credit and adjustment evidence",
                        owner="financial.ledger",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active customer-position ledger adjustments plus "
                            "financial.credit_notes document evidence"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Callers own the session. Scalar and bounded cohort queries "
                        "rebuild the signed position from committed canonical evidence "
                        "without writes or transaction completion."
                    ),
                    locking=(
                        "No read locks. State-changing financial owners lock their "
                        "canonical rows and re-resolve this projection before writing."
                    ),
                    idempotency=(
                        "The same opening position, time bound, currency, and committed "
                        "financial evidence produce the same signed result."
                    ),
                    retries=(
                        "Transient reads may be retried; malformed, ambiguous, or "
                        "missing authority remains a deterministic fail-closed result."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner=(
                        "billing, access, reporting, portal, and reconciliation adapters"
                    ),
                    fail_closed_on=(
                        "missing reviewed prepaid authority for a pre-cutover account",
                        "cross-currency automation input",
                        "prepaid invoice without an exact subscription line or complete "
                        "active settlement applications",
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="customer-visible financial position",
                        input_names=(
                            "reviewed prepaid reconstruction position",
                            "canonical payment and refund documents",
                            "canonical collectible invoice documents",
                            "canonical paid prepaid consumption documents",
                            "canonical renewal debit evidence",
                            "canonical credit and adjustment evidence",
                        ),
                        writer="customer.financial_position",
                        freshness="rebuilt from committed source evidence on every query",
                        stale_behavior=(
                            "missing authority and ambiguous currency fail closed; paid "
                            "prepaid consumption is never left as reusable funding"
                        ),
                        drift_signal=(
                            "scalar and bounded-cohort results differ, or a paid prepaid "
                            "invoice total remains in spendable funding without an exact "
                            "direct-renewal precedence match"
                        ),
                        rebuild_operation=(
                            "list_customer_financial_events and "
                            "customer_financial_balances_by_currency recompute the view"
                        ),
                        repair_owner="customer.financial_position",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "collectible-AR-only native balance projection that counted a "
                        "gross payment but omitted its paid prepaid invoice consumption"
                    ),
                    new_owner="customer.financial_position",
                    verification=(
                        "Scalar/bulk parity, reviewed-baseline, paid prepaid invoice, "
                        "direct-renewal precedence, settlement-net provider-fee, "
                        "refund, legacy fallback, and architecture tests."
                    ),
                    cutover_gate=(
                        "Every prepaid balance display and enforcement reader consumes "
                        "the rebuilt customer.financial_position projection."
                    ),
                    fallback_retirement=(
                        "No UI, enforcement path, or repair script subtracts invoice "
                        "applications locally or edits a stored customer balance."
                    ),
                ),
                steward="finance operations",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                ),
                test_refs=(
                    "tests/test_customer_financial_ledger.py",
                    "tests/architecture/test_prepaid_funding_reconstruction_ownership.py",
                ),
            ),
        ),
        SOTService(
            name="customer.account_status_actions",
            module="app.services.account_status_commands",
            owns=(
                "administrative account-status impact preview",
                "administrative account-bound idempotent status confirmation",
            ),
            depends_on=(
                "customer.accounts",
                "customer.identity_scope",
                "access.subscription_lifecycle",
                "events.dispatcher",
            ),
            notes=(
                "Generic identity and contact edits cannot carry lifecycle state. "
                "Administrative account overrides require a reviewed, stale-safe "
                "confirmation. Unsuspend reverses only same-source administrative "
                "suspension consequences; disabled services and unrelated locks "
                "remain independently authoritative."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="administrative account-status impact preview",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "authenticated administrative status context",
                            "canonical account and subscription lifecycle state",
                            "account-status action protocol",
                        ),
                    ),
                    ConcernContract(
                        name=(
                            "administrative account-bound idempotent status "
                            "confirmation"
                        ),
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authenticated administrative status context",
                            "canonical account and subscription lifecycle state",
                            "signed account-status preview evidence",
                            "account-bound status idempotency evidence",
                            "account-status action protocol",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authenticated administrative status context",
                        owner="customer.identity_scope",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "authenticated actor, lifecycle write scope, reason, "
                            "command, correlation, and idempotency identifiers"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical account and subscription lifecycle state",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "locked Subscriber lifecycle override and every locked "
                            "Subscription identity, status, and active EnforcementLock "
                            "for the account"
                        ),
                    ),
                    AuthorityInput(
                        name="signed account-status preview evidence",
                        owner="customer.account_status_actions",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "SHA-256 fingerprint over account, billing approval, "
                            "override provenance, subscription, lock, target, "
                            "preservation, blocker, and projected status"
                        ),
                    ),
                    AuthorityInput(
                        name="account-bound status idempotency evidence",
                        owner="customer.account_status_actions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "locked IdempotencyKey scope, key, account binding, and "
                            "persisted result reference"
                        ),
                    ),
                    AuthorityInput(
                        name="account-status action protocol",
                        owner="customer.account_status_actions",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed activate, unsuspend, suspend, block, or disable "
                            "action; unsuspend is provenance-scoped and never aliases "
                            "broad activation"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "A confirmed command enters execute_owner_command once on a "
                        "clean session, stages lifecycle, audit, event, and replay "
                        "evidence, then commits the complete decision once."
                    ),
                    locking=(
                        "The Subscriber, all account Subscription identities, active "
                        "EnforcementLock rows, and existing idempotency evidence are "
                        "selected FOR UPDATE in stable identity order before the "
                        "preview is rechecked."
                    ),
                    idempotency=(
                        "Action scope, caller key, and account identity replay the "
                        "stored outcome without reapplying the lifecycle transition."
                    ),
                    retries=(
                        "Completed commands replay; a concurrent unique-key conflict "
                        "rolls back the whole command and is safe to retry."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "customer.account_status_actions.account_not_found",
                        "customer.account_status_actions.invalid_idempotency_key",
                        "customer.account_status_actions.idempotency_account_mismatch",
                        "customer.account_status_actions.idempotency_conflict",
                        "customer.account_status_actions.invalid_replay_evidence",
                        "customer.account_status_actions.command_scope_mismatch",
                        "customer.account_status_actions.invalid_reason",
                        "customer.account_status_actions.invalid_preview_fingerprint",
                        "customer.account_status_actions.stale_preview",
                        "customer.account_status_actions.action_not_allowed",
                        "customer.account_status_actions.billing_approval_required",
                        "customer.account_status_actions.invalid_command_context",
                        "customer.account_status_actions.command_contract_violation",
                        "customer.account_status_actions.nested_owner_command",
                        "customer.account_status_actions.active_caller_transaction",
                        "customer.account_status_actions.nested_transaction_completion",
                    ),
                    mapping_owner=("app.api.subscribers and app.web.admin.customers"),
                    retryable_codes=(
                        "customer.account_status_actions.idempotency_conflict",
                    ),
                    fail_closed_on=(
                        "missing actor, reason, preview, or idempotency evidence",
                        "stale account or subscription lifecycle state",
                        "billing-unapproved activation",
                        "cross-account idempotency reuse",
                    ),
                ),
                events=EventContract(
                    event_types=("subscriber.updated",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "The event retains subscriber identity and adds typed account-"
                        "status command, prior/current status, override, reason, and "
                        "preview evidence."
                    ),
                    replay=(
                        "The outbox replays delivery; command idempotency prevents "
                        "duplicate authoritative lifecycle mutation."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "generic SubscriberUpdate and admin customer forms that "
                        "created or cleared account lifecycle overrides"
                    ),
                    new_owner="customer.account_status_actions",
                    verification=(
                        "Generic edit rejection, read-only form, preview, stale "
                        "confirmation, lock, replay, audit, and architecture tests."
                    ),
                    cutover_gate=(
                        "All post-creation administrative status changes use the "
                        "dedicated preview and confirmation owner."
                    ),
                    fallback_retirement=(
                        "Subscriber.update ignores no lifecycle fields and contains "
                        "no override writer; adapters cannot submit status or is_active."
                    ),
                ),
                steward="customer operations",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_account_status_commands.py",
                    "tests/test_web_customer_details.py",
                    "tests/architecture/test_generic_lifecycle_edit_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="customer.reseller_status_actions",
            module="app.services.reseller_portal",
            owns=(
                "reseller-scoped account-action impact preview",
                "lock-aware account-action eligibility",
                "account-action stale-preview fingerprint",
                "account-bound idempotent status confirmation",
            ),
            depends_on=(
                "customer.accounts",
                "customer.identity_scope",
                "access.subscription_lifecycle",
                "events.dispatcher",
            ),
            notes=(
                "The reseller adapter renders a distinct confirmation step "
                "bound to this preview and an account-scoped idempotency key. "
                "Subscription and account lifecycle mutation remains owned by "
                "access.subscription_lifecycle."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="reseller-scoped account-action impact preview",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical reseller account scope",
                            "canonical account and subscription lifecycle state",
                            "reseller account-status action protocol",
                        ),
                    ),
                    ConcernContract(
                        name="lock-aware account-action eligibility",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical account and subscription lifecycle state",
                            "canonical enforcement lock and login-conflict state",
                            "reseller account-status action protocol",
                        ),
                    ),
                    ConcernContract(
                        name="account-action stale-preview fingerprint",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical reseller account scope",
                            "canonical account and subscription lifecycle state",
                            "canonical enforcement lock and login-conflict state",
                            "reseller account-status action protocol",
                        ),
                    ),
                    ConcernContract(
                        name="account-bound idempotent status confirmation",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authenticated reseller status command context",
                            "canonical reseller account scope",
                            "canonical account and subscription lifecycle state",
                            "canonical enforcement lock and login-conflict state",
                            "signed status preview evidence",
                            "account-bound status idempotency evidence",
                            "reseller account-status action protocol",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authenticated reseller status command context",
                        owner="customer.identity_scope",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "authenticated reseller principal, reseller/account scope, "
                            "reason, command, correlation, and idempotency identifiers"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical reseller account scope",
                        owner="customer.identity_scope",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "active non-reseller Subscriber selected only through its "
                            "canonical reseller_id ownership boundary"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical account and subscription lifecycle state",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "locked Subscriber lifecycle override plus all locked "
                            "Subscription identities and statuses for the account"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical enforcement lock and login-conflict state",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "active EnforcementLock evidence and duplicate-login "
                            "reactivation policy for each subscription"
                        ),
                    ),
                    AuthorityInput(
                        name="signed status preview evidence",
                        owner="customer.reseller_status_actions",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "SHA-256 fingerprint over account, override, subscription, "
                            "lock, eligibility, and affected-identity evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="account-bound status idempotency evidence",
                        owner="customer.reseller_status_actions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "locked IdempotencyKey scope/key/account binding and exact "
                            "persisted confirmation result reference"
                        ),
                    ),
                    AuthorityInput(
                        name="reseller account-status action protocol",
                        owner="customer.reseller_status_actions",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed deactivate, restore, or disable action and the "
                            "owner-defined eligibility and consequence vocabulary"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "A typed confirmation starts on a clean adapter session, locks "
                        "the account, subscriptions, and idempotency evidence, rechecks "
                        "the preview, stages lifecycle participants and their events, "
                        "records the replay result, then commits once."
                    ),
                    locking=(
                        "The reseller-owned Subscriber, every account Subscription, and "
                        "any existing scope/key IdempotencyKey are selected FOR UPDATE "
                        "before the confirmed consequence is staged."
                    ),
                    idempotency=(
                        "Scope, key, and account identity bind a confirmation to one "
                        "action; a completed replay returns the exact stored status and "
                        "changed/skipped counts without reapplying lifecycle changes."
                    ),
                    retries=(
                        "Stable completed confirmations replay. A concurrent unique-key "
                        "conflict rolls back the complete command and is retryable after "
                        "the winning confirmation records its result."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "customer.reseller_status_actions.invalid_preview_fingerprint",
                        "customer.reseller_status_actions.stale_preview",
                        "customer.reseller_status_actions.action_not_allowed",
                        "customer.reseller_status_actions.invalid_idempotency_key",
                        "customer.reseller_status_actions.idempotency_account_mismatch",
                        "customer.reseller_status_actions.confirmation_in_progress",
                        "customer.reseller_status_actions.idempotency_conflict",
                        "customer.reseller_status_actions.command_scope_mismatch",
                        "customer.reseller_status_actions.invalid_command_context",
                        "customer.reseller_status_actions.command_contract_violation",
                        "customer.reseller_status_actions.nested_owner_command",
                        "customer.reseller_status_actions.active_caller_transaction",
                        "customer.reseller_status_actions.nested_transaction_completion",
                    ),
                    mapping_owner="app.services.web_reseller_routes",
                    retryable_codes=(
                        "customer.reseller_status_actions.idempotency_conflict",
                        "customer.reseller_status_actions.confirmation_in_progress",
                    ),
                    fail_closed_on=(
                        "foreign reseller/account ownership",
                        "missing, malformed, or stale preview evidence",
                        "active enforcement locks or duplicate-login conflicts",
                        "idempotency key reuse across accounts",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "reseller portal string actions with optional helper commits, "
                        "helper rollback/requery, and a public non-idempotent bypass"
                    ),
                    new_owner="customer.reseller_status_actions",
                    verification=(
                        "Typed preview/confirmation, reseller scope, stale fingerprint, "
                        "lock eligibility, replay, cross-account key, rollback, clean-"
                        "adapter, lifecycle consequence, and architecture tests."
                    ),
                    cutover_gate=(
                        "The reseller web adapter constructs typed requests and commands "
                        "on a clean session; every status mutation enters the one "
                        "idempotent coordinator boundary."
                    ),
                    fallback_retirement=(
                        "The public direct-update helper, optional commit flag, helper "
                        "commit/rollback, free-form action mutation, and cross-account "
                        "idempotency fallback are removed."
                    ),
                ),
                steward="customer operations",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                ),
                test_refs=(
                    "tests/test_reseller_gaps.py",
                    "tests/test_reseller_portal_services.py",
                    "tests/architecture/test_reseller_status_action_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="customer.service_level",
            module="app.services.customer_service_level",
            owns=(
                "per-subscription SLA policy resolution and period score",
                "immutable effective-dated SLA policy versions",
                "immutable SLA period-score revisions and evidence snapshots",
            ),
            depends_on=(
                "access.subscription_lifecycle_evidence",
                "billing.contracts",
                "financial.prepaid_service_coverage",
                "network.customer_outage_accrual",
                "sessions.radius_resolution",
                "service_intent.catalog_policy",
            ),
            notes=(
                "Shadow-phase read-time scorer (OUTAGE_SLA_SPINE §4): "
                "resolves the effective policy (offer-version precedence "
                "today; subscription/account contracts and persisted "
                "immutable policy versions arrive with cutover), merges "
                "the accrual ledger's qualifying intervals per "
                "Africa/Lagos calendar month, and never invents a "
                "contractual SLA — no policy renders measured "
                "availability as no_contractual_sla. Eligibility is the "
                "intersection of immutable lifecycle evidence and exact "
                "prepaid entitlement or authoritative postpaid contract "
                "history. Positive subscription-bound RADIUS accounting "
                "proves monitored time; every remaining eligible gap is "
                "unknown, never uptime. Overlaps union, exclusions and "
                "estimated evidence report in their own bucket. Each run "
                "appends a reproducible score revision and exact evidence "
                "snapshots; incomplete evidence may prove a breach but can "
                "never produce passing or at-risk. The "
                "legacy topology.customer_availability stays the "
                "displayed authority until the shadow-comparison gate "
                "cuts over; two displayed scores must never coexist."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="immutable effective-dated SLA policy versions",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=("contractual SLA terms",),
                        canonical_writer="customer.service_level",
                    ),
                    ConcernContract(
                        name=(
                            "per-subscription SLA policy resolution and period score"
                        ),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "contractual SLA terms",
                            "period-scoped lifecycle evidence",
                            "period-scoped prepaid entitlement evidence",
                            "period-scoped postpaid contract evidence",
                            "positive subscription monitoring evidence",
                            "qualifying downtime intervals",
                            "offer SLA policy inputs",
                        ),
                    ),
                    ConcernContract(
                        name=(
                            "immutable SLA period-score revisions and evidence "
                            "snapshots"
                        ),
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "contractual SLA terms",
                            "period-scoped lifecycle evidence",
                            "period-scoped prepaid entitlement evidence",
                            "period-scoped postpaid contract evidence",
                            "positive subscription monitoring evidence",
                            "qualifying downtime intervals",
                        ),
                        canonical_writer="customer.service_level",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="contractual SLA terms",
                        owner="customer.service_level",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "immutable effective-dated sla_policy_versions "
                            "rows, append-only, one version in force per "
                            "policy_key per instant. Scope precedence, highest "
                            "first: subscription_contract, account_contract, "
                            "offer_version, plan_family, internal_measurement. "
                            "The plan_family scope carries a commercial-family "
                            "default (unlimited/dedicated/home_flex) so a family "
                            "promise has one owner instead of being copied onto "
                            "every offer in it"
                        ),
                    ),
                    AuthorityInput(
                        name="period-scoped lifecycle evidence",
                        owner="access.subscription_lifecycle_evidence",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "trusted append-only lifecycle transitions projected "
                            "into proven-active intervals with explicit coverage "
                            "issues"
                        ),
                    ),
                    AuthorityInput(
                        name="period-scoped prepaid entitlement evidence",
                        owner="financial.prepaid_service_coverage",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "exact funded ServiceEntitlement and applied service-"
                            "extension grant intervals with unresolved paid-through "
                            "projection diagnostics"
                        ),
                    ),
                    AuthorityInput(
                        name="period-scoped postpaid contract evidence",
                        owner="billing.contracts",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "authoritative effective/superseded billing-contract "
                            "versions; shadow rows are explicit incomplete evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="positive subscription monitoring evidence",
                        owner="sessions.radius_resolution",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "exact-subscription RADIUS accounting session start to "
                            "stop/last-observation intervals; unbound sessions and "
                            "unobserved gaps prove nothing"
                        ),
                    ),
                    AuthorityInput(
                        name="qualifying downtime intervals",
                        owner="network.customer_outage_accrual",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "customer_outage_intervals with state, "
                            "quality, exclusion candidates, and "
                            "provisional/finalized ends"
                        ),
                    ),
                    AuthorityInput(
                        name="offer SLA policy inputs",
                        owner="service_intent.catalog_policy",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "CatalogOffer.sla_profile_id and SlaProfile "
                            "uptime/credit fields as display-only policy "
                            "evidence until effective-dated versions land"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "record_policy_version appends one immutable "
                        "version and closes the version it supersedes in "
                        "a single owner-managed transaction, staging its "
                        "typed output with the write. record_period_score locks "
                        "one subscription, calculates from typed owner facts, "
                        "appends one revision plus its eligibility/monitoring "
                        "snapshots, and stages its event in one transaction. The "
                        "read scorer performs the same calculation without writes."
                    ),
                    locking=(
                        "The writer locks the target policy series "
                        "(SELECT ... FOR UPDATE on sla_policy_versions "
                        "for one policy_key, ordered by version desc) "
                        "before reading the version in force, so "
                        "concurrent writers on one scope serialise "
                        "instead of both acting on a stale current "
                        "version. No other table is locked, so the "
                        "single-resource order cannot deadlock against "
                        "the accrual ledger. Period-score writers lock only the "
                        "Subscription row before reading the latest revision; source "
                        "owners never lock score rows. Read scoring acquires no "
                        "mutation locks."
                    ),
                    idempotency=(
                        "When a key is supplied it, not the fingerprint, "
                        "is the identity: the same key with the same "
                        "fingerprint replays, the same key with different "
                        "terms raises idempotency_conflict, and identical "
                        "terms submitted under a NEW key raise "
                        "duplicate_policy_terms rather than reporting "
                        "success under a key that reserves nothing. With "
                        "no key supplied the fingerprint is the only "
                        "identity and replays on match. "
                        "A durable command fingerprint over derived "
                        "policy key, source, effective_from and terms is "
                        "stored on the row under a unique constraint; a "
                        "replay returns the original PolicyVersionOutcome "
                        "with replayed=True rather than raising against "
                        "the row it already created. Scoring is "
                        "naturally idempotent: canonical JSON over the exact "
                        "intervals, policy segments, lineage and measured-through "
                        "instant yields one evidence digest. The same command/key "
                        "and digest replays; changed evidence under the same identity "
                        "conflicts; exact evidence under another identity is rejected "
                        "instead of reporting an unreserved success."
                    ),
                    retries=(
                        "A writer that loses the race surfaces "
                        "customer.service_level.concurrent_version_"
                        "conflict for the named race constraints, "
                        "including the idempotency key: a raw collision "
                        "does not reveal whether the winner wrote the "
                        "same terms, so the retry re-reads the winner and "
                        "decides replay-or-conflict from evidence. Named "
                        "input constraints surface as "
                        "invalid_policy_version, since retrying those "
                        "would loop forever. Any UNRECOGNISED constraint "
                        "or driver failure is re-raised unchanged — an "
                        "unexpected defect must stay unexpected. Scope "
                        "and parent existence are validated before the "
                        "database sees the row. Period-score uniqueness races "
                        "surface concurrent_score_conflict; reads are always safe "
                        "to retry."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        *owner_command_boundary_error_codes("customer.service_level"),
                        "customer.service_level.contractual_target_required",
                        "customer.service_level.missing_effective_from",
                        "customer.service_level.not_after_current",
                        "customer.service_level.would_rewrite_closed_period",
                        "customer.service_level.scope_required",
                        "customer.service_level.invalid_scope",
                        "customer.service_level.unknown_scope",
                        "customer.service_level.idempotency_conflict",
                        "customer.service_level.duplicate_policy_terms",
                        "customer.service_level.invalid_policy_version",
                        "customer.service_level.concurrent_version_conflict",
                        "customer.service_level.unknown_subscription",
                        "customer.service_level.score_idempotency_conflict",
                        "customer.service_level.duplicate_score_evidence",
                        "customer.service_level.concurrent_score_conflict",
                    ),
                    mapping_owner="app.services.web_customer_details",
                    fail_closed_on=(
                        "contractual source without an availability target",
                        "effective_from at or before the version in force",
                        "backdating behind an already-closed version",
                        "a precedence claim with no matching scope",
                        "a scope id that does not belong to the source",
                        "a scope id with no such parent record",
                        "an idempotency key reused for different terms",
                        "identical terms already recorded under another key",
                        "a concurrent writer winning the series race",
                        "missing subscription identity for period scoring",
                        "a score command identity reused after evidence changes",
                        "exact score evidence submitted under another identity",
                        "a concurrent writer winning the score revision race",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "sla_policy_version.recorded",
                        "sla_period_score.recorded",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 policy events carry key, version, source and "
                        "supersession; score events carry revision identity, period, "
                        "verdict, completeness and digest. Fields are additive. The "
                        "immutable rows, not these breadcrumbs, are authoritative."
                    ),
                    replay=(
                        "No projection handler consumes it; replay writes nothing."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.SHADOWING,
                    old_owner=(
                        "mutable SlaProfile terms and the read-time "
                        "topology.customer_availability "
                        "trailing-window calculation"
                    ),
                    new_owner="customer.service_level",
                    verification=(
                        "shadow_compare discrepancy review across the "
                        "active base plus the scorer's period, union, "
                        "exclusion, and verdict tests."
                    ),
                    cutover_gate=(
                        "Displayed availability switches only after the "
                        "discrepancy review passes and evidence coverage "
                        "gates customer visibility; two displayed scores "
                        "never coexist."
                    ),
                    fallback_retirement=(
                        "The legacy trailing-window derivation is "
                        "retired at cutover with explicit approval."
                    ),
                ),
                steward="customer operations",
                design_refs=(
                    "docs/designs/OUTAGE_SLA_SPINE.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_customer_service_level.py",
                    "tests/integration/test_sla_policy_versions_postgres.py",
                    "tests/integration/test_sla_period_scores_postgres.py",
                ),
            ),
        ),
        SOTService(
            name="customer.service_status",
            module="app.services.service_status",
            owns=(
                "customer-visible service health",
                "customer financial action hints",
                "payment-restores-service claims",
            ),
            depends_on=(
                "financial.access_resolution",
                "customer.financial_position",
                "financial.grace_policy",
            ),
        ),
        SOTService(
            name="customer.usage_summary",
            module="app.services.usage_summary",
            owns=(
                "customer usage window definitions",
                "customer usage headline totals",
                "customer usage total provenance",
            ),
            depends_on=("sessions.radius_reconciliation",),
            notes=(
                "Authoritative zero is a valid total. Customer clients do "
                "not replace server totals with loaded-session pages or "
                "retention-limited chart series."
            ),
        ),
        SOTService(
            name="customer.experience_lifecycle",
            module="app.services.customer_experience_lifecycle",
            owns=(
                "read-only Project to ProjectTask to WorkOrder to Ticket composition",
                "customer experience-state projection",
                "server-owned customer self-care action projection",
            ),
            depends_on=(
                "customer.identity_scope",
                "operations.project_lifecycle",
                "operations.work_orders",
                "support.ticket_lifecycle",
            ),
            notes=(
                "This owner composes native Sub state and never mutates a "
                "domain root. Customer, reseller, field, web, and mobile "
                "surfaces consume the typed projection without CRM mirror "
                "fallbacks or client-side action eligibility decisions."
            ),
        ),
        SOTService(
            name="customer.work_order_selfcare",
            module="app.services.customer_work_order_selfcare",
            owns=(
                "subscriber-scoped live technician-location read",
                "canonical customer technician rating",
            ),
            depends_on=(
                "customer.identity_scope",
                "operations.work_order_commands",
                "operations.work_orders",
                "observability.audit_log",
            ),
        ),
        SOTService(
            name="customer.field_job_chat",
            module="app.services.customer_field_job_chat",
            owns=("subscriber-scoped job chat read and send",),
            depends_on=(
                "customer.identity_scope",
                "communications.team_inbox_field_job",
                "operations.work_orders",
            ),
            notes=(
                "Portal adapter for the technician chat. It scopes every "
                "call to the caller's own work order and delegates the "
                "inbox write to communications.team_inbox_field_job; it "
                "decides nothing about when a chat exists."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="subscriber-scoped job chat read and send",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "authenticated subscriber identity",
                            "canonical job chat conversation",
                            "canonical work order ownership",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authenticated subscriber identity",
                        owner="customer.identity_scope",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Portal session principal id.",
                    ),
                    AuthorityInput(
                        name="canonical job chat conversation",
                        owner="communications.team_inbox_field_job",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "field_job conversation keyed by work order, with "
                            "its open/closed lifecycle."
                        ),
                    ),
                    AuthorityInput(
                        name="canonical work order ownership",
                        owner="operations.work_orders",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="WorkOrder.subscriber_id and public_id.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary=(
                        "Holds no transaction. The inbox owner writes and "
                        "commits the message and returns an inert snapshot; "
                        "this service only scopes the request and broadcasts "
                        "afterwards, which never rolls the write back."
                    ),
                    locking="None: it takes no locks of its own.",
                    idempotency=(
                        "None: a repeated send is a genuinely new message, as "
                        "in any chat."
                    ),
                    retries=(
                        "A closed or undeparted visit fails closed rather than "
                        "creating a conversation the technician never opened."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "customer.field_job_chat.empty_body",
                        "customer.field_job_chat.not_found",
                        "customer.field_job_chat.not_departed",
                        "customer.field_job_chat.closed",
                    ),
                    mapping_owner="app.api.me",
                    fail_closed_on=(
                        "customer.field_job_chat.not_found",
                        "customer.field_job_chat.not_departed",
                        "customer.field_job_chat.closed",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="customer.field_job_chat",
                    verification=(
                        "tests/test_field_job_chat.py asserts subscriber "
                        "scoping and the not-departed and closed refusals."
                    ),
                ),
                steward="customer experience platform",
                design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
                test_refs=("tests/test_field_job_chat.py",),
            ),
        ),
        SOTService(
            name="subscriber.growth_reports",
            module="app.services.subscriber_growth",
            owns=(
                "admin subscriber growth and churn report figures",
                "monthly subscriber growth and churn series",
                "derived subscriber-status report counts",
            ),
            notes=(
                "Domain read owner for the admin /reports growth, churn, "
                "and status figures. The web report layer composes these "
                "reads and owns presentation only."
            ),
        ),
        SOTService(
            name="customer.data_completeness",
            module="app.services.subscriber_data_completeness",
            owns=(
                "purpose-specific subscriber data requirements",
                "derived completeness and revalidation state",
                "subscriber capture backlog and filing-readiness counts",
            ),
            depends_on=("customer.identity_scope",),
            notes=(
                "Read-only policy owner. It reports absent, inferred, "
                "captured, and stale state; it never fills a field or "
                "writes a capture fact."
            ),
        ),
        SOTService(
            name="customer.location_verification",
            module="app.services.geocode_reconciler",
            owns=(
                "subscriber location verification ledger writes",
                "reconciliation of a captured pin against claimed location",
            ),
            depends_on=("customer.identity_scope",),
            notes=(
                "Captured location facts flow through this owner. The "
                "reconciler adjudicates a GPS pin against what was claimed "
                "and writes ledger rows only for what agrees; a "
                "disagreement is flagged for a human, never auto-applied. "
                "It never writes Subscriber columns — projecting a captured "
                "fact onto the profile stays the subscriber owner's job. "
                "Only the location-capture owner invokes this writer."
            ),
        ),
        SOTService(
            name="customer.location_capture",
            module="app.services.location_capture",
            owns=(
                "location-capture rollout and source authorization",
                "location prompt eligibility and snooze lifecycle",
                "field, portal, and agent capture orchestration",
            ),
            depends_on=(
                "customer.identity_scope",
                "customer.data_completeness",
                "customer.location_verification",
            ),
            notes=(
                "The field-arrival, portal, and agent adapters call this "
                "owner. It enforces the default-off controls before "
                "delegating adjudication and ledger writes to location "
                "verification; it never writes Subscriber columns."
            ),
        ),
        SOTService(
            name="customer.branding",
            module="app.services.brand_profiles",
            owns=(
                "platform/reseller/organization brand profiles",
                "customer-facing brand precedence",
                "brand primary, secondary, and semantic UI color roles",
                "runtime web theme token generation",
                "legacy branding convergence",
            ),
            depends_on=("customer.identity_scope", "control.domain_settings"),
        ),
    ),
    entrypoints=(
        "app.web.customer",
        "app.api.me",
        "app.api.subscribers",
        "app.web.admin.customers",
        "mobile",
        "app.services.customer_portal_*",
        "app.services.crm_api",
    ),
    rule="Customer-facing surfaces resolve scope once through customer context "
    "and compose network/financial summaries through services. Clients "
    "consume service-status action hints instead of inferring restoration "
    "policy from subscription status or invoice rows, and consume usage "
    "totals with their server-owned provenance instead of reconstructing "
    "headlines from partial client data.",
)
