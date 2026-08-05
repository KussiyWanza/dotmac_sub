"""Canonical SOT declarations for the ui_semantic_presentation domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    MigrationContract,
    OwnerRole,
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="ui_semantic_presentation",
    services=(
        SOTService(
            name="ui.projection_contracts",
            module="app.services.ui_contracts",
            owns=(
                "UI value availability and freshness contract",
                "UI KPI exact-cohort contract",
                "UI action eligibility and confirmation contract",
            ),
            depends_on=("ui.status_presentation",),
            notes=(
                "Transport-neutral StateValue, Kpi, and Action shapes. Domain "
                "read and command owners supply the facts and decisions; "
                "templates and clients render them without deriving meaning."
            ),
            contract=ServiceContract(
                concerns=tuple(
                    ConcernContract(
                        name=concern,
                        role=OwnerRole.POLICY,
                        input_names=("UI projection contract vocabulary",),
                    )
                    for concern in (
                        "UI value availability and freshness contract",
                        "UI KPI exact-cohort contract",
                        "UI action eligibility and confirmation contract",
                    )
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="UI projection contract vocabulary",
                        owner="ui.projection_contracts",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "StateKind, StateValue, Kpi, and Action typed invariants"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary=(
                        "Typed projection value objects validate in memory and "
                        "never access a database session."
                    ),
                    locking="Immutable value objects require no lock.",
                    idempotency=(
                        "Construction is deterministic for the same typed inputs."
                    ),
                    retries="In-memory construction has no retry side effect.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="domain projection owners and UI adapters",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "portal-specific dictionaries and template-derived "
                        "availability, KPI, and action semantics"
                    ),
                    new_owner="ui.projection_contracts",
                    verification=(
                        "Typed invariant, projection-boundary, and portal "
                        "adoption tests."
                    ),
                    cutover_gate=(
                        "Adopted projections return StateValue, Kpi, and Action "
                        "objects without template-side decision logic."
                    ),
                    fallback_retirement=(
                        "Adopted templates no longer derive unknown/stale state, "
                        "KPI cohorts, eligibility, or confirmation requirements."
                    ),
                ),
                steward="platform UI",
                design_refs=(
                    "docs/designs/UI_PROJECTION_CONTRACTS.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_ui_contracts.py",
                    "tests/architecture/test_template_projection_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="ui.operational_evidence_projection",
            module="app.services.operational_checks",
            owns=(
                "question-driven operational evidence projection",
                "operational retry and next-action projection",
                "payment automation operational evidence projection",
            ),
            depends_on=(
                "ui.projection_contracts",
                "observability.recording",
                "scheduler.registry",
                "integration.registry",
                "integration.installations",
                "control.feature_registry",
                "financial.payment_provider_events",
                "financial.payment_reconciliation",
                "financial.topup_intents",
            ),
            notes=(
                "Keeps administrative expectation, last observation, evidence "
                "age, customer-data impact, and retry/next action separate. It "
                "does not convert missing or stale telemetry into device or "
                "customer service down."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="question-driven operational evidence projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "bounded collector and task observations",
                            "scheduler expectation",
                            "integration capability binding facts",
                            "native quote cutover controls",
                        ),
                    ),
                    ConcernContract(
                        name="operational retry and next-action projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "bounded collector and task observations",
                            "scheduler expectation",
                            "integration capability binding facts",
                            "native quote cutover controls",
                        ),
                    ),
                    ConcernContract(
                        name="payment automation operational evidence projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "bounded collector and task observations",
                            "scheduler expectation",
                            "integration capability binding facts",
                            "canonical payment-provider observations",
                            "canonical top-up reconciliation backlog",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="bounded collector and task observations",
                        owner="observability.recording",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "bandwidth poller snapshot, task heartbeat result, "
                            "and CRM capability-operation receipt"
                        ),
                    ),
                    AuthorityInput(
                        name="scheduler expectation",
                        owner="scheduler.registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="effective ScheduledTask enablement and cadence",
                    ),
                    AuthorityInput(
                        name="integration capability binding facts",
                        owner="integration.installations",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "version-pinned installation, enabled binding, "
                            "validated config revision, and manifest declaration"
                        ),
                    ),
                    AuthorityInput(
                        name="native quote cutover controls",
                        owner="control.feature_registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "effective quotes.native_read and "
                            "quotes.native_write controls"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical payment-provider observations",
                        owner="financial.payment_provider_events",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "persisted Paystack webhook receipt observations with "
                            "provider event time and verified-signature ingress "
                            "provenance"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical top-up reconciliation backlog",
                        owner="financial.payment_reconciliation",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "bounded eligible and outside-window pending-intent "
                            "projection at the operational observation time"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Materializes bounded database control facts and reads "
                        "bounded cache observations without a business write."
                    ),
                    locking="Read projection acquires no mutation locks.",
                    idempotency=(
                        "The same facts and observation time produce the same "
                        "operator questions, evidence, and next action."
                    ),
                    retries="Read projection calls are safe to retry.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="NOC and integration web adapters",
                ),
                projections=(
                    ProjectionContract(
                        name="question-driven operational evidence projection",
                        input_names=(
                            "bounded collector and task observations",
                            "scheduler expectation",
                            "integration capability binding facts",
                            "native quote cutover controls",
                        ),
                        writer="ui.operational_evidence_projection",
                        freshness=(
                            "Each observation retains its source observed_at; "
                            "expected cadence remains a separate control fact."
                        ),
                        stale_behavior=(
                            "Explains that evidence is late or absent and never "
                            "turns that gap into a service-down claim."
                        ),
                        drift_signal=(
                            "NOC/integration projection contract tests and missing "
                            "source timestamps."
                        ),
                        rebuild_operation=(
                            "Recompute on read from bounded observations and "
                            "current control-plane facts."
                        ),
                        repair_owner="ui.operational_evidence_projection",
                    ),
                    ProjectionContract(
                        name="payment automation operational evidence projection",
                        input_names=(
                            "bounded collector and task observations",
                            "scheduler expectation",
                            "integration capability binding facts",
                            "canonical payment-provider observations",
                            "canonical top-up reconciliation backlog",
                        ),
                        writer="ui.operational_evidence_projection",
                        freshness=(
                            "Recomputed on read from the latest signed-webhook "
                            "receipt time, runner heartbeat and result, effective "
                            "schedule, binding state, and current backlog."
                        ),
                        stale_behavior=(
                            "Reports missing delivery evidence, runner rejection "
                            "counts, and outside-window backlog separately; it "
                            "never infers that provider payment did not occur."
                        ),
                        drift_signal=(
                            "A stale eligible backlog without recent webhook or "
                            "successful reconciliation evidence, a partial runner "
                            "result, or a disabled capability or schedule."
                        ),
                        rebuild_operation=(
                            "Recompute the Paystack operational check from "
                            "canonical provider-event, reconciliation, scheduler, "
                            "binding, and heartbeat inputs."
                        ),
                        repair_owner="ui.operational_evidence_projection",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "NOC and installed-integration templates collapsing "
                        "expectation and evidence into healthy/degraded/unknown"
                    ),
                    new_owner="ui.operational_evidence_projection",
                    verification=(
                        "NOC, integration evidence, retry, freshness, and "
                        "template projection tests."
                    ),
                    cutover_gate=(
                        "NOC and installed integrations render owner-provided "
                        "questions and evidence without local status derivation."
                    ),
                    fallback_retirement=(
                        "The installed-integrations generic health field and "
                        "health badge branch are removed."
                    ),
                ),
                steward="platform operations UI",
                design_refs=(
                    "docs/designs/OPERATIONAL_EVIDENCE_AND_RETRY.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/runbooks/PAYSTACK_AUTOMATIC_POSTING.md",
                ),
                test_refs=(
                    "tests/test_operational_evidence_followup.py",
                    "tests/test_web_network_noc.py",
                    "tests/test_integrations_observability.py",
                ),
            ),
        ),
        SOTService(
            name="ui.billing_account_workspace_projection",
            module="app.services.web_billing_accounts",
            owns=(
                "admin billing-account first-viewport projection",
                "admin account-statement currency summary projection",
                "admin account-statement row and source-link projection",
            ),
            depends_on=(
                "customer.accounts",
                "customer.financial_position",
                "financial.billing_profile",
                "financial.ledger",
                "financial.prepaid_funding_reconstruction",
                "ui.projection_contracts",
                "ui.status_presentation",
            ),
            notes=(
                "Receivables and prepaid funding remain separate. Statement "
                "opening, activity, closing, and running balances are grouped "
                "by currency and never nominally netted. Financial commands "
                "remain with their invoice, payment, credit-note, and ledger owners."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="admin billing-account first-viewport projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical billing-account state",
                            "canonical billing-mode profile",
                            "canonical customer financial position",
                            "UI projection vocabulary",
                        ),
                    ),
                    ConcernContract(
                        name="admin account-statement currency summary projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical customer financial events",
                            "UI projection vocabulary",
                        ),
                    ),
                    ConcernContract(
                        name="admin account-statement row and source-link projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical customer financial events",
                            "canonical financial document identities",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical billing-account state",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Subscriber account identity and lifecycle status",
                    ),
                    AuthorityInput(
                        name="canonical billing-mode profile",
                        owner="financial.billing_profile",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "effective account/subscription billing mode, source, "
                            "and invalid reason"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical customer financial position",
                        owner="customer.financial_position",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "currency-typed invoice receivables and reviewed prepaid "
                            "funding position"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical customer financial events",
                        owner="financial.ledger",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "customer_financial_ledger source documents, reviewed "
                            "opening positions, and native ledger evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical financial document identities",
                        owner="financial.ledger",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="event-to-invoice, payment, and closure identities",
                    ),
                    AuthorityInput(
                        name="UI projection vocabulary",
                        owner="ui.projection_contracts",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="StateValue availability semantics and display contracts",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "The workspace and statement services read on the adapter "
                        "session and never mutate or complete a transaction."
                    ),
                    locking="Read projections acquire no mutation locks.",
                    idempotency=(
                        "The same account, date range, and authoritative facts produce "
                        "the same state, currency lanes, rows, and source links."
                    ),
                    retries="Read-only projection calls are safe to retry.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="app.web.admin.billing_accounts",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "templates/admin/billing/account_detail.html generic account "
                        "balance and cross-currency statement arithmetic"
                    ),
                    new_owner="ui.billing_account_workspace_projection",
                    verification=(
                        "Billing-account overview, currency separation, CSV parity, "
                        "source-link, and template-boundary tests."
                    ),
                    cutover_gate=(
                        "The account route and template consume only the owner-provided "
                        "overview and statement projections."
                    ),
                    fallback_retirement=(
                        "account.balance rendering and scalar multi-currency statement "
                        "totals are absent."
                    ),
                ),
                steward="finance operations",
                design_refs=(
                    "docs/designs/BILLING_ACCOUNT_360.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_billing_accounts_list.py",
                    "tests/test_billing_statement_service.py",
                    "tests/architecture/test_template_projection_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="ui.portal_account_health_projection",
            module="app.services.portal_account_health",
            owns=(
                "customer and reseller account-health first-viewport projection",
                "subscription-scoped service-health row projection",
                "portal financial-position currency-lane projection",
                "mobile account-health transport projection",
                "pending service-change presentation",
            ),
            depends_on=(
                "access.subscription_lifecycle",
                "customer.accounts",
                "customer.financial_position",
                "customer.service_status",
                "financial.billing_profile",
                "financial.prepaid_funding_reconstruction",
                "network.connection_health",
                "network.outage_lifecycle",
                "network.radius_sessions",
                "service_intent.subscription_lifecycle",
                "ui.projection_contracts",
                "ui.status_presentation",
            ),
            notes=(
                "One read owner composes lifecycle, billing mode, currency-typed "
                "receivables, prepaid funding, access decisions, live-session "
                "freshness, customer-safe connection/outage diagnosis, and the "
                "canonical next action. It never polls equipment or mutates state."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "customer and reseller account-health first-viewport "
                            "projection"
                        ),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical account state",
                            "canonical billing profile",
                            "canonical customer financial position",
                            "canonical service-health rows",
                            "UI projection vocabulary",
                        ),
                    ),
                    ConcernContract(
                        name="subscription-scoped service-health row projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical current subscriptions",
                            "canonical service access decision",
                            "canonical live-session evidence",
                            "canonical connection and outage diagnosis",
                            "canonical pending service change",
                            "UI status semantics",
                        ),
                    ),
                    ConcernContract(
                        name="pending service-change presentation",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical pending service change",
                            "canonical current subscriptions",
                        ),
                    ),
                    ConcernContract(
                        name="portal financial-position currency-lane projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical billing profile",
                            "canonical customer financial position",
                            "UI projection vocabulary",
                        ),
                    ),
                    ConcernContract(
                        name="mobile account-health transport projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical account-health projection",
                            "UI projection vocabulary",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical account state",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Subscriber identity and lifecycle status",
                    ),
                    AuthorityInput(
                        name="canonical current subscriptions",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "operationally-current Subscription identity, offer, "
                            "billing mode, and lifecycle status"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical billing profile",
                        owner="financial.billing_profile",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "effective account/subscription billing mode, source, "
                            "and invalid reason"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical customer financial position",
                        owner="customer.financial_position",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "currency-separated open receivables and reviewed prepaid "
                            "funding position"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical service access decision",
                        owner="customer.service_status",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "per-subscription usability, reason, charge/lapse dates, "
                            "and customer action"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical live-session evidence",
                        owner="network.radius_sessions",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "subscription-scoped online/stale/offline state, binding, "
                            "IP, NAS, and observation time"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical pending service change",
                        owner="service_intent.subscription_lifecycle",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "active SubscriptionChangeRequest intent, target offer, "
                            "effective date, lifecycle status, and owner-classified "
                            "delivery mode"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical connection and outage diagnosis",
                        owner="network.connection_health",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "customer-safe connection state, wording, access medium, "
                            "area-outage result, and checked time"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical service-health rows",
                        owner="ui.portal_account_health_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="the exact account's composed service-health rows",
                    ),
                    AuthorityInput(
                        name="canonical account-health projection",
                        owner="ui.portal_account_health_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="the exact account's composed Account Health DTO",
                    ),
                    AuthorityInput(
                        name="UI projection vocabulary",
                        owner="ui.projection_contracts",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="StateValue availability and freshness semantics",
                    ),
                    AuthorityInput(
                        name="UI status semantics",
                        owner="ui.status_presentation",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="semantic labels, tones, and icon keys",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "The projection reads on the adapter session and never polls, "
                        "mutates, commits, or rolls back."
                    ),
                    locking="Read projections acquire no mutation locks.",
                    idempotency=(
                        "The same account, current cohort, authoritative evidence, "
                        "and evaluation time produce the same projection."
                    ),
                    retries="Read-only projection calls are safe to retry.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner=(
                        "customer, reseller, and /api/v1/me account-health adapters"
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "customer dashboard generic balance/service-access arithmetic, "
                        "customer service page local RADIUS freshness, reseller account "
                        "detail open_balance/status mapping, and separate mobile "
                        "service-status and connection-status responses"
                    ),
                    new_owner="ui.portal_account_health_projection",
                    verification=(
                        "Financial separation, availability, multi-service session "
                        "non-leakage, pending service-change visibility, Customer 360 "
                        "reuse, shared-template, API cutover, mobile model, and query-"
                        "budget tests."
                    ),
                    cutover_gate=(
                        "Customer dashboard/detail, reseller account detail, Customer "
                        "360, and mobile consume the shared projection."
                    ),
                    fallback_retirement=(
                        "Generic balances, local freshness/status mapping, /me/service-"
                        "status, /me/connection-status, and the old mobile model are "
                        "absent."
                    ),
                ),
                steward="customer operations",
                design_refs=(
                    "docs/designs/PORTAL_ACCOUNT_SERVICE_HEALTH.md",
                    "docs/designs/CUSTOMER_SELF_SERVICE_LIFECYCLE.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_portal_account_health.py",
                    "tests/test_network_sot_services.py",
                    "tests/test_connection_health_ui_contract.py",
                    "mobile/test/models_test.dart",
                    "mobile/test/connection_status_test.dart",
                ),
            ),
        ),
        SOTService(
            name="ui.customer_network_path_projection",
            module="app.services.customer_network_path",
            owns=(
                "customer network path graph projection",
                "customer serving-endpoint presentation projection",
                "customer passive-fibre path detail projection",
                "customer geographic network path projection",
                "shared network graph view contract",
            ),
            depends_on=(
                "network.access_path",
                "network.fiber_topology",
                "customer.identity_scope",
                "ui.status_presentation",
            ),
            notes=(
                "network.access_path owns path identity, ordering, and "
                "gaps; observation owners own each hop's state and "
                "freshness; ui.status_presentation owns label/tone/icon "
                "meaning. This read owner composes those facts into the "
                "shared NetworkGraphView (app.services.network_graph) and "
                "the serving-endpoint presentation. It makes no topology, "
                "health, outage, or notification decision, performs no "
                "device I/O, and never manufactures a hop, an edge, or a "
                "status. The graph contract is the one vocabulary for the "
                "Customer 360 network path and the future network "
                "explorer surface."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="customer network path graph projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "subscription access-path resolution",
                            "semantic status presentation vocabulary",
                            "shared network graph vocabulary",
                        ),
                    ),
                    ConcernContract(
                        name=("customer serving-endpoint presentation projection"),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "subscription access-path resolution",
                            "semantic status presentation vocabulary",
                        ),
                    ),
                    ConcernContract(
                        name="customer passive-fibre path detail projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "validated fibre plant trace",
                            "semantic status presentation vocabulary",
                            "shared network graph vocabulary",
                        ),
                    ),
                    ConcernContract(
                        name="customer geographic network path projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "validated fibre plant trace",
                            "customer primary service address",
                        ),
                    ),
                    ConcernContract(
                        name="shared network graph view contract",
                        role=OwnerRole.POLICY,
                        input_names=("shared network graph vocabulary",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="subscription access-path resolution",
                        owner="network.access_path",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "resolved CustomerPath with AccessPathSummary "
                            "and SubscriberTopologyTrace identity, "
                            "ordering, hop states, evidence sources, "
                            "observation times, and typed breaks"
                        ),
                    ),
                    AuthorityInput(
                        name="validated fibre plant trace",
                        owner="network.fiber_topology",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "FiberSubscriptionTrace validated hop order, "
                            "evidence, splitter losses, and typed gap "
                            "codes; passive hops stay not-applicable, "
                            "never fabricated up/down"
                        ),
                    ),
                    AuthorityInput(
                        name="semantic status presentation vocabulary",
                        owner="ui.status_presentation",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "StatusPresentation label/tone/icon "
                            "projections for hop states, path gaps, "
                            "serving-endpoint sources, and RF signal "
                            "freshness"
                        ),
                    ),
                    AuthorityInput(
                        name="customer primary service address",
                        owner="customer.identity_scope",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "the customer account's selected primary Address latitude "
                            "and longitude; the map never geocodes or substitutes a "
                            "nearby asset"
                        ),
                    ),
                    AuthorityInput(
                        name="shared network graph vocabulary",
                        owner="ui.customer_network_path_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "NetworkGraphNode, NetworkGraphEdge, "
                            "NetworkGraphGap, NetworkGraphEvidence, "
                            "NetworkGraphMeasurement, and NetworkGraphView "
                            "typed invariants in app.services.network_graph"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Projects already-resolved access paths on the "
                        "adapter session without a business write and "
                        "without device, SSH, UISP, OLT, or ACS I/O."
                    ),
                    locking="Read projection acquires no mutation locks.",
                    idempotency=(
                        "The same resolved path, observations, and "
                        "presentation vocabulary produce the same graph "
                        "view and endpoint presentation."
                    ),
                    retries=(
                        "Read projection calls are safe to retry; a "
                        "failed resolution degrades to an explicit "
                        "unresolved projection per subscription."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="app.services.web_customer_details",
                ),
                projections=(
                    ProjectionContract(
                        name="customer network path graph projection",
                        input_names=(
                            "subscription access-path resolution",
                            "semantic status presentation vocabulary",
                            "shared network graph vocabulary",
                        ),
                        writer="ui.customer_network_path_projection",
                        freshness=(
                            "Recomputed on read; every hop retains its "
                            "owner's observed_at and freshness word, and "
                            "unknown, stale, unavailable, and "
                            "not-applicable stay distinct."
                        ),
                        stale_behavior=(
                            "Renders the owner's stale or unknown word "
                            "with its evidence age; it never converts "
                            "missing or aged observations into up or "
                            "down."
                        ),
                        drift_signal=(
                            "Customer network path projection and "
                            "template-boundary tests, and access-path "
                            "trace contract changes."
                        ),
                        rebuild_operation=(
                            "Recompute on read from the current "
                            "access-path resolution; nothing is "
                            "persisted."
                        ),
                        repair_owner="ui.customer_network_path_projection",
                    ),
                    ProjectionContract(
                        name="customer geographic network path projection",
                        input_names=(
                            "validated fibre plant trace",
                            "customer primary service address",
                        ),
                        writer="ui.customer_network_path_projection",
                        freshness=(
                            "Recomputed on customer-detail read from the current "
                            "primary service-address coordinates, validated fiber "
                            "trace, and mapped canonical asset coordinates."
                        ),
                        stale_behavior=(
                            "Missing coordinates or topology are emitted as explicit "
                            "map gaps; the projection never connects assets by "
                            "proximity or silently bridges an owner gap."
                        ),
                        drift_signal=(
                            "Customer geographic path contract tests and the fiber "
                            "topology owner's trace completeness evidence."
                        ),
                        rebuild_operation=(
                            "Recompute on read from the current address and fiber "
                            "topology records; nothing is persisted."
                        ),
                        repair_owner="ui.customer_network_path_projection",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "templates/admin/customers/detail.html inline "
                        "topology-trace tone mapping, endpoint-source "
                        "labels, and RF freshness styling"
                    ),
                    new_owner="ui.customer_network_path_projection",
                    verification=(
                        "Customer network path projection, presentation, "
                        "multi-subscription, query-budget, and "
                        "template-boundary tests."
                    ),
                    cutover_gate=(
                        "The customer detail template renders only "
                        "owner-provided presentations and composed "
                        "display strings for path hops, gaps, endpoint "
                        "source, and RF signal."
                    ),
                    fallback_retirement=(
                        "detail.html no longer maps hop states or "
                        "endpoint sources to colours or labels; the "
                        "inline node.state and endpoint_source label "
                        "branches are removed."
                    ),
                ),
                steward="network operations UI",
                design_refs=(
                    "docs/designs/CUSTOMER_NETWORK_PATH.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_customer_network_path.py",
                    "tests/test_customer_detail_access_endpoint.py",
                ),
            ),
        ),
        SOTService(
            name="ui.network_explorer_projection",
            module="app.services.network_explorer",
            owns=(
                "network explorer typed subject search",
                "network explorer subject-centred graph projection",
                "network explorer subject inspector projection",
                "network path coverage and drift projection",
            ),
            depends_on=(
                "network.identity",
                "network.access_path",
                "network.forwarding_topology",
                "network.device_state",
                "network.radio_signal",
                "network.outage_impact",
                "network.outage_lifecycle",
                "support.ticket_lifecycle",
                "ui.customer_network_path_projection",
                "ui.status_presentation",
            ),
            notes=(
                "Subject-centred, bounded neighbourhood graphs for "
                "/admin/network/explorer, restated in the shared "
                "NetworkGraphView contract. Composes the customer path "
                "projection, reviewed forwarding adjacency, the binary "
                "device verdict, ONT observation words, and audience "
                "cohorts. It decides no topology, health, outage, or "
                "consequence; never loads the whole fleet; groups "
                "fan-out into explicit cohort nodes; renders site "
                "containment as containment, never connectivity; and "
                "omits customer-identity kinds for viewers without "
                "customer:read."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="network explorer typed subject search",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "network inventory identity",
                            "semantic status presentation vocabulary",
                        ),
                    ),
                    ConcernContract(
                        name=("network explorer subject-centred graph projection"),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "network inventory identity",
                            "customer network path view",
                            "authoritative forwarding adjacency",
                            "binary device operation verdict",
                            "topological audience cohorts",
                            "semantic status presentation vocabulary",
                        ),
                    ),
                    ConcernContract(
                        name=("network explorer subject inspector projection"),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "network inventory identity",
                            "customer network path view",
                            "binary device operation verdict",
                            "effective RF signal",
                            "topological audience cohorts",
                            "live incident scope state",
                            "semantic status presentation vocabulary",
                        ),
                    ),
                    ConcernContract(
                        name=("network path coverage and drift projection"),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "per-subscription path gap classification",
                            "forwarding declaration evidence states",
                            "network inventory identity",
                            "unmatched-radio review queue state",
                            "semantic status presentation vocabulary",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="network inventory identity",
                        owner="network.identity",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "OLT, PON, ONT, CPE, NAS, FDH, splitter, "
                            "device, and site rows with their declared "
                            "relations, observation columns, and declared "
                            "topology links carrying capacity and "
                            "observed utilization"
                        ),
                    ),
                    AuthorityInput(
                        name="customer network path view",
                        owner="ui.customer_network_path_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "NetworkGraphView for a subscription subject "
                            "and the canonical asset deep-link map"
                        ),
                    ),
                    AuthorityInput(
                        name="authoritative forwarding adjacency",
                        owner="network.forwarding_topology",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "projected authoritative forwarding graph "
                            "adjacency and upstream mapping"
                        ),
                    ),
                    AuthorityInput(
                        name="binary device operation verdict",
                        owner="network.device_state",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "batch-annotated working/not_working verdicts "
                            "with machine reasons"
                        ),
                    ),
                    AuthorityInput(
                        name="topological audience cohorts",
                        owner="network.outage_impact",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "attached, provisioned, and served "
                            "subscription cohorts per node, basestation, "
                            "or cabinet"
                        ),
                    ),
                    AuthorityInput(
                        name="effective RF signal",
                        owner="network.radio_signal",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "value + source + explicit freshness + "
                            "reason for a radio's RF observation"
                        ),
                    ),
                    AuthorityInput(
                        name="per-subscription path gap classification",
                        owner="network.access_path",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "batched per-subscription medium and gap "
                            "classification contractually kept in sync "
                            "with resolve_customer_path"
                        ),
                    ),
                    AuthorityInput(
                        name="forwarding declaration evidence states",
                        owner="network.forwarding_topology",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "idempotent reconcile report state counts: "
                            "agreement, drift, missing observation, and "
                            "invalid declaration"
                        ),
                    ),
                    AuthorityInput(
                        name="unmatched-radio review queue state",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "open unmatched_radio tickets with creation "
                            "times for queue size and ageing"
                        ),
                    ),
                    AuthorityInput(
                        name="live incident scope state",
                        owner="network.outage_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "live OutageIncident rows scoped to a node, "
                            "basestation, or FDH cabinet with status and "
                            "lifecycle stamps"
                        ),
                    ),
                    AuthorityInput(
                        name="semantic status presentation vocabulary",
                        owner="ui.status_presentation",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "StatusPresentation label/tone/icon "
                            "projections for hop states, device "
                            "verdicts, and incident statuses"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Reads one bounded subject neighbourhood on the "
                        "adapter session without a business write and "
                        "without device, SSH, UISP, OLT, or ACS I/O."
                    ),
                    locking="Read projection acquires no mutation locks.",
                    idempotency=(
                        "The same inventory, adjacency, observations, and "
                        "subject produce the same search results and "
                        "graph view."
                    ),
                    retries=(
                        "Read projection calls are safe to retry; an "
                        "unprovable subject renders an explicit missing "
                        "state."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="app.web.admin.network_explorer",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="ui.network_explorer_projection",
                ),
                steward="network operations UI",
                design_refs=(
                    "docs/designs/NETWORK_EXPLORER.md",
                    "docs/designs/CUSTOMER_NETWORK_PATH.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_network_explorer.py",
                    "tests/architecture/test_thin_wrappers.py",
                ),
            ),
        ),
        SOTService(
            name="ui.status_presentation",
            module="app.services.status_presentation",
            owns=(
                "account status labels, semantic tones, and icon keys",
                "subscription status labels, semantic tones, and icon keys",
                "invoice status labels, semantic tones, and icon keys",
                "payment status labels, semantic tones, and icon keys",
                "outage incident status labels, semantic tones, and icon keys",
                "device operational status labels, semantic tones, and icon keys",
                "customer connection health labels, semantic tones, and icon keys",
                "RADIUS access-session observation labels, semantic tones, and icon keys",
                "service access availability labels, semantic tones, and icon keys",
                "access-path hop state labels, semantic tones, and icon keys",
                "access-path gap presentation semantics",
                "serving-endpoint source labels, semantic tones, and icon keys",
                "RF signal freshness labels, semantic tones, and icon keys",
                "service impact state labels, semantic tones, and icon keys",
                "SLA verdict labels, semantic tones, and icon keys",
                "support-ticket status labels, semantic tones, and icon keys",
                "field work-order status labels, semantic tones, and icon keys",
                "vendor installation-project status labels, semantic tones, and icon keys",
                "vendor quote status labels, semantic tones, and icon keys",
                "vendor proposed-route status labels, semantic tones, and icon keys",
                "vendor as-built status labels, semantic tones, and icon keys",
                "vendor material-release status labels, semantic tones, and icon keys",
                "vendor advance status labels, semantic tones, and icon keys",
                "supplier-invoice status labels, semantic tones, and icon keys",
                "status presentation fallback semantics",
            ),
            depends_on=(
                "customer.service_status",
                "financial.invoices",
                "financial.payments",
                "network.device_state",
                "network.connection_health",
                "network.outage_lifecycle",
                "network.access_path",
                "network.radio_signal",
                "support.ticket_lifecycle",
                "operations.work_order_status",
                "operations.vendor_project_lifecycle",
                "operations.vendor_project_workspace",
                "operations.vendor_material_release",
                "operations.vendor_advances",
                "integration.dotmac_erp_payables_adapter",
            ),
            notes=(
                "Domain services own lifecycle or derived operational state. "
                "This read projection owns its cross-client semantic meaning; "
                "customer.branding owns the concrete color behind each tone. "
                "Clients render the tone through brand/theme tokens and do not "
                "keep local tone-to-color maps."
            ),
        ),
    ),
    entrypoints=(
        "app.services.customer_network_path",
        "app.services.network_explorer",
        "app.services.network_graph",
        "app.schemas.catalog.SubscriptionRead",
        "app.schemas.billing.InvoiceRead",
        "app.schemas.billing.PaymentRead",
        "app.schemas.service_status.ServiceStatusItem",
        "app.schemas.support.TicketRead",
        "app.schemas.network_monitoring.NetworkDeviceRead",
        "app.services.crm_api.outage_incident_row",
        "app.services.web_customer_lists",
        "app.services.web_customer_details",
        "app.services.customer_portal_context",
        "app.schemas.field.FieldJobSummary",
        "app.schemas.field.FieldManagerJob",
        "app.services.field.map_search",
        "templates.admin.customers",
        "templates.admin.billing",
        "templates.admin.network.outages",
        "templates.admin.network.core-devices",
        "templates.admin.network.network-devices",
        "templates.admin.network.monitoring",
        "templates.customer.connection",
        "templates.reseller.dashboard",
        "templates.customer.dashboard.restricted",
        "templates.customer.billing",
        "templates.admin.support.tickets",
        "templates.customer.support",
        "mobile",
        "field_mobile",
    ),
    rule="Domain state owners provide raw or derived status values. Server read "
    "projections add one StatusPresentation label/tone/icon contract. "
    "Templates and mobile clients render that contract and do not map "
    "the same domain values independently.",
)
