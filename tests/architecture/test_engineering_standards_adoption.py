"""Sub's product-owned review surface for Governance schema 9.

The connector detector remains Governance-owned. These tests guard the exact
pin, declared baselines, and conservation evidence without copying it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / ".dotmac" / "standards-profile.json"
WORKFLOW = ROOT / ".github" / "workflows" / "engineering-standards.yml"
EVIDENCE = ROOT / "docs" / "external-connector-surface.md"
ACCEPTED_GOVERNANCE_SHA = "a19259b10568d29dc0a9617347498fea7f1e7a97"
CATEGORIES = {
    "outbound_transport",
    "webhook_surface",
    "provider_credential",
    "connector_task",
    "sync_checkpoint",
    "delivery_retry",
}
_BASELINE_ROW = re.compile(r"^\|\s*`(\w+)`\s*\|\s*(\d+)\s*\|$", re.MULTILINE)
_LEDGER_ROW = re.compile(
    r"^\| `([^`]+)` \| `([^`]+)` \| `([^`]+)` \| `([0-9a-f]{64})` \|$",
    re.MULTILINE,
)
_ACTION_REF = re.compile(
    r"^\s*uses:\s*michaelayoade/dotmac_governance/"
    r"\.github/actions/standards-check@([^\s#]+)\s*$",
    re.MULTILINE,
)


def _profile() -> dict:
    return json.loads(PROFILE.read_text(encoding="utf-8"))


def _action_refs(text: str) -> list[str]:
    return _ACTION_REF.findall(text)


def test_the_profile_declares_the_accepted_schema_nine_surface() -> None:
    profile = _profile()
    assert profile["schema_version"] == 9
    assert profile["enforcement_mode"] == "required"
    surface = profile["external_connector_surface"]
    assert set(surface) == {"baselines", "conserved_exclusions"}
    assert set(surface["baselines"]) == CATEGORIES
    assert all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in surface["baselines"].values()
    )


def test_the_review_record_equals_the_declared_baselines() -> None:
    declared = _profile()["external_connector_surface"]["baselines"]
    rows = _BASELINE_ROW.findall(EVIDENCE.read_text(encoding="utf-8"))
    recorded = {category: int(count) for category, count in rows}
    assert set(recorded) == CATEGORIES, rows
    assert recorded == declared


def test_the_review_record_equals_the_conservation_ledger() -> None:
    declared = _profile()["external_connector_surface"]["conserved_exclusions"]
    rows = _LEDGER_ROW.findall(EVIDENCE.read_text(encoding="utf-8"))
    recorded = [
        {
            "path": path,
            "symbol": symbol,
            "category": category,
            "fingerprint": fingerprint,
        }
        for path, symbol, category, fingerprint in rows
    ]
    key = lambda item: (
        item["path"],
        item["symbol"],
        item["category"],
        item["fingerprint"],
    )
    assert sorted(recorded, key=key) == sorted(declared, key=key)


def test_the_workflow_executes_the_profile_pin_exactly_once() -> None:
    profile_pin = _profile()["governance_model"]["revision"]
    refs = _action_refs(WORKFLOW.read_text(encoding="utf-8"))
    assert profile_pin == ACCEPTED_GOVERNANCE_SHA
    assert refs == [profile_pin]


def test_the_action_ref_parser_refuses_a_moving_or_missing_reference() -> None:
    armed = (
        "uses: michaelayoade/dotmac_governance/"
        f".github/actions/standards-check@{ACCEPTED_GOVERNANCE_SHA}\n"
    )
    assert _action_refs(armed) == [ACCEPTED_GOVERNANCE_SHA]
    assert _action_refs(armed.replace(ACCEPTED_GOVERNANCE_SHA, "main")) == ["main"]
    assert _action_refs("# " + armed) == []
    assert _action_refs("") == []


def test_the_profile_names_the_accepted_governance_source() -> None:
    assert _profile()["governance_model"] == {
        "kind": "pinned",
        "canonical_url": "https://github.com/michaelayoade/dotmac_governance",
        "revision": ACCEPTED_GOVERNANCE_SHA,
        "source": "docs/adr/0006-cross-repository-engineering-conformance.md",
        "status": "accepted",
    }
