"""Contract tests for the Network Devices surface (UI projection contracts).

The owner (``web_network_core_devices_inventory``) must project device-count
summaries as ``Kpi`` and per-row management actions as ``Action``. Projection
repair age does not create a third public device state.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.services.ui_contracts import Action, Kpi, StateValue
from app.services.web_network_core_devices_inventory import (
    NETWORK_DEVICE_LIST_DEFINITION,
    _device_cohort_url,
    _device_row_actions,
    _device_stat_kpis,
)

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "admin" / "network"

_SAMPLE_STATS = {
    "total": 42,
    "core": 5,
    "olt": 3,
    "ont": 30,
    "cpe": 4,
    "working": 22,
    "not_working": 20,
    "archived": 0,
}


def _query(**filters):
    return NETWORK_DEVICE_LIST_DEFINITION.build_query(
        search=filters.pop("search", None),
        filters={
            "type": None,
            "status": None,
            "vendor": None,
            "lifecycle": None,
            **filters,
        },
    )


def test_stat_kpis_are_kpi_contracts_with_present_state() -> None:
    kpis = _device_stat_kpis(_SAMPLE_STATS, _query())
    assert set(kpis) >= {
        "total",
        "core",
        "olt",
        "ont",
        "cpe",
        "working",
        "not_working",
    }
    for kpi in kpis.values():
        assert isinstance(kpi, Kpi)
        assert isinstance(kpi.value, StateValue)
        assert kpi.value.is_present
        assert kpi.cohort_url.startswith("/")
    # Counts project through StateValue, not raw ints.
    assert kpis["working"].value.value == 22


def test_status_kpi_cohort_encodes_status_and_preserves_active_filters() -> None:
    # An active TYPE filter is present on the page, but a status tile is an
    # overview across every type: its cohort_url must narrow ONLY on its status
    # (plus vendor/search) and must NOT inherit the page's type filter, or the
    # overview count and the rows it links to would diverge (KPI-parity rule).
    kpis = _device_stat_kpis(
        _SAMPLE_STATS,
        _query(type="core", vendor="huawei", search="edge"),
    )
    parsed = parse_qs(urlsplit(kpis["not_working"].cohort_url).query)
    assert parsed["status"] == ["not_working"]
    assert parsed["vendor"] == ["huawei"]
    assert parsed["search"] == ["edge"]
    # The page's active type filter is deliberately dropped from the tile cohort.
    assert "type" not in parsed


def test_type_kpi_cohort_narrows_on_type_and_ignores_active_status() -> None:
    # An active STATUS filter is present, but type/total tiles are an overview
    # across every status: their cohort_url must narrow ONLY on type and must
    # NOT inherit the page's status filter.
    kpis = _device_stat_kpis(_SAMPLE_STATS, _query(status="not_working"))
    olt = parse_qs(urlsplit(kpis["olt"].cohort_url).query)
    assert olt["type"] == ["olt"]
    assert "status" not in olt
    # "All devices" drills across every type AND status -> unfiltered list.
    total = parse_qs(urlsplit(kpis["total"].cohort_url).query)
    assert total["type"] == ["all"]
    assert "status" not in total


def test_kpi_tiles_show_overview_counts_independent_of_page_filter(monkeypatch) -> None:
    # Parity at the page level: even with an active status=not_working / type=core page
    # filter (which shrinks the table below), the KPI tiles must display the true
    # overview counts, computed by a stats query that drops the page status/type
    # filter. The tile value must equal the count at the cohort the tile links to.
    from app.services import web_network_core_devices_inventory as mod

    stats_calls: list[dict] = []

    def fake_stats(
        db,
        *,
        device_type=None,
        status=None,
        vendor=None,
        search=None,
        lifecycle=None,
    ):
        stats_calls.append(
            {
                "device_type": device_type,
                "status": status,
                "vendor": vendor,
                "search": search,
                "lifecycle": lifecycle,
            }
        )
        # The page-filtered call returns a shrunken subset; the overview call
        # (no type/status) returns the real fleet-wide counts.
        if device_type is None and status is None:
            return dict(_SAMPLE_STATS)
        return dict.fromkeys(_SAMPLE_STATS, 0)

    monkeypatch.setattr(
        mod.device_projection_views, "device_projection_stats", fake_stats
    )
    monkeypatch.setattr(
        mod.device_projection_views,
        "query_device_projections",
        lambda *a, **k: ([], 0),
    )
    list_query = NETWORK_DEVICE_LIST_DEFINITION.build_query(
        search=None,
        filters={
            "type": "core",
            "status": "not_working",
            "vendor": None,
            "lifecycle": None,
        },
    )
    data = mod.devices_list_page_data(object(), list_query)
    kpis = data["device_kpis"]

    # Tiles show the overview counts, not the page-filtered (0-valued) subset.
    assert kpis["working"].value.value == _SAMPLE_STATS["working"]
    assert kpis["olt"].value.value == _SAMPLE_STATS["olt"]
    assert kpis["total"].value.value == _SAMPLE_STATS["total"]
    # An overview stats query (no page status/type) was issued to feed the tiles.
    assert any(
        call["device_type"] is None and call["status"] is None for call in stats_calls
    )


def test_cohort_url_helper_drops_empty_params() -> None:
    assert _device_cohort_url(_query()) == "/admin/network/devices"
    assert _device_cohort_url(_query(), status="working") == (
        "/admin/network/devices?status=working"
    )


def test_row_actions_allowed_ping_carries_no_reason() -> None:
    actions = _device_row_actions(
        {"id": "d1", "type": "core", "ip_address": "10.0.0.1"}
    )
    assert isinstance(actions["ping"], Action)
    assert actions["ping"].allowed
    assert actions["ping"].reason is None
    assert actions["reboot"].allowed  # core + ip is rebootable
    assert actions["reboot"].requires_confirmation is True
    assert actions["reboot"].preview_url.endswith("/reboot/preview")
    assert actions["archive"].allowed
    assert actions["archive"].label == "Decommission Device"
    assert actions["archive"].preview_url.endswith("/archive/preview")


def test_archived_core_device_action_restores_without_preview() -> None:
    actions = _device_row_actions(
        {
            "id": "d1",
            "type": "core",
            "ip_address": "10.0.0.1",
            "lifecycle_state": "archived",
        }
    )
    assert actions["archive"].allowed
    assert actions["archive"].label == "Restore Device"
    assert actions["archive"].preview_url is None
    assert not actions["ping"].allowed
    assert not actions["reboot"].allowed


def test_row_actions_block_ping_without_ip_and_reboot_for_cpe() -> None:
    no_ip = _device_row_actions({"id": "d2", "type": "core", "ip_address": None})
    assert not no_ip["ping"].allowed
    assert no_ip["ping"].reason  # blocked action must carry a reason
    assert not no_ip["reboot"].allowed

    cpe = _device_row_actions({"id": "d3", "type": "cpe", "ip_address": "10.0.0.9"})
    assert cpe["ping"].allowed  # has an IP
    assert not cpe["reboot"].allowed
    assert "not available" in cpe["reboot"].reason


def test_index_template_renders_kpi_contract_fields() -> None:
    text = (_TEMPLATES / "devices" / "index.html").read_text()
    assert "device_kpis.working.cohort_url" in text
    assert "device_kpis.working.value.is_present" in text
    assert "device_kpis.working.tone" in text
    assert "device_kpis.total" in text
    # No longer reads the raw stats dict for the tiles.
    assert "stats.working" not in text


def test_table_rows_template_gates_actions_on_eligibility() -> None:
    text = (_TEMPLATES / "devices" / "_table_rows.html").read_text()
    assert "device.actions.ping" in text
    assert "device.actions.reboot" in text
    assert "device.actions.archive" in text
    assert "action_permitted(request, ping)" in text
    assert "action_permitted(request, reboot)" in text
    assert "action_permitted(request, remove)" in text
    assert "reboot.preview_url" in text
    assert "hx-confirm" not in text
