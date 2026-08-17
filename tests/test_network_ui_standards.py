"""Regression tests for shared network-admin table and action behavior."""

from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

from fastapi.templating import Jinja2Templates
from sqlalchemy import event

from app.models.network import NetworkZone, Vlan, VlanPurpose
from app.models.network_monitoring import DeviceStatus, NetworkDevice, PopSite
from app.models.uisp_control import (
    UispDeviceIntent,
    UispIntentStatus,
    UispIntentTargetType,
)
from app.services import (
    uisp_control_plane,
    web_network_pop_sites,
    web_network_vlans,
    web_network_zones,
)
from app.services.device_operational_status import mismatch_worklist

TEMPLATES = Path("templates")


class _NestedStatsRowParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._div_row_stack: list[bool] = []
        self.nested_row_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "div":
            return
        classes = dict(attrs).get("class", "") or ""
        is_stats_row = {"flex", "justify-between"}.issubset(classes.split())
        if is_stats_row and any(self._div_row_stack):
            self.nested_row_count += 1
        self._div_row_stack.append(is_stats_row)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._div_row_stack:
            self._div_row_stack.pop()


def test_pop_sites_list_is_searchable_paginated_and_clamps_stale_pages(db_session):
    marker = uuid4().hex[:8]
    db_session.add_all(
        [
            PopSite(
                name=f"{marker} POP {index:02d}",
                code=f"{marker}-{index:02d}",
                city="Abuja",
                is_active=index % 2 == 0,
            )
            for index in range(26)
        ]
    )
    db_session.commit()

    payload = web_network_pop_sites.list_page_data(
        db_session,
        "all",
        search=marker,
        page=99,
        per_page=10,
    )

    assert payload["pagination"] == {
        "page": 3,
        "per_page": 10,
        "total": 26,
        "total_pages": 3,
    }
    assert len(payload["pop_sites"]) == 6
    assert all(marker in site.name for site in payload["pop_sites"])


