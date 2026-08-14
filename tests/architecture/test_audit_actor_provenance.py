"""Ratchet adoption of the typed audit actor contract across every entry point."""

from __future__ import annotations

from pathlib import Path

from scripts.audit_actor_provenance_report import (
    AuditActorCallKind,
    AuditActorProvenanceBaseline,
    collect_audit_actor_calls,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = Path(__file__).with_name("audit_actor_provenance_baseline.json")


def test_legacy_audit_actor_callers_match_the_two_directional_baseline() -> None:
    report = collect_audit_actor_calls(PROJECT_ROOT)
    baseline = AuditActorProvenanceBaseline.from_path(BASELINE_PATH)

    assert report.legacy_call_count == baseline.legacy_call_count, (
        "Legacy scalar audit-actor call count changed. New debt is forbidden; "
        "when a caller is migrated, lower the baseline in the same reviewed change."
    )
    assert report.legacy_sites_sha256 == baseline.legacy_sites_sha256, (
        "Legacy audit-actor caller identities changed without an explicit baseline "
        "update; inspect the PII-free report before accepting the new inventory."
    )
    assert report.typed_call_count >= baseline.typed_call_count_floor


def test_rbac_assignment_owner_is_a_typed_actor_adopter() -> None:
    report = collect_audit_actor_calls(PROJECT_ROOT)

    assert any(
        site.kind is AuditActorCallKind.TYPED
        and site.path == "app/services/system_user_assignments.py"
        for site in report.sites
    )


def test_actor_provenance_detector_distinguishes_typed_and_legacy_calls(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "example.py").write_text(
        """
def legacy(db):
    stage_audit_event(db, action="legacy", entity_type="test")

def typed(db, actor):
    record_audit_event(db, action="typed", entity_type="test", actor=actor)
""",
        encoding="utf-8",
    )

    report = collect_audit_actor_calls(tmp_path)

    assert report.legacy_call_count == 1
    assert report.typed_call_count == 1
    assert {site.kind for site in report.sites} == {
        AuditActorCallKind.LEGACY,
        AuditActorCallKind.TYPED,
    }
