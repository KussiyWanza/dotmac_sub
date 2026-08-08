from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MIGRATED_BADGE_TEMPLATES = (
    "templates/admin/network/core-devices/index.html",
    "templates/admin/network/core-devices/detail.html",
    "templates/admin/network/network-devices/index.html",
    "templates/admin/network/devices/_table_rows.html",
    "templates/admin/network/device_status_worklist.html",
    "templates/admin/network/olts/index.html",
    "templates/admin/network/monitoring/index.html",
)


def test_device_operational_surfaces_consume_shared_semantic_badge() -> None:
    for relative_path in MIGRATED_BADGE_TEMPLATES:
        source = (ROOT / relative_path).read_text()

        assert "status_presentation_badge" in source, relative_path
        assert "display_status_map" not in source, relative_path
        assert "operational_label" not in source, relative_path
        assert "device_status_variants" not in source, relative_path
        assert "{% set opmap" not in source, relative_path
        assert "{% set op_color" not in source, relative_path


def test_network_map_uses_server_semantic_tone_not_admin_status() -> None:
    service = (ROOT / "app/services/network_map.py").read_text()
    template = (ROOT / "templates/admin/network/map.html").read_text()

    assert "status_presentation=NetworkMapStatusPresentation.from_contract(" in service
    assert "device.status_presentation" in service
    assert "device.status.value" not in service
    assert "p.status_presentation?.tone" in template
    assert "semanticToneColor(deviceTone)" in template
    assert (
        'device_status_chart["tones"]'
        in (ROOT / "templates/admin/network/monitoring/index.html").read_text()
    )
    assert (
        "p.status === 'online'"
        not in template.split("case 'network_device':", maxsplit=1)[1].split(
            "case 'customer':", maxsplit=1
        )[0]
    )


def test_network_device_api_declares_operational_projection() -> None:
    schema = (ROOT / "app/schemas/network_monitoring.py").read_text()
    api = (ROOT / "app/api/domains_monitoring.py").read_text()

    assert (
        'operational_status: Literal["working", "not_working"] | None = None' in schema
    )
    assert "operational_reason: str | None = None" in schema
    assert "operational_retry_pending" not in schema
    assert "status_presentation: StatusPresentation | None = None" in schema
    assert "_with_device_operational_status" in api
