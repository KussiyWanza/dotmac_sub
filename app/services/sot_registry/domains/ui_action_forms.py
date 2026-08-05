"""Canonical SOT declarations for the ui_action_forms domain."""

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
    domain="ui_action_forms",
    services=(
        SOTService(
            name="ui.action_form_contracts",
            module="app.services.action_forms",
            owns=(
                "action visibility and disabled-reason projection",
                "action impact and confirmation presentation",
                "action field and option metadata",
                "owner-produced hidden action evidence transport",
                "submitted action values and structured error binding",
            ),
            notes=(
                "Domain command services still own authorization, eligibility, "
                "validation, locking, execution, and audit consequences."
            ),
        ),
        SOTService(
            name="ui.invoice_batch_action_projection",
            module="app.services.web_billing_invoice_batch",
            owns=(
                "admin invoice batch exact-scope preview",
                "admin invoice batch fingerprint and confirmation projection",
                "admin billing-run retry eligibility presentation",
            ),
            depends_on=(
                "ui.action_form_contracts",
                "financial.billing_automation",
                "auth.permission_gate",
            ),
            notes=(
                "The projection performs a side-effect-free billing-owner dry "
                "resolution and renders its exact membership and evidence. "
                "Billing automation remains the execution and BillingRun owner."
            ),
            contract=ServiceContract(
                concerns=tuple(
                    ConcernContract(
                        name=name,
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical invoice batch dry-run facts",
                            "authorized billing staff scope",
                        ),
                    )
                    for name in (
                        "admin invoice batch exact-scope preview",
                        ("admin invoice batch fingerprint and confirmation projection"),
                        "admin billing-run retry eligibility presentation",
                    )
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical invoice batch dry-run facts",
                        owner="financial.billing_automation",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "exact postpaid subscription/account/period/currency/"
                            "amount membership and BillingRun lifecycle facts"
                        ),
                    ),
                    AuthorityInput(
                        name="authorized billing staff scope",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="billing:batch:read/write principal and visibility",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Preview, fingerprint, retry eligibility, and ActionForm "
                        "projection do not commit or retain ORM mutation."
                    ),
                    locking=(
                        "No projection locks; billing automation recomputes the "
                        "fingerprint immediately before durable launch."
                    ),
                    idempotency=(
                        "Equivalent owner facts and normalized scope produce the "
                        "same deterministic fingerprint and form."
                    ),
                    retries=(
                        "Read failures may be retried; stale fingerprints require "
                        "a fresh operator review."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "ui.invoice_batch_action_projection.invalid_cycle",
                        "ui.invoice_batch_action_projection.invalid_date",
                        "ui.invoice_batch_action_projection.retry_ineligible",
                        "ui.invoice_batch_action_projection.stale_preview",
                        "ui.invoice_batch_action_projection.empty_scope",
                        "ui.invoice_batch_action_projection.unauthorized",
                    ),
                    mapping_owner="administrative invoice batch adapter",
                    retryable_codes=(
                        "ui.invoice_batch_action_projection.stale_preview",
                    ),
                    fail_closed_on=(
                        "missing permission",
                        "changed exact membership",
                        "empty current scope",
                        "non-failed retry source",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "invoice batch Jinja/Alpine confirmation, incomplete JSON "
                        "preview, and raw all-status retry form"
                    ),
                    new_owner="ui.invoice_batch_action_projection",
                    verification=(
                        "exact preview, fingerprint drift, confirmation, retry "
                        "eligibility, template, route, and architecture tests"
                    ),
                    cutover_gate=(
                        "Every manual/retry launch renders the shared ActionForm "
                        "with current exact owner evidence."
                    ),
                    fallback_retirement=(
                        "Browser dialogs, direct execute form, incomplete JSON "
                        "preview, and non-failed retry controls are absent."
                    ),
                ),
                steward="billing operations UI",
                design_refs=(
                    "docs/designs/INVOICE_BATCH_AND_REMINDER_SAFE_ACTIONS.md",
                    "docs/FRONTEND_SPEC.md",
                ),
                test_refs=(
                    "tests/test_billing_invoice_batch_web.py",
                    "tests/test_billing_invoice_templates.py",
                    "tests/architecture/test_action_form_ownership.py",
                ),
            ),
        ),
        SOTService(
            name="ui.payment_proof_review_projection",
            module="app.services.web_billing_payment_proofs",
            owns=(
                "payment-proof review action visibility",
                "payment-proof verify and reject form projection",
                "payment-proof duplicate-correction action projection",
                "payment-proof failed-submission presentation",
                "payment-proof reviewer identity display projection",
            ),
            depends_on=(
                "ui.action_form_contracts",
                "financial.payment_proofs",
            ),
        ),
        SOTService(
            name="ui.service_extension_detail_projection",
            module="app.services.web_billing_service_extensions",
            owns=(
                "admin service-extension detail projection",
                "exact service-extension activity presentation",
                "service-extension status and action presentation",
            ),
            depends_on=(
                "auth.permission_gate",
                "auth.staff_provisioning",
                "financial.service_extensions",
                "observability.audit_log",
                "ui.display_formatting",
            ),
            notes=(
                "One typed read owner composes lifecycle facts, exact entity-linked "
                "audit evidence, actor labels, defensible legacy provenance, impact, "
                "status presentation, and permission-aware transition visibility. "
                "It never treats request-path audits as entity history and never "
                "exposes raw audit metadata through the billing-read page."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="admin service-extension detail projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical service-extension lifecycle facts",
                            "canonical service-extension activity evidence",
                            "canonical staff display identity",
                            "service-extension permission result",
                            "application display-timezone policy",
                            "service-extension presentation policy",
                        ),
                    ),
                    ConcernContract(
                        name="exact service-extension activity presentation",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical service-extension lifecycle facts",
                            "canonical service-extension activity evidence",
                            "canonical staff display identity",
                            "application display-timezone policy",
                        ),
                    ),
                    ConcernContract(
                        name="service-extension status and action presentation",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical service-extension lifecycle facts",
                            "service-extension permission result",
                            "service-extension presentation policy",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical service-extension lifecycle facts",
                        owner="financial.service_extensions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "ServiceExtension aggregate, immutable entries, selected "
                            "scope, and sampled affected subscriptions"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical service-extension activity evidence",
                        owner="observability.audit_log",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "AuditEvent rows filtered by exact "
                            "entity_type=service_extension and exact extension UUID"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical staff display identity",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "write-time audit actor-label snapshot, with canonical "
                            "SystemUser lookup only for legacy lifecycle columns"
                        ),
                    ),
                    AuthorityInput(
                        name="service-extension permission result",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "billing:extension:read and billing:extension:apply "
                            "request authorization"
                        ),
                    ),
                    AuthorityInput(
                        name="application display-timezone policy",
                        owner="ui.display_formatting",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="application timezone and timestamp display formatter",
                    ),
                    AuthorityInput(
                        name="service-extension presentation policy",
                        owner="ui.service_extension_detail_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed status, action, activity-label, ordering, and "
                            "legacy-provenance policy"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "The projection reads on the adapter-owned session and never "
                        "mutates, flushes, commits, or rolls back."
                    ),
                    locking=(
                        "No mutation lock is acquired; state-changing command owners "
                        "lock and recheck lifecycle eligibility."
                    ),
                    idempotency=(
                        "The same extension, exact audit cohort, staff identities, "
                        "permissions, and evaluation time produce the same typed "
                        "projection and deterministic activity order."
                    ),
                    retries="The bounded read-only projection is safe to retry.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="admin billing service-extension adapter",
                    fail_closed_on=(
                        "missing lifecycle permission",
                        "ambiguous or absent canonical extension identity",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "admin billing route audit queries, template-local status "
                        "mapping, lifecycle eligibility, and path-based activity"
                    ),
                    new_owner="ui.service_extension_detail_projection",
                    verification=(
                        "Exact filtering, activity provenance, actor snapshot, "
                        "permission, deterministic ordering, template, mobile layout, "
                        "and route-delegation tests."
                    ),
                    cutover_gate=(
                        "The detail route passes one typed projection and the template "
                        "renders owner-provided status and action eligibility."
                    ),
                    fallback_retirement=(
                        "Route/template audit queries, actor lookup, status maps, "
                        "eligibility decisions, and misleading broad audit links are "
                        "absent."
                    ),
                ),
                steward="billing operations UI",
                design_refs=(
                    "docs/designs/SERVICE_EXTENSION_LIFECYCLE_SOT.md",
                    "docs/FRONTEND_SPEC.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_web_billing_service_extensions.py",
                    "tests/architecture/test_service_extension_sot_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.web.admin.billing_invoice_batch",
        "app.web.admin.billing_invoice_bulk",
        "app.web.admin.billing_payment_proofs",
        "app.web.admin.billing_extensions",
        "templates.admin.billing.invoice_batch",
        "templates.admin.billing.invoice_bulk_review",
        "templates.admin.billing.payment_proof_detail",
        "templates.admin.billing.service_extension_detail",
        "templates.components.forms.action_form",
        "templates.components.ui.timeline_item",
    ),
    rule="Action forms render owner-provided eligibility, impact, confirmation, "
    "declared fields, submitted values, and structured errors. Unauthorized "
    "actions are omitted. Routes remain adapters, and command owners lock "
    "and recheck permission and eligibility before mutation.",
)
