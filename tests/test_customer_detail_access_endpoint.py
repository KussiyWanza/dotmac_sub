"""Customer detail renders the serving endpoint, not the provisioning NAS site (G1)."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import web_customer_details as details
from app.services.topology.customer_path import CustomerPath


class _Asset(SimpleNamespace):
    pass


def _subscription_stub():
    return SimpleNamespace(
        id=uuid.uuid4(),
        subscriber_id=uuid.uuid4(),
        login="10005452",
        ipv4_address="10.10.11.6",
        status=SimpleNamespace(value="active"),
        offer=SimpleNamespace(name="Home 20M"),
        provisioning_nas_device=SimpleNamespace(
            id=uuid.uuid4(),
            name="Abuja BNG",
            pop_site=SimpleNamespace(name="Jabi POP"),
        ),
        mac_address=None,
        ipv6_address=None,
    )


def _fiber_path():
    return CustomerPath(
        ont=_Asset(id=uuid.uuid4(), serial_number="UBNT58508c30"),
        pon_port=_Asset(id=uuid.uuid4(), name="0/1/3", port_number=3),
        access_device=_Asset(id=uuid.uuid4(), name="Gudu OLT"),
        access_device_kind="olt",
        upstream_chain=[_Asset(id=uuid.uuid4(), name="Abuja BNG")],
    )


def test_endpoint_projection_reports_serving_endpoint(monkeypatch):
    monkeypatch.setattr(
        details, "resolve_customer_path", lambda _db, _sub: _fiber_path()
    )

    projection = details._build_access_endpoint_projection(None, _subscription_stub())
    endpoint = projection.endpoint

    assert endpoint.endpoint_display == "Gudu OLT (0/1/3)"
    assert endpoint.pon_port_label == "0/1/3"
    # Fibre resolves from the ONT assignment, not the NAS-arm live/provisioned flag.
    assert endpoint.endpoint_source == "ont_assignment"
    assert endpoint.source_presentation.label == "ONT assignment"
    assert endpoint.endpoint_complete is True
    assert [node.kind for node in projection.view.nodes] == [
        "ont",
        "pon_port",
        "olt",
        "network_device",
    ]
    # The legacy trace projection stays available for evidence consumers.
    assert [node["kind"] for node in projection.trace_dict["nodes"]] == [
        "ont",
        "pon_port",
        "olt",
        "network_device",
    ]


def test_endpoint_projection_degrades_without_breaking_the_page(monkeypatch):
    """An unavailable trace must not take the customer record down with it."""

    def _boom(_db, _sub):
        raise RuntimeError("topology backend unavailable")

    monkeypatch.setattr(details, "resolve_customer_path", _boom)

    projection = details._build_access_endpoint_projection(None, _subscription_stub())

    assert projection.endpoint.endpoint_source == "unresolved"
    assert projection.endpoint.source_presentation.label == "Unresolved"
    assert projection.view is None
    assert projection.trace is None


def test_card_carries_endpoint_separately_from_provisioned_site():
    """The provisioning NAS site is kept, but is no longer the serving location."""

    sub = _subscription_stub()
    sub_id = str(sub.id)

    cards = details._build_network_access_cards(
        [sub],
        {},
        {},
        {
            sub_id: {
                "endpoint_display": "Gudu OLT (0/1/3)",
                "endpoint_source": "live_session",
            }
        },
        {sub_id: {"nodes": [], "breaks": []}},
    )

    assert cards[0]["access_endpoint"]["endpoint_display"] == "Gudu OLT (0/1/3)"
    assert cards[0]["access_endpoint"]["endpoint_source"] == "live_session"
    # Still present, but as intent rather than as the serving location.
    assert cards[0]["pop_site_name"] == "Jabi POP"


def test_cards_tolerate_missing_endpoint_projection():
    sub = _subscription_stub()

    cards = details._build_network_access_cards([sub], {})

    assert cards[0]["access_endpoint"] == {}
    assert cards[0]["topology_trace"] is None


@pytest.mark.parametrize("needle", ["pon_port_label", "access_device_name"])
def test_template_does_not_compose_the_endpoint_string_itself(needle):
    """endpoint_display is composed in the service so surfaces cannot drift."""

    template = Path("templates/admin/customers/detail.html").read_text()

    assert needle not in template


def test_customer_detail_template_still_compiles():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    env.parse(Path("templates/admin/customers/detail.html").read_text())
