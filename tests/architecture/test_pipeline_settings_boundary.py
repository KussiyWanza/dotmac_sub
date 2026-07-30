"""Architecture guards for the native Selfcare Pipeline Settings boundary."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_pipeline_settings_stays_native_and_uses_the_canonical_route() -> None:
    routes = _read("app/web/admin/sales.py")
    service = _read("app/services/web_sales.py")
    active_ui = "\n".join(
        _read(path)
        for path in (
            "templates/admin/sales/pipelines/index.html",
            "templates/admin/sales/pipelines/form.html",
            "templates/admin/sales/leads/index.html",
            "templates/admin/sales/leads/board.html",
        )
    )

    assert '"/pipelines-settings"' in routes
    assert "/admin/sales/pipelines-settings" in active_ui
    assert "/admin/sales/pipelines/" not in active_ui
    assert "CRMClient" not in routes
    assert "CRMClient" not in service


def test_stage_presentation_is_a_governed_versioned_contract() -> None:
    contract = _read("app/services/sales/pipeline_configuration.py")
    registry = _read("app/services/sot_relationships.py")
    relationship_map = _read("docs/SOT_RELATIONSHIP_MAP.md")

    assert 'METADATA_KEY = "pipeline_stage_presentation_v1"' in contract
    assert "class PipelineStageType(StrEnum)" in contract
    assert "STAGE_ICON_OPTIONS" in contract
    assert "governed pipeline stage presentation and ordering" in registry
    assert "atomic stage ordering" in relationship_map


def test_pipeline_board_reads_stage_presentation_from_the_native_owner() -> None:
    sales_owner = _read("app/services/sales/service.py")
    board_script = _read("static/js/kanban.js")

    assert "pipeline_configuration.stage_presentation(" in sales_owner
    assert '"stage_type": presentation.stage_type.value' in sales_owner
    assert '"color": presentation.color' in sales_owner
    assert "column.color" in board_script
    assert "column.icon" in board_script
