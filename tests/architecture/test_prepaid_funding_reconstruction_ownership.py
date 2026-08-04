"""Guard the final prepaid funding authority boundary."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_PREPAID_TIMER_FIELDS = {
    "prepaid_low_balance_at",
    "prepaid_deactivation_at",
}
_PREPAID_TIMER_OWNER = "app/services/prepaid_enforcement_state.py"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _read_migration(suffix: str) -> str:
    matches = list((ROOT / "alembic" / "versions").glob(f"*_{suffix}.py"))
    assert len(matches) == 1, matches
    return matches[0].read_text(encoding="utf-8")


def _attribute_targets(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Attribute) and target.attr in _PREPAID_TIMER_FIELDS:
        return {target.attr}
    if isinstance(target, (ast.List, ast.Tuple)):
        return set().union(*(_attribute_targets(item) for item in target.elts))
    return set()


def test_prepaid_timer_fields_have_one_canonical_writer() -> None:
    writers = {field: set() for field in _PREPAID_TIMER_FIELDS}
    for path in (ROOT / "app").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: set[str] = set()
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    targets.update(_attribute_targets(target))
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets.update(_attribute_targets(node.target))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _PREPAID_TIMER_FIELDS
            ):
                targets.add(str(node.args[1].value))
            for field in targets:
                writers[field].add(relative)

    assert writers == {field: {_PREPAID_TIMER_OWNER} for field in _PREPAID_TIMER_FIELDS}


def test_reconstruction_models_have_one_writer_owner() -> None:
    constructors = {
        "PrepaidFundingReconstructionBatch(": [],
        "PrepaidFundingBaseline(": [],
    }
    for path in (ROOT / "app").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "app/models/prepaid_funding.py":
            continue
        source = path.read_text(encoding="utf-8")
        for constructor in constructors:
            if constructor in source:
                constructors[constructor].append(relative)

    assert constructors == {
        "PrepaidFundingReconstructionBatch(": [
            "app/services/prepaid_funding_reconstruction.py"
        ],
        "PrepaidFundingBaseline(": ["app/services/prepaid_funding_reconstruction.py"],
    }


def test_runtime_has_no_legacy_authority_toggle_or_fallback() -> None:
    position = _read("app/services/customer_financial_position.py")
    owner = _read("app/services/prepaid_funding_reconstruction.py")
    ledger = _read("app/services/customer_financial_ledger.py")
    planner = _read("app/services/prepaid_enforcement_planner.py")
    planner_script = _read("scripts/one_off/plan_prepaid_balance_sweep.py")
    settings = _read("app/services/settings_spec.py")

    assert "verified_prepaid_funding_balance" in position
    assert "legacy_projection" not in position
    assert "prepaid_funding_authority" not in settings
    assert "SplynxBillingTransaction" not in owner
    assert "native_customer_financial_balances_by_currency" in owner
    assert "list_customer_financial_events" not in owner
    assert "SplynxBillingTransaction" not in ledger
    assert "include_legacy_mirror" not in ledger
    assert "_has_legacy_mirror" not in ledger
    assert "INTERNAL_MEMO_PREFIXES" not in ledger
    assert "affects_customer_position" in ledger
    assert "prepaid_subscription_invoice_ids" in ledger
    assert "_exactly_settled_invoice_ids" in ledger
    assert "_paid_prepaid_consumption_filter" in ledger
    assert "exact_direct_renewal_debit_precedence" in ledger
    assert "PrepaidFundingSnapshot" not in planner
    assert "funding_snapshot" not in planner
    assert "--funding-snapshot" not in planner_script


def test_legacy_subscriber_deposit_is_not_an_api_balance_surface() -> None:
    from app.schemas.subscriber import (
        SubscriberCreate,
        SubscriberRead,
        SubscriberUpdate,
    )

    for schema in (SubscriberCreate, SubscriberUpdate, SubscriberRead):
        assert "deposit" not in schema.model_fields


def test_funded_inactive_audit_uses_verified_prepaid_funding() -> None:
    audit = _read("app/services/funded_inactive_exposure.py")

    assert "prepaid_available_balances" in audit
    assert "s.deposit" not in audit
    assert "ledger_entries" not in audit


def test_materializer_requires_explicit_final_cutover_acknowledgement() -> None:
    script = _read("scripts/one_off/materialize_prepaid_funding_reconstruction.py")
    migration = _read_migration("prepaid_funding_reconstruction")

    assert "MATERIALIZE_VERIFIED_PREPAID_FUNDING" in script
    assert "--confirm-final-cutover" in script
    assert "authority cutover is final" in migration


def test_materializer_owner_requires_a_config_trusted_clean_replay_seal() -> None:
    exporter = _read("scripts/one_off/export_prepaid_funding_snapshot.py")
    materializer = _read(
        "scripts/one_off/materialize_prepaid_funding_reconstruction.py"
    )
    owner = _read("app/services/prepaid_funding_reconstruction.py")
    attestation = _read("app/services/prepaid_funding_attestation.py")
    settings = _read("app/services/settings_spec.py")

    assert "--signing-key-ref" in exporter
    assert "sealed_funding_payload" in exporter
    assert "is_openbao_ref" in exporter
    assert "required_balance" not in exporter
    assert "resolve_prepaid_thresholds" not in exporter
    assert "verify_prepaid_funding_manifest" in owner
    assert "reconstruction_existing_attestation_mismatch" in owner
    assert "reconstruction_source_cohort_incomplete" in owner
    assert "expected_candidate_hash = candidate_cohort_sha256" in owner
    assert "resolve_splynx_history_opening_targets" in exporter
    assert "--allow-quarantined-subset" not in exporter
    assert "source_cohort_incomplete" in _read(
        "app/services/billing/splynx_history_opening.py"
    )
    assert "apply_prepaid_funding_reconstruction" in materializer
    assert "Ed25519PublicKey" in attestation
    assert "is_openbao_ref" in attestation
    assert "prepaid_reconstruction_attestation_public_key_ref" in settings


def test_complete_history_reader_and_retired_quarantine_name_have_one_app_home() -> (
    None
):
    root = Path(__file__).resolve().parents[2]
    app_files = tuple((root / "app").rglob("*.py"))

    history_readers = [
        path.relative_to(root).as_posix()
        for path in app_files
        if "audit_splynx_final_balances" in path.read_text(encoding="utf-8")
        and path.name != "sot_relationships.py"
    ]
    quarantine_name_homes = [
        path.relative_to(root).as_posix()
        for path in app_files
        if "prepaid_funding_quarantined_account_ids" in path.read_text(encoding="utf-8")
    ]

    assert history_readers == ["app/services/billing/splynx_history_opening.py"]
    assert quarantine_name_homes == ["app/services/prepaid_funding_reconstruction.py"]


def test_gap_adjudication_cannot_write_money_or_override_replay() -> None:
    script = _read("scripts/one_off/adjudicate_prepaid_funding_gaps.py")

    assert "SessionLocal" not in script
    assert "Payment(" not in script
    assert "--apply" not in script
    assert "financial.payments" in script
    assert "blocked_pending_owner_actions_and_independent_replay" in script
    assert "amount/date coincidence is insufficient" in script


def test_audit_restore_is_isolated_and_cannot_reach_the_live_database() -> None:
    """The audit restore holds a full copy of production. Keep it fenced.

    The exporter's ``_require_ephemeral_postgres`` guard is only as good as the
    thing that provisions the database it runs against. This asserts the
    provisioner cannot be quietly repointed at the live stack, cannot expose
    the copy, and cannot itself write a baseline.
    """
    script = _read("scripts/one_off/prepaid_funding_audit_restore.sh")

    # Refuses any database name the exporter would refuse, and refuses to
    # adopt the live database container under any of its fleet names.
    assert '"${AUDIT_DB}" == *_audit' in script
    for reserved in ("dotmac_pg_local", "dotmac_sub_db", "postgres-local"):
        assert reserved in script, f"{reserved} must be in the refused-name list"
    assert "must not be the live database container" in script

    # The restored copy is unreachable: internal network, no published ports.
    # Trust auth is only defensible alongside both, so they are asserted
    # together -- weakening either one must fail this test.
    assert "--internal" in script
    assert "--publish" not in script
    assert "POSTGRES_HOST_AUTH_METHOD=trust" in script

    # Teardown destroys the copy rather than merely stopping it.
    assert "docker volume rm" in script

    # Provisioning is not a write path for funding authority.
    assert "apply_prepaid_funding_reconstruction" not in script
    assert "materialize_prepaid_funding_reconstruction" not in script
    assert "--confirm-final-cutover" not in script


def test_audit_restore_runbook_documents_complete_history_completion() -> None:
    runbook = _read("docs/runbooks/PREPAID_FUNDING_AUDIT_RESTORE.md")

    assert "complete empty transaction set is zero" in runbook
    assert "There is no partial-subset option" in runbook
    assert "BILLING_AUDIT_EPHEMERAL" in runbook
    assert "_audit" in runbook
    # The two constraints that most often derail a first attempt.
    assert "cohort-complete" in runbook
    assert "reconstruction_position_not_newer" in runbook
    assert "Do not leave the audit stack running" in runbook
