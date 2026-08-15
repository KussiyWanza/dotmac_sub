"""Readiness gate for the shadow-drift inspection script.

The script is a read-only cutover gate: exit 0 only when every one of the five
legacy-scalar shadow counters is zero, exit 2 while any team or membership
still lacks its composed fact.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import uuid4

import pytest

from app.models.service_team import ServiceTeam
from scripts.migration import inspect_service_team_shadow_drift as script


@pytest.fixture(autouse=True)
def _script_reads_the_test_session(monkeypatch, db_session):
    @contextmanager
    def read_only_snapshot_session():
        yield db_session

    monkeypatch.setattr(
        script, "read_only_snapshot_session", read_only_snapshot_session
    )


def _run(capsys) -> tuple[int, dict]:
    exit_code = script.main([])
    return exit_code, json.loads(capsys.readouterr().out)


def test_gate_passes_with_zero_drift(db_session, capsys):
    db_session.add(ServiceTeam(name=f"Native {uuid4()}", is_active=True))
    db_session.commit()

    exit_code, payload = _run(capsys)

    assert exit_code == 0
    assert payload["blocker_count"] == 0
    assert payload["legacy_type_without_capability"] == 0
    assert payload["legacy_role_without_responsibility"] == 0
    assert payload["legacy_manager_without_responsibility"] == 0
    assert payload["legacy_region_without_geo_scope"] == 0
    assert payload["legacy_workforce_without_external_reference"] == 0


def test_gate_blocks_while_a_legacy_scalar_lacks_its_composed_fact(db_session, capsys):
    db_session.add(
        ServiceTeam(
            name=f"Legacy shadow {uuid4()}",
            team_type="support",
            region="Abuja",
            is_active=True,
        )
    )
    db_session.commit()

    exit_code, payload = _run(capsys)

    assert exit_code == 2
    assert payload["legacy_type_without_capability"] == 1
    assert payload["legacy_region_without_geo_scope"] == 1
    assert payload["blocker_count"] >= 2