def test_zone_list_has_bounded_query_count_and_pagination(db_session):
    marker = uuid4().hex[:8]
    db_session.add_all(
        [
            NetworkZone(
                name=f"{marker} Zone {index:02d}",
                description=f"Coverage area {marker}",
                is_active=index < 12,
            )
            for index in range(15)
        ]
    )
    db_session.commit()

    statements = 0

    def count_statement(_conn, _cursor, statement, *_args):
        nonlocal statements
        normalized = statement.lstrip().upper()
        if normalized.startswith(
            ("SAVEPOINT ", "RELEASE SAVEPOINT ", "ROLLBACK TO SAVEPOINT ")
        ):
            return
        statements += 1

    event.listen(db_session.bind, "before_cursor_execute", count_statement)
    try:
        payload = web_network_zones.list_page_data(
            db_session,
            "all",
            search=marker,
            page=2,
            per_page=10,
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", count_statement)

    assert payload["pagination"] == {
        "page": 2,
        "per_page": 10,
        "total": 15,
        "total_pages": 2,
    }
    assert len(payload["zones"]) == 5
    assert statements == 3


def test_uisp_intents_support_counted_offset_pagination(db_session):
    intents = [
        UispDeviceIntent(
            target_type=UispIntentTargetType.cpe,
            target_id=uuid4(),
            uisp_device_id=f"ui-standard-{uuid4().hex[:8]}",
            desired_state={},
            status=UispIntentStatus.staged,
        )
        for _ in range(12)
    ]
    db_session.add_all(intents)
    db_session.commit()

    first_page = uisp_control_plane.list_intents(
        db_session,
        status=UispIntentStatus.staged,
        limit=5,
        offset=0,
    )
    second_page = uisp_control_plane.list_intents(
        db_session,
        status=UispIntentStatus.staged,
        limit=5,
        offset=5,
    )

    assert (
        uisp_control_plane.count_intents(db_session, status=UispIntentStatus.staged)
        >= 12
    )
    assert len(first_page) == 5
    assert len(second_page) == 5
    assert {intent.id for intent in first_page}.isdisjoint(
        intent.id for intent in second_page
    )


def test_vlan_inventory_is_searchable_paginated_and_clamps_pages(db_session, region):
    marker = uuid4().hex[:8]
    db_session.add_all(
        [
            Vlan(
                region_id=region.id,
                tag=2000 + index,
                name=f"{marker} VLAN {index:02d}",
                purpose=(
                    VlanPurpose.management if index % 2 == 0 else VlanPurpose.internet
                ),
                is_active=True,
            )
            for index in range(26)
        ]
    )
    db_session.commit()

    payload = web_network_vlans.build_vlans_list_data(
        db_session,
        search=marker,
        page=99,
        per_page=10,
    )

    assert payload["pagination"] == {
        "page": 3,
        "per_page": 10,
        "total": 26,
        "total_pages": 3,
    }
    assert len(payload["vlans"]) == 6
    assert all(marker in vlan.name for vlan in payload["vlans"])


def test_device_status_worklist_searches_and_paginates_derived_rows(db_session):
    marker = uuid4().hex[:8]
    db_session.add_all(
        [
            NetworkDevice(
                name=f"{marker} Device {index:02d}",
                status=DeviceStatus.online,
                live_status="down",
            )
            for index in range(27)
        ]
    )
    db_session.commit()

    payload = mismatch_worklist(
        db_session,
        search=marker,
        page=99,
        per_page=10,
    )

    assert payload["pagination"] == {
        "page": 3,
        "per_page": 10,
        "total": 27,
        "total_pages": 3,
    }
    assert sum(len(group["rows"]) for group in payload["groups"]) == 7


def test_network_templates_do_not_use_inline_browser_confirmations():
    inline_confirm_patterns = (
        'onsubmit="return confirm',
        'onclick="return confirm',
        'onclick="if(confirm',
        "return confirm(",
    )

    offenders = []
    for template in (TEMPLATES / "admin/network").rglob("*.html"):
        source = template.read_text()
        if any(pattern in source for pattern in inline_confirm_patterns):
            offenders.append(str(template))

    assert offenders == []


def test_network_map_stats_rows_are_siblings() -> None:
    source = (TEMPLATES / "admin/network/map.html").read_text(encoding="utf-8")
    stats_card = source.split("<!-- Stats -->", maxsplit=1)[1].split(
        "<!-- Legend -->", maxsplit=1
    )[0]
    parser = _NestedStatsRowParser()

    parser.feed(stats_card)

    assert parser.nested_row_count == 0


def test_network_map_layout_is_map_first_and_responsive() -> None:
    source = (TEMPLATES / "admin/network/map.html").read_text(encoding="utf-8")

    assert "width: 320px" not in source
    assert "network-map-layout" in source
    assert '"map"\n            "health"\n            "layers"' in source
    assert source.index('class="network-map-canvas"') < source.index(
        'class="network-map-health'
    )
    assert "Back to Network" not in source
    assert '<details class="network-map-legend' in source


def test_shared_confirmation_assets_and_critical_actions_are_wired():
    base = (TEMPLATES / "base.html").read_text()
    confirmation_js = Path("static/js/action-confirmations.js").read_text()
    ont_config = (
        TEMPLATES / "admin/network/onts/_apply_device_config_panel.html"
    ).read_text()
    uisp_detail = (TEMPLATES / "admin/network/uisp-control/detail.html").read_text()
    confirm_modal = (TEMPLATES / "components/modals/confirm_modal.html").read_text()
    table_macros = (TEMPLATES / "components/ui/macros.html").read_text()

    assert "/static/js/action-confirmations.js" in base
    assert "htmx:confirm" in confirmation_js
    assert "event.submitter" in confirmation_js
    assert ont_config.count("hx-confirm=") >= 5
    assert 'data-confirm="Apply this desired-state revision' in uisp_detail
    assert "bg-primary-600" in confirm_modal
    assert "focus:ring-primary-500" in table_macros
    pagination_macro = table_macros.split("PAGINATION", 1)[1].split("TYPE BADGE", 1)[0]
    assert "{{ color }}" not in pagination_macro
    assert "bg-primary-600" in pagination_macro


def test_changed_network_templates_compile():
    env = Jinja2Templates(directory="templates").env
    templates = (
        "admin/network/map.html",
        "admin/network/pop-sites/index.html",
        "admin/network/zones/index.html",
        "admin/network/tr069/index.html",
        "admin/network/vpn/peer_form.html",
        "admin/network/vpn/server_form.html",
        "admin/network/onts/_apply_device_config_panel.html",
        "admin/network/onts/_onu_mode_modal.html",
        "admin/network/monitoring/alarms.html",
        "admin/network/uisp-control/index.html",
        "admin/network/vlans/index.html",
        "admin/network/device_status_worklist.html",
        "components/modals/confirm_modal.html",
        "components/ui/macros.html",
    )

    for template in templates:
        env.get_template(template)
