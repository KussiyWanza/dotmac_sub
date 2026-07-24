"""The runtime-posture screen reports what the runtime would actually do.

Phase 5 of ADR 0005. The screen's value is that an operator can trust it, so
these pin that executability comes from the real resolver — the page can never
claim a connector is runnable when an operation against it would be refused.
"""

from __future__ import annotations

from app.services import web_connector_runtime as service


def _row(db_session, key: str) -> dict:
    posture = service.build_runtime_posture(db_session)
    return next(r for r in posture["connectors"] if r["key"] == key)


def test_a_builtin_connector_is_reported_executable(db_session):
    row = _row(db_session, "paystack")
    assert row["tier"] == "builtin_worker"
    assert row["executable"] is True
    assert row["not_executable_reason"] is None
    assert row["is_external"] is False


def test_a_builtin_connectors_declared_egress_is_summarised(db_session):
    row = _row(db_session, "paystack")
    assert "api.paystack.co" in row["egress_summary"]


def test_a_connector_with_no_declared_egress_reads_as_default_deny(db_session):
    posture = service.build_runtime_posture(db_session)
    no_egress = [
        r for r in posture["connectors"] if "default-deny" in r["egress_summary"]
    ]
    assert no_egress, "expected at least one connector with no declared egress"


def test_the_external_tier_is_reported_as_not_live_while_it_fails_closed(db_session):
    """The screen must not imply external connectors are ready to run."""
    posture = service.build_runtime_posture(db_session)
    assert posture["external_tier_live"] is False
    assert posture["stats"]["external_executable"] == 0


def test_stats_count_connectors_and_the_external_subset(db_session):
    posture = service.build_runtime_posture(db_session)
    assert posture["stats"]["total"] == len(posture["connectors"])
    assert posture["stats"]["external"] == sum(
        1 for r in posture["connectors"] if r["is_external"]
    )


def test_every_row_carries_a_manifest_digest(db_session):
    posture = service.build_runtime_posture(db_session)
    assert posture["connectors"]
    for row in posture["connectors"]:
        assert len(row["manifest_digest"]) == 64


def test_install_counts_reflect_the_installations_owner(db_session):
    from app.services.integrations import installations

    before = _row(db_session, "paystack")["installed_count"]
    installations.create_draft(
        db_session,
        connector_key="paystack",
        name="Paystack Posture Test",
        environment="test",
    )
    db_session.flush()
    assert _row(db_session, "paystack")["installed_count"] == before + 1


def test_a_catalogue_only_connector_is_not_executable(db_session):
    posture = service.build_runtime_posture(db_session)
    catalogue = [r for r in posture["connectors"] if r["tier"] == "catalogue_only"]
    for row in catalogue:
        assert row["executable"] is False
        assert "catalogue-only" in (row["not_executable_reason"] or "")
