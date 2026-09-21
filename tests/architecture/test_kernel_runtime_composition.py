"""The product-owned CI path stays bound to its declared v3 contract."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs" / "kernel-runtime-composition.json"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ACTION = (
    "michaelayoade/dotmac_starter_mt/.github/actions/verify-composition-observations"
)
PRODUCT = "sub"


def _load_record() -> dict[str, object]:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def _load_workflow() -> dict[str, object]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _require_exact_action_binding(
    workflow: dict[str, object], record: dict[str, object]
) -> None:
    contract = record.get("contract_revision")
    assert isinstance(contract, dict)
    revision = contract.get("commit")
    assert isinstance(revision, str)
    expected = f"{ACTION}@{revision}"
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    steps = [
        step
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", ())
        if isinstance(step, dict) and str(step.get("uses", "")).startswith(ACTION)
    ]
    assert len(steps) == 1, "CI must invoke exactly one composition verifier"
    assert steps[0].get("uses") == expected, "the verifier must use the record pin"
    assert steps[0].get("with") == {"product": PRODUCT}


def test_ci_verifies_the_record_with_its_exact_contract_revision() -> None:
    _require_exact_action_binding(_load_workflow(), _load_record())


def test_a_mutable_action_ref_is_refused_by_the_local_wiring_guard() -> None:
    workflow = deepcopy(_load_workflow())
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    step = next(
        step
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", ())
        if isinstance(step, dict) and str(step.get("uses", "")).startswith(ACTION)
    )
    step["uses"] = f"{ACTION}@main"
    with pytest.raises(AssertionError, match="record pin"):
        _require_exact_action_binding(workflow, _load_record())
