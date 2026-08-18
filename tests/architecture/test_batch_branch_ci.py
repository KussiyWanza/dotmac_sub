from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = (
    ROOT / ".github/workflows/ci.yml",
    ROOT / ".github/workflows/mobile.yml",
    ROOT / ".github/workflows/engineering-standards.yml",
)
BATCH_BRANCHES = ("integration/**", "consolidate/**")


def _missing_batch_controls(workflows: dict[str, str]) -> list[str]:
    missing: list[str] = []
    for name, workflow in workflows.items():
        for event in ("push", "pull_request"):
            event_block = workflow.split(f"  {event}:\n", maxsplit=1)[1].split(
                "\n  ", maxsplit=1
            )[0]
            for branch in BATCH_BRANCHES:
                if f"'{branch}'" not in event_block:
                    missing.append(f"{name}:{event}:{branch}")

    ci_workflow = workflows["ci.yml"]
    enforced_case = 'integration/*|consolidate/*) MODE="" ;;'
    if enforced_case not in ci_workflow:
        missing.append("ci.yml:migration-sequence:batch-branches")
    return missing


def test_batch_branches_receive_full_ci_and_governance_controls() -> None:
    workflows = {path.name: path.read_text(encoding="utf-8") for path in WORKFLOWS}

    assert _missing_batch_controls(workflows) == []


def test_batch_branch_control_detector_is_sensitive_to_each_boundary() -> None:
    workflows = {path.name: path.read_text(encoding="utf-8") for path in WORKFLOWS}

    for name in workflows:
        for branch in BATCH_BRANCHES:
            planted = dict(workflows)
            planted[name] = planted[name].replace(f", '{branch}'", "")
            assert f"{name}:push:{branch}" in _missing_batch_controls(planted)
            assert f"{name}:pull_request:{branch}" in _missing_batch_controls(planted)

    planted = dict(workflows)
    planted["ci.yml"] = planted["ci.yml"].replace(
        'integration/*|consolidate/*) MODE="" ;;',
        'integration/*) MODE="" ;;',
    )
    assert "ci.yml:migration-sequence:batch-branches" in _missing_batch_controls(
        planted
    )
