"""financial_access SOT declarations: customer subledger."""

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
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="financial.customer_subledger",
        module="app.services.billing.customer_subledger",
        owns=(
            "append-only customer posting groups",
            "customer posting reversal chain",
            "typed per-currency subledger position",
        ),
        depends_on=(
            "billing.obligations",
            "customer.accounts",
            "events.dispatcher",
        ),
        notes=(
            "ADR 0007 Phase 3. One business result stages exactly one "
            "immutable posting group as a required flush-only "
            "participant inside the deciding owner's transaction. "
            "Position is derived only from these postings per currency "
            "and semantic lane; effects are operational meanings, not "
            "ERP debits/credits, and Dotmac ERP keeps the general "
            "ledger. Wrong postings are reversed by a linked group, "
            "never edited."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="append-only customer posting groups",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "deciding owner command evidence",
                        "recorded customer postings",
                    ),
                    canonical_writer="financial.customer_subledger",
                ),
                ConcernContract(
                    name="customer posting reversal chain",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("recorded customer postings",),
                    canonical_writer="financial.customer_subledger",
                ),
                ConcernContract(
                    name="typed per-currency subledger position",
                    role=OwnerRole.RESOLVER,
                    input_names=("recorded customer postings",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="deciding owner command evidence",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "account identity plus the calling owner's active "
                        "command context (actor, reason, idempotency key)"
                    ),
                ),
                AuthorityInput(
                    name="recorded customer postings",
                    owner="financial.customer_subledger",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "customer_posting_groups and customer_position_effects rows"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "stage_posting_group and stage_reversal run only "
                    "inside another owner's active execute_owner_command "
                    "transaction, use flush only, and never commit or "
                    "roll back. Calling them outside an owner command "
                    "fails closed."
                ),
                locking=(
                    "The calling owner holds its canonical account/record "
                    "locks; the idempotency unique constraint serialises "
                    "duplicate posting attempts."
                ),
                idempotency=(
                    "One posting group per (producer owner, business "
                    "idempotency key); a replay returns the original "
                    "group. A group has at most one reversal."
                ),
                retries=(
                    "The calling owner retries its complete command; a "
                    "posting group is never partially visible because it "
                    "commits atomically with the business result."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.customer_subledger.command_contract_violation",
                    "financial.customer_subledger.invalid_effect_amount",
                    "financial.customer_subledger.invalid_posting_currency",
                    "financial.customer_subledger.invalid_posting_instant",
                    "financial.customer_subledger.idempotency_conflict",
                    "financial.customer_subledger.missing_idempotency_key",
                    "financial.customer_subledger.posting_group_already_reversed",
                    "financial.customer_subledger.posting_group_not_found",
                    "financial.customer_subledger.posting_requires_owner_command",
                ),
                mapping_owner="the deciding money owners and their adapters",
                fail_closed_on=(
                    "staging outside an active owner command",
                    "a non-positive effect amount",
                    "a second reversal of one posting group",
                ),
            ),
            events=EventContract(
                event_types=("financial.customer_posting.committed",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive. Consumers project committed "
                    "postings and never re-decide why money moved."
                ),
                replay=(
                    "Rebuildable from customer_posting_groups. Phase 3 is "
                    "shadow and stages no delivery; ADR 0007 Phase 4 adds "
                    "the transactional outbox and consumer receipts."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner=(
                    "financial.ledger per-entry rows plus the multi-source "
                    "customer.financial_position document-union formulas"
                ),
                new_owner="financial.customer_subledger",
                verification=(
                    "Participant boundary, idempotent replay, reversal "
                    "chain, and per-lane position tests plus the ADR 0007 "
                    "guards."
                ),
                cutover_gate=(
                    "ADR 0007 Phase 3 gate: every new money-changing path "
                    "produces one posting group, per-currency/lane shadow "
                    "differences are zero for the approved observation "
                    "window, and finance signs the cohort evidence."
                ),
                fallback_retirement=(
                    "Document-union balance formulas, the account-credit "
                    "special formula, and legacy financial.ledger writer "
                    "paths are removed after cutover."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_customer_subledger.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
    SOTService(
        name="financial.customer_subledger_opening_positions",
        module="app.services.billing.subledger_opening",
        owns=(
            "reviewed customer-subledger opening-position capture",
            "customer-subledger authority cutover activation",
        ),
        depends_on=(
            "billing.shadow_verification",
            "customer.accounts",
            "events.dispatcher",
            "financial.customer_subledger",
            "financial.prepaid_funding_reconstruction",
        ),
        notes=(
            "ADR 0007 Phase 3 migration owner. It captures only the exact "
            "operator- and finance-approved verifier result. Each immutable "
            "opening evidence row and shadow posting group commit together. "
            "The residual is verified legacy position minus already-recorded "
            "shadow position at the preview cutoff, so forward groups are not "
            "double-counted. The complete source cohort is mandatory. A later "
            "completion run preserves existing immutable openings and captures "
            "only missing accounts; no account is permanently excluded. After "
            "authority activation, a distinct single-account completion may "
            "capture one explicitly selected native-after-handoff account "
            "against the original cutoff without reading or changing unrelated "
            "opening debt. It is forbidden before activation and cannot support "
            "the initial cutover gate."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=("reviewed customer-subledger opening-position capture"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "approved opening-position verification run",
                        "verified prepaid funding position",
                        "recorded customer postings",
                        "canonical customer account",
                    ),
                    canonical_writer=("financial.customer_subledger_opening_positions"),
                ),
                ConcernContract(
                    name="customer-subledger authority cutover activation",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "approved subledger parity verification run",
                        "recorded customer postings",
                    ),
                    canonical_writer=("financial.customer_subledger_opening_positions"),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="approved opening-position verification run",
                    owner="billing.shadow_verification",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "immutable phase_3_opening_preview run with exact "
                        "result fingerprint plus operator and finance approval, "
                        "or one immutable phase_3_post_cutover_opening_preview "
                        "bound to an explicit account and the active authority "
                        "record"
                    ),
                ),
                AuthorityInput(
                    name="verified prepaid funding position",
                    owner="financial.prepaid_funding_reconstruction",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "reviewed active baseline/opening plus canonical later "
                        "native facts, or the fingerprinted native-after-"
                        "handoff zero-history target used only by opening "
                        "verification"
                    ),
                ),
                AuthorityInput(
                    name="recorded customer postings",
                    owner="financial.customer_subledger",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "immutable shadow posting groups and typed effects "
                        "recorded before the preview cutoff"
                    ),
                ),
                AuthorityInput(
                    name="canonical customer account",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Subscriber identity and currency scope",
                ),
                AuthorityInput(
                    name="approved subledger parity verification run",
                    owner="billing.shadow_verification",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "immutable phase_3_subledger_parity run with zero "
                        "blockers plus operator and finance approval"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "The fingerprint-bound capture enters execute_owner_command "
                    "once; every opening evidence row, posting group, and capture "
                    "event commits or rolls back as one transaction. A bounded "
                    "post-cutover capture uses the same owner transaction and "
                    "never updates the authority record."
                ),
                locking=(
                    "The approved verification run is locked before capture; "
                    "a post-cutover run then locks and recomputes its one selected "
                    "account against the immutable original cutoff; "
                    "unique account/currency and posting idempotency constraints "
                    "arbitrate concurrent attempts."
                ),
                idempotency=(
                    "One immutable opening per account/currency. Exact replay of "
                    "the same reviewed run returns the recorded cohort; changed "
                    "rows, selected-account evidence, or fingerprints fail closed."
                ),
                retries=(
                    "Retry the complete command with the same approved run and "
                    "idempotency key only after rollback."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes(
                        "financial.customer_subledger_opening_positions"
                    ),
                    (
                        "financial.customer_subledger_opening_positions."
                        "approval_required"
                    ),
                    (
                        "financial.customer_subledger_opening_positions."
                        "authority_already_activated"
                    ),
                    (
                        "financial.customer_subledger_opening_positions."
                        "corrupt_reviewed_preview"
                    ),
                    (
                        "financial.customer_subledger_opening_positions."
                        "idempotency_conflict"
                    ),
                    (
                        "financial.customer_subledger_opening_positions."
                        "invalid_result_fingerprint"
                    ),
                    (
                        "financial.customer_subledger_opening_positions."
                        "missing_idempotency_key"
                    ),
                    (
                        "financial.customer_subledger_opening_positions."
                        "missing_review_reference"
                    ),
                    (
                        "financial.customer_subledger_opening_positions."
                        "opening_position_already_captured"
                    ),
                    (
                        "financial.customer_subledger_opening_positions."
                        "source_cohort_incomplete"
                    ),
                    (
                        "financial.customer_subledger_opening_positions."
                        "stale_reviewed_preview"
                    ),
                    (
                        "financial.customer_subledger_opening_positions."
                        "verification_run_not_found"
                    ),
                ),
                mapping_owner="billing migration command adapters",
                fail_closed_on=(
                    "missing operator or finance approval",
                    "changed or corrupt reviewed fingerprint",
                    "an incomplete source cohort",
                    "a stale or ineligible selected post-cutover account",
                    "an existing account/currency opening",
                ),
            ),
            events=EventContract(
                event_types=(
                    "customer_subledger.opening_positions_captured",
                    "customer_subledger.authority_activated",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries run, fingerprint, currency, captured count, "
                    "the compatibility quarantined_count fixed at zero, and "
                    "authority_moved=false."
                ),
                replay=(
                    "Rebuild consumers from immutable opening evidence and "
                    "posting groups; capture replay emits no second event."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUTOVER_READY,
                old_owner=(
                    "implicit runtime baseline plus multi-source legacy balance "
                    "formula with no subledger opening posting"
                ),
                new_owner=("financial.customer_subledger_opening_positions"),
                verification=(
                    "Fingerprint-bound complete-cohort preview/capture, "
                    "post-cutover single-account revalidation, atomic rollback, "
                    "idempotency, and cohort parity tests."
                ),
                cutover_gate=(
                    "Every account present at subledger activation has one "
                    "reviewed opening, later native accounts start at zero, "
                    "per-account/currency/lane variance is zero, all "
                    "forward facts are covered, and finance approves the run."
                ),
                fallback_retirement=(
                    "Retire this migration command after read cutover; opening "
                    "postings remain immutable history."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_subledger_opening_positions.py",
                "tests/architecture/test_customer_subledger_ownership.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
)
