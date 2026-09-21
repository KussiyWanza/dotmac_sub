"""Sanitized configuration planning never claims to have written policies."""

import pytest
from pydantic import ValidationError

from scripts.import_inbox_sla_config import (
    ImportMappings,
    ImportSource,
    SourcePolicy,
    SourceRule,
    plan_import,
)


def test_planner_counts_policies_with_multiple_rules_without_negative_skips() -> None:
    source = ImportSource(
        policies=(
            SourcePolicy(
                name="Support",
                rules=(SourceRule(source_team_id="a"), SourceRule(source_team_id="b")),
            ),
        )
    )
    result = plan_import(source, ImportMappings(team={"a": "team-a", "b": "team-b"}))
    assert result.skipped == 1
    assert result.created == result.updated == 0
    assert result.unresolved == ()


def test_planner_reports_unresolved_mapping_and_refuses_unknown_payload_fields() -> (
    None
):
    source = ImportSource(
        policies=(SourcePolicy(name="Support", rules=(SourceRule(source_team_id=42),)),)
    )
    assert plan_import(source, ImportMappings()).unresolved == ("team:42",)
    with pytest.raises(ValidationError):
        ImportSource.model_validate({"policies": [], "messages": [{"body": "private"}]})
