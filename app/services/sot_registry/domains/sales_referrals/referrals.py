"""sales_referrals SOT declarations: referrals."""

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
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="referrals.program",
        module="app.services.referrals",
        owns=(
            "Party-first Refer & Earn capture policy",
            "canonical Referral program record",
            "Referral Subscriber attachment record",
            "referral qualification and reward policy",
            "atomic referral program transition orchestration",
        ),
        depends_on=(
            "customer.accounts",
            "party.registry",
            "sales.lead_lifecycle",
            "access.subscription_lifecycle",
            "financial.credit_notes",
            "control.settings_spec",
            "events.dispatcher",
            "observability.audit_log",
        ),
        notes=(
            "Typed commands lock canonical Referral, ReferralCode, and "
            "Subscriber rows, call transaction-neutral Party, Lead, and "
            "credit-note collaborators, and stage PII-free audit/events "
            "before one commit. Contact observations never establish "
            "identity or attach an account."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="Party-first Refer & Earn capture policy",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "referral program policy settings",
                        "canonical referrer account state",
                        "canonical Party identity and reachability facts",
                    ),
                ),
                ConcernContract(
                    name="canonical Referral program record",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "referral program command evidence",
                        "referral program policy settings",
                        "canonical referrer account state",
                        "canonical Party identity and reachability facts",
                        "canonical attributed Lead state",
                    ),
                    canonical_writer="referrals.program",
                ),
                ConcernContract(
                    name="Referral Subscriber attachment record",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical Referral program record",
                        "canonical referred account state",
                        "canonical Party identity and reachability facts",
                        "canonical attributed Lead state",
                    ),
                    canonical_writer="referrals.program",
                ),
                ConcernContract(
                    name="referral qualification and reward policy",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical Referral program record",
                        "referral program policy settings",
                        "canonical subscriber activation state",
                        "canonical referral reward credit evidence",
                    ),
                ),
                ConcernContract(
                    name="atomic referral program transition orchestration",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "referral program command evidence",
                        "canonical Referral program record",
                        "referral program policy settings",
                        "canonical referrer account state",
                        "canonical referred account state",
                        "canonical Party identity and reachability facts",
                        "canonical attributed Lead state",
                        "canonical subscriber activation state",
                        "canonical referral reward credit evidence",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="referral program command evidence",
                    owner="referrals.program",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed CommandContext carrying actor, scope, reason, "
                        "command, correlation, causation, and idempotency evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical Referral program record",
                    owner="referrals.program",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active ReferralCode and Referral rows with Party, "
                        "Lead, Subscriber, lifecycle, reward snapshot, and "
                        "credit-link evidence"
                    ),
                ),
                AuthorityInput(
                    name="referral program policy settings",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "database-authoritative enablement, reward amount and "
                        "currency, qualification window, approval mode, and "
                        "share-base settings in the subscriber domain"
                    ),
                ),
                AuthorityInput(
                    name="canonical referrer account state",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="the exact Subscriber that owns the active referral code",
                ),
                AuthorityInput(
                    name="canonical referred account state",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "the exact reviewed Subscriber selected by conversion "
                        "or subscriber lifecycle evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical Party identity and reachability facts",
                    owner="party.registry",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "quarantined Person Party and unverified contact-point "
                        "observations; contacts are risk inputs, never identity keys"
                    ),
                ),
                AuthorityInput(
                    name="canonical attributed Lead state",
                    owner="sales.lead_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Party-bound Lead, immutable referral origin, and exact "
                        "reviewed Subscriber attachment evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscriber activation state",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "derived active Subscriber status or an exact active "
                        "Subscription observed from lifecycle events"
                    ),
                ),
                AuthorityInput(
                    name="canonical referral reward credit evidence",
                    owner="financial.credit_notes",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "owner-previewed issued CreditNote, exact legacy-compatible "
                        "referral reference, and funding-ledger link"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Each code, capture, qualification, rejection, or reward "
                    "command enters execute_owner_command on a transaction-free "
                    "adapter session. Referral state, collaborators, audit, and "
                    "events commit or roll back together."
                ),
                locking=(
                    "Code issuance locks the Subscriber; capture locks the exact "
                    "ReferralCode before retry comparison; transitions lock the "
                    "Referral before Subscriber or financial account state. "
                    "Database uniqueness arbitrates generated-code collisions."
                ),
                idempotency=(
                    "One active code per locked Subscriber, same-code plus exact "
                    "normalized contact-set capture replay, monotonic lifecycle "
                    "states, and the legacy referral:<UUID> credit reference "
                    "return stable outcomes without duplicate evidence or money."
                ),
                retries=(
                    "Rolled-back commands may retry with the same intent key. "
                    "Generated-code or serialization conflicts are retryable; "
                    "identity, lifecycle, policy, and financial conflicts require "
                    "review. Event delivery retries independently by event_id."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "referrals.program.invalid_command",
                    "referrals.program.invalid_configuration",
                    "referrals.program.program_disabled",
                    "referrals.program.subscriber_not_found",
                    "referrals.program.referral_not_found",
                    "referrals.program.code_not_found",
                    "referrals.program.contact_required",
                    "referrals.program.self_referral",
                    "referrals.program.existing_customer",
                    "referrals.program.incomplete_context",
                    "referrals.program.account_conflict",
                    "referrals.program.account_attachment_required",
                    "referrals.program.invalid_transition",
                    "referrals.program.invalid_reward",
                    "referrals.program.incomplete_reward_evidence",
                    "referrals.program.financial_conflict",
                    "referrals.program.collaboration_conflict",
                    "referrals.program.idempotency_conflict",
                    "referrals.program.invalid_filter",
                    "referrals.program.code_generation_exhausted",
                    "referrals.program.write_conflict",
                    "referrals.program.invalid_command_context",
                    "referrals.program.command_contract_violation",
                    "referrals.program.nested_owner_command",
                    "referrals.program.active_caller_transaction",
                    "referrals.program.nested_transaction_completion",
                ),
                mapping_owner=(
                    "app.api.crm_referrals, app.api.me, "
                    "app.web.admin.crm_referrals, app.web.customer.referrals, "
                    "and app.services.events.handlers.referral adapters"
                ),
                retryable_codes=(
                    "referrals.program.code_generation_exhausted",
                    "referrals.program.write_conflict",
                ),
                fail_closed_on=(
                    "missing or invalid canonical program settings",
                    "ambiguous identity or known self/existing-customer contact",
                    "incomplete Party, Lead, Subscriber, or reward evidence",
                    "invalid lifecycle transition or issued-credit conflict",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=(
                    "referral_code.issued",
                    "referral.captured",
                    "referral.subscriber_attached",
                    "referral.qualified",
                    "referral.expired",
                    "referral.rejected",
                    "referral.reward_issued",
                    "referral.reward_reconciled",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 contains canonical UUIDs, lifecycle/reward "
                    "outcome, bounded financial evidence, and command tracing. "
                    "It contains no name, email, phone, address, notes, reason "
                    "text, referral code, or bearer capability."
                ),
                replay=(
                    "Command replay emits no duplicate transition event. "
                    "The reward-issued event resolves through the canonical "
                    "notification template/channel policy and communication "
                    "intents deduplicate each event and channel."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "CRM referral mutation and an uncontracted service that "
                    "mixed HTTP errors, helper commits, direct push transport, "
                    "raw runtime environment fallback, and keyword mutations"
                ),
                new_owner="referrals.program",
                verification=(
                    "Focused code, capture, identity-risk, attachment, "
                    "qualification, reward, rollback, idempotency, audit, event, "
                    "adapter, policy, manifest, and architecture tests."
                ),
                cutover_gate=(
                    "Staff, public, customer API/web, and lifecycle-event writes "
                    "construct typed owner commands on transaction-free sessions."
                ),
                fallback_retirement=(
                    "CRM/write-through authority, service HTTP/commit/rollback, "
                    "direct push delivery, raw share-base environment reads, "
                    "and public keyword mutation entry points are removed."
                ),
            ),
            steward="customer operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/PARTY_FIRST_REFERRAL_CAPTURE.md",
                "docs/REFERRAL_ACCOUNT_CONVERSION.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_referrals_native.py",
                "tests/test_admin_referrals_web.py",
                "tests/test_customer_portal_referrals.py",
                "tests/architecture/test_referrals_program_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="referrals.account_conversion",
        module="app.services.referral_account_conversion",
        owns=(
            "stable Referral Party Lead conversion context validation",
            "atomic referral account creation and adjudication orchestration",
            "public referral signup capability purpose claims and lifetime",
        ),
        depends_on=(
            "customer.accounts",
            "party.registry",
            "sales.lead_lifecycle",
            "referrals.program",
            "auth.token_signing",
            "control.settings_spec",
            "events.dispatcher",
            "observability.audit_log",
        ),
        notes=(
            "Typed public and staff commands enter one verified coordinator "
            "transaction. The owner locks and revalidates exact UUID context, "
            "calls transaction-neutral record-owner collaborators, and stages "
            "PII-free audit and events before one commit. Public capability "
            "lifetime resolves only through the settings owner. Identity is "
            "never selected by contact observations."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=("stable Referral Party Lead conversion context validation"),
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical Referral conversion record",
                        "canonical referred Party identity",
                        "canonical attributed Lead state",
                    ),
                ),
                ConcernContract(
                    name=(
                        "atomic referral account creation and adjudication "
                        "orchestration"
                    ),
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "referral account conversion command evidence",
                        "canonical Referral conversion record",
                        "canonical referred Party identity",
                        "canonical attributed Lead state",
                        "canonical Subscriber account state",
                    ),
                ),
                ConcernContract(
                    name=(
                        "public referral signup capability purpose claims and lifetime"
                    ),
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical Referral conversion record",
                        "referral signup capability policy settings",
                        "verified public signup capability envelope",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="referral account conversion command evidence",
                    owner="referrals.account_conversion",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed CommandContext carrying actor, scope, reason, "
                        "command, correlation, causation, and idempotency "
                        "evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical Referral conversion record",
                    owner="referrals.program",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active Referral Party, Lead, referrer, attached "
                        "Subscriber, status, and complete binding evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical referred Party identity",
                    owner="party.registry",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "the exact active or quarantined Party row and its "
                        "canonical Subscriber binding"
                    ),
                ),
                AuthorityInput(
                    name="canonical attributed Lead state",
                    owner="sales.lead_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "the exact Party-bound Lead and its canonical "
                        "Subscriber attachment evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical Subscriber account state",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "transaction-neutral account initialization and the "
                        "exact existing or newly prepared Subscriber"
                    ),
                ),
                AuthorityInput(
                    name="referral signup capability policy settings",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "database-authoritative bounded referral signup "
                        "context expiry in the subscriber settings domain"
                    ),
                ),
                AuthorityInput(
                    name="verified public signup capability envelope",
                    owner="auth.token_signing",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "configured signing key and algorithm verification "
                        "for exact purpose, version, UUID, issued-at, and "
                        "expiry claims"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Each create or attach command enters "
                    "execute_owner_command on a transaction-free adapter "
                    "session. Subscriber preparation, Party binding, Lead "
                    "and Referral attachment, audit, subscriber.created, and "
                    "referral_account.converted commit or roll back together."
                ),
                locking=(
                    "The exact Referral, Party, Lead, and any existing "
                    "Subscriber are selected FOR UPDATE in canonical order. "
                    "Referral serialization and database identity constraints "
                    "arbitrate concurrent account creation and attachment."
                ),
                idempotency=(
                    "The Referral row is the natural conversion key. An exact "
                    "replay returns its already attached Subscriber without a "
                    "second account, audit row, or conversion event; a "
                    "different account or Party fails closed."
                ),
                retries=(
                    "Adapters may retry a rolled-back command with the same "
                    "intent key after transient database failure. Canonical "
                    "context conflicts require review; outbox delivery retries "
                    "independently."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "referrals.account_conversion.invalid_command",
                    "referrals.account_conversion.invalid_configuration",
                    "referrals.account_conversion.invalid_capability",
                    "referrals.account_conversion.context_not_found",
                    "referrals.account_conversion.incomplete_context",
                    "referrals.account_conversion.stale_context",
                    "referrals.account_conversion.context_not_convertible",
                    "referrals.account_conversion.subscriber_not_found",
                    "referrals.account_conversion.account_conflict",
                    "referrals.account_conversion.self_referral",
                    ("referrals.account_conversion.invalid_command_context"),
                    ("referrals.account_conversion.command_contract_violation"),
                    "referrals.account_conversion.nested_owner_command",
                    ("referrals.account_conversion.active_caller_transaction"),
                    ("referrals.account_conversion.nested_transaction_completion"),
                ),
                mapping_owner=(
                    "app.api.crm_referrals and app.web.admin.crm_referrals adapters"
                ),
                fail_closed_on=(
                    "missing or altered Referral, Party, or Lead context",
                    "incomplete binding evidence",
                    "different Party, Subscriber, or self-referral",
                    "invalid or expired public capability",
                    "missing or invalid canonical lifetime policy",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=(
                    "subscriber.created",
                    "referral_account.converted",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 contains canonical UUIDs, conversion outcome, "
                    "and command/correlation evidence only. It never contains "
                    "name, email, phone, address, reason text, or bearer "
                    "capability."
                ),
                replay=(
                    "Events are immutable committed evidence. Consumers "
                    "deduplicate by event_id; command replay converges on the "
                    "Referral's canonical attached Subscriber."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "uncontracted keyword service functions using savepoints, "
                    "helper commits, status-coded errors, and adapter-owned "
                    "transaction handoff"
                ),
                new_owner="referrals.account_conversion",
                verification=(
                    "Focused create, attach, public capability, stale-context, "
                    "self-referral, idempotency, rollback, event, audit, "
                    "adapter, policy, manifest, and architecture tests."
                ),
                cutover_gate=(
                    "Public API, staff API, and admin web conversion surfaces "
                    "construct only typed owner commands on transaction-free "
                    "sessions."
                ),
                fallback_retirement=(
                    "Service commits, savepoints, FastAPI errors, keyword "
                    "mutation entry points, hardcoded capability lifetime, and "
                    "post-conversion adapter transaction completion are removed."
                ),
            ),
            steward="customer operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/REFERRAL_ACCOUNT_CONVERSION.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_referral_account_conversion.py",
                "tests/test_referral_self_service_signup.py",
                ("tests/architecture/test_referral_account_conversion_boundary.py"),
            ),
        ),
    ),
)
