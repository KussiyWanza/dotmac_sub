from __future__ import annotations

from types import SimpleNamespace


def _make_subscriber(db_session, email: str):
    from app.models.subscriber import Subscriber, SubscriberStatus

    subscriber = Subscriber(
        first_name="Static",
        last_name="WAN",
        email=email,
        status=SubscriberStatus.active,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def test_configure_push_scope_sections_are_individual() -> None:
    from app.web.admin.network_onts import _configure_push_scope_sections

    assert _configure_push_scope_sections("wifi") == (False, False, False, True)
    assert _configure_push_scope_sections("wan") == (True, False, False, False)
    assert _configure_push_scope_sections("lan") == (False, True, False, False)
    assert _configure_push_scope_sections("management") == (False, False, True, False)
    assert _configure_push_scope_sections("all") == (True, True, True, True)


def test_update_ont_config_reports_pending_when_acs_delivery_is_unavailable(
    db_session, monkeypatch
) -> None:
    from app.models.network import OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions.db_config import update_ont_config

    ont = OntUnit(serial_number="CONFIG-PENDING-001", is_active=True)
    db_session.add(ont)
    db_session.commit()

    def fake_set_lan_config(*args, **kwargs):
        return ActionResult(
            success=False,
            message=(
                "ONT CONFIG-PENDING-001 has no GenieACS identity. Sync-only "
                "provisioning requires a resolvable ACS device before push."
            ),
            data={"missing_acs_identity": True},
        )

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.db_config.set_lan_config",
        fake_set_lan_config,
    )

    result = update_ont_config(
        db_session,
        str(ont.id),
        lan_gateway_ip="192.168.1.1",
        lan_dhcp_enabled=True,
        push_to_device=True,
        push_wan=False,
        push_lan=True,
        push_mgmt=False,
        push_wifi=False,
    )

    assert result.success is True
    assert result.waiting is True
    # "saved" and "delivered" are reported as two separate facts: the operator
    # must not read a deferred push as a completed change.
    assert "Saved to database." in result.message
    assert "Not yet delivered to the device" in result.message
    assert "waiting for its next inform" in result.message
    assert "Use Advanced Actions" not in result.message
    db_session.refresh(ont)
    assert ont.desired_config["delivery"]["pending_apply"] is True


def test_update_ont_config_still_fails_invalid_delivery_input(
    db_session, monkeypatch
) -> None:
    from app.models.network import OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions.db_config import update_ont_config

    ont = OntUnit(serial_number="CONFIG-INVALID-001", is_active=True)
    db_session.add(ont)
    db_session.commit()

    def fake_set_lan_config(*args, **kwargs):
        return ActionResult(
            success=False,
            message="LAN IP address must be a valid IPv4 address.",
        )

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.db_config.set_lan_config",
        fake_set_lan_config,
    )

    result = update_ont_config(
        db_session,
        str(ont.id),
        lan_gateway_ip="not-an-ip",
        push_to_device=True,
        push_wan=False,
        push_lan=True,
        push_mgmt=False,
        push_wifi=False,
    )

    assert result.success is False
    assert result.waiting is False
    assert "must be a valid IPv4 address" in result.message


def test_update_ont_config_pushes_wifi_enabled_only(db_session, monkeypatch) -> None:
    from app.models.network import OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions.db_config import update_ont_config

    ont = OntUnit(serial_number="WIFI-ENABLE-ONLY", is_active=True)
    db_session.add(ont)
    db_session.commit()

    calls = []

    def fake_set_wifi_config(*args, **kwargs):
        calls.append(kwargs)
        return ActionResult(success=True, message="wifi ok")

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.db_config.set_wifi_config",
        fake_set_wifi_config,
    )

    result = update_ont_config(
        db_session,
        str(ont.id),
        wifi_enabled=False,
        push_to_device=True,
        push_wan=False,
        push_lan=False,
        push_mgmt=False,
        push_wifi=True,
    )

    assert result.success is True
    assert calls[0]["enabled"] is False


def test_update_ont_config_pushes_lan_dhcp_range_only(db_session, monkeypatch) -> None:
    from app.models.network import OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions.db_config import update_ont_config

    ont = OntUnit(serial_number="LAN-RANGE-ONLY", is_active=True)
    db_session.add(ont)
    db_session.commit()

    calls = []

    def fake_set_lan_config(*args, **kwargs):
        calls.append(kwargs)
        return ActionResult(success=True, message="lan ok")

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.db_config.set_lan_config",
        fake_set_lan_config,
    )

    result = update_ont_config(
        db_session,
        str(ont.id),
        lan_dhcp_start="192.168.1.100",
        lan_dhcp_end="192.168.1.200",
        push_to_device=True,
        push_wan=False,
        push_lan=True,
        push_mgmt=False,
        push_wifi=False,
    )

    assert result.success is True
    assert calls[0]["dhcp_start"] == "192.168.1.100"
    assert calls[0]["dhcp_end"] == "192.168.1.200"


def test_update_ont_config_pushes_static_wan_fields(db_session, monkeypatch) -> None:
    from app.models.network import OntUnit
    from app.services.network.ont_desired_config import desired_config
    from app.services.web_network_ont_actions.db_config import update_ont_config

    ont = OntUnit(serial_number="STATIC-WAN-FIELDS", is_active=True)
    db_session.add(ont)
    db_session.commit()

    calls = []

    def fake_reconcile(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            sync_status="synced",
            actions_applied=(),
            failure=None,
        )

    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        fake_reconcile,
    )

    result = update_ont_config(
        db_session,
        str(ont.id),
        wan_mode="static_ip",
        ip_protocol="",
        wan_static_ip="100.64.1.2",
        wan_static_subnet="255.255.255.252",
        wan_static_gateway="100.64.1.1",
        wan_static_dns="1.1.1.1",
        push_to_device=True,
        push_wan=True,
        push_lan=False,
        push_mgmt=False,
        push_wifi=False,
    )

    assert result.success is True
    assert calls[0]["mode"] == "sync"
    assert calls[0]["proposed_change"].as_mapping() == {
        "wan_mode": "static",
        "wan_static_ip": "100.64.1.2",
        "wan_static_subnet": "255.255.255.252",
        "wan_static_gateway": "100.64.1.1",
        "wan_static_dns": "1.1.1.1",
        "wan_static_ip_is_public": False,
    }
    db_session.refresh(ont)
    wan = desired_config(ont)["wan"]
    assert wan["mode"] == "static_ip"
    assert wan["static_ip"] == "100.64.1.2"
    assert wan["static_subnet"] == "255.255.255.252"
    assert wan["static_gateway"] == "100.64.1.1"
    assert wan["static_dns"] == "1.1.1.1"


def test_update_ont_config_claims_static_wan_ipam_address(
    db_session, catalog_offer
) -> None:
    from app.models.catalog import Subscription, SubscriptionStatus
    from app.models.network import (
        IPAssignment,
        IpBlock,
        IpPool,
        IPVersion,
        OntAssignment,
        OntUnit,
    )
    from app.services.network.ont_desired_config import desired_config
    from app.services.web_network_ont_actions.db_config import update_ont_config

    subscriber = _make_subscriber(db_session, "static-wan-owner@example.com")
    ont = OntUnit(serial_number="STATIC-WAN-IPAM-CLAIM", is_active=True)
    pool = IpPool(
        name="Subscriber WAN Static Pool",
        ip_version=IPVersion.ipv4,
        cidr="100.64.10.0/29",
        is_active=True,
    )
    db_session.add_all([ont, pool])
    db_session.flush()
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add(subscription)
    db_session.flush()
    db_session.add_all(
        [
            OntAssignment(
                ont_unit_id=ont.id,
                subscriber_id=subscriber.id,
                subscription_id=subscription.id,
                active=True,
            ),
            IpBlock(pool_id=pool.id, cidr="100.64.10.0/29", is_active=True),
        ]
    )
    db_session.commit()

    result = update_ont_config(
        db_session,
        str(ont.id),
        wan_mode="static_ip",
        wan_static_ip="100.64.10.2",
        push_to_device=False,
    )

    assert result.success is True
    db_session.refresh(ont)
    assert desired_config(ont)["wan"]["static_ip"] == "100.64.10.2"
    assignment = (
        db_session.query(IPAssignment)
        .join(IPAssignment.ipv4_address)
        .filter(IPAssignment.subscriber_id == subscriber.id)
        .filter(IPAssignment.is_active.is_(True))
        .one()
    )
    assert assignment.ipv4_address.address == "100.64.10.2"
    assert assignment.subscription_id == subscription.id


def test_update_ont_config_rejects_static_wan_ip_assigned_elsewhere(
    db_session,
) -> None:
    from app.models.network import (
        IPAssignment,
        IpPool,
        IPv4Address,
        IPVersion,
        OntAssignment,
        OntUnit,
    )
    from app.services.web_network_ont_actions.db_config import update_ont_config

    owner = _make_subscriber(db_session, "static-wan-existing@example.com")
    requester = _make_subscriber(db_session, "static-wan-requester@example.com")
    ont = OntUnit(serial_number="STATIC-WAN-IPAM-CONFLICT", is_active=True)
    pool = IpPool(
        name="Subscriber WAN Conflict Pool",
        ip_version=IPVersion.ipv4,
        cidr="100.64.20.0/29",
        is_active=True,
    )
    db_session.add_all([ont, pool])
    db_session.flush()
    address = IPv4Address(
        address="100.64.20.2",
        pool_id=pool.id,
        is_reserved=False,
        allocation_type="wan",
    )
    db_session.add(address)
    db_session.flush()
    db_session.add_all(
        [
            OntAssignment(
                ont_unit_id=ont.id,
                subscriber_id=requester.id,
                active=True,
            ),
            IPAssignment(
                subscriber_id=owner.id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=address.id,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    result = update_ont_config(
        db_session,
        str(ont.id),
        wan_mode="static_ip",
        wan_static_ip="100.64.20.2",
        push_to_device=False,
    )

    assert result.success is False
    assert "already assigned to another subscriber" in result.message


def test_update_ont_config_does_not_convert_omci_failure_to_pending(
    db_session, monkeypatch
) -> None:
    from app.models.network import OntUnit
    from app.services.web_network_ont_actions.db_config import update_ont_config

    ont = OntUnit(serial_number="OMCI-WAN-FAIL", is_active=True)
    db_session.add(ont)
    db_session.commit()

    def fake_reconcile(*args, **kwargs):
        return SimpleNamespace(
            success=False,
            sync_status="out_of_sync",
            actions_applied=(),
            failure=SimpleNamespace(
                reason="olt_write_rejected",
                message="WAN PPPoE OMCI apply failed: OLT rejected",
            ),
        )

    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        fake_reconcile,
    )

    result = update_ont_config(
        db_session,
        str(ont.id),
        wan_mode="pppoe",
        pppoe_username="100025868",
        pppoe_password="secret",
        push_to_device=True,
        push_wan=True,
        push_lan=False,
        push_mgmt=False,
        push_wifi=False,
    )

    assert result.success is False
    assert result.waiting is False
    assert "WAN PPPoE OMCI apply failed" in result.message


def test_update_ont_config_persists_static_management_pool_values(db_session) -> None:
    from app.models.network import IpPool, IPv4Address, IPVersion, OntUnit
    from app.services.web_network_ont_actions.db_config import update_ont_config

    pool = IpPool(
        name="Management 201",
        cidr="172.16.201.0/24",
        gateway="172.16.201.1",
        ip_version=IPVersion.ipv4,
        is_active=True,
    )
    ont = OntUnit(serial_number="MGMT-FULL-PERSIST", is_active=True)
    db_session.add_all([pool, ont])
    db_session.flush()
    db_session.add(
        IPv4Address(
            address="172.16.201.140",
            pool_id=pool.id,
            is_reserved=True,
            ont_unit_id=ont.id,
            allocation_type="management",
        )
    )
    db_session.commit()

    result = update_ont_config(
        db_session,
        str(ont.id),
        mgmt_ip_mode="static_ip",
        mgmt_ip_address="172.16.201.140",
        push_to_device=False,
    )

    assert result.success is True
    db_session.refresh(ont)
    assert ont.desired_config["management"] == {
        "ip_mode": "static_ip",
        "ip_address": "172.16.201.140",
        "subnet": "255.255.255.0",
        "gateway": "172.16.201.1",
    }
    record = db_session.query(IPv4Address).filter_by(address="172.16.201.140").one()
    assert record.ont_unit_id == ont.id
    assert record.allocation_type == "management"
    assert record.is_reserved is True


def test_saved_wifi_only_desired_config_requires_assignment_for_apply_on_inform(
    db_session,
) -> None:
    from app.models.network import OntAssignment, OntUnit
    from app.services.network.ont_desired_config import set_desired_config_values
    from app.services.tr069 import _ont_has_saved_service_intent

    ont = OntUnit(serial_number="WIFI-INTENT-001", is_active=True)
    set_desired_config_values(
        ont,
        {
            "wifi.enabled": True,
            "wifi.ssid": "DOTMAC-WIFI-INTENT",
        },
    )
    db_session.add(ont)
    db_session.commit()

    assert _ont_has_saved_service_intent(db_session, ont.id) is False

    db_session.add(OntAssignment(ont_unit_id=ont.id, active=True))
    db_session.commit()

    assert _ont_has_saved_service_intent(db_session, ont.id) is True


def test_effective_config_ignores_legacy_assignment_service_fields(db_session) -> None:
    from app.models.network import MgmtIpMode, OntAssignment, OntUnit, OnuMode
    from app.services.network.effective_ont_config import resolve_effective_ont_config
    from app.services.network.ont_desired_config import set_desired_config_values

    ont = OntUnit(serial_number="NO-ASSIGNMENT-FALLBACK", is_active=True)
    set_desired_config_values(
        ont,
        {
            "wan.mode": "dhcp",
            "wifi.ssid": None,
        },
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            active=True,
            wan_mode=OnuMode.routing,
            ip_mode=MgmtIpMode.dhcp,
            pppoe_username="stale-user",
            pppoe_password="stale-password",
            wifi_ssid="STALE-WIFI",
        )
    )
    db_session.commit()

    values = resolve_effective_ont_config(db_session, ont)["values"]

    assert values["wan_mode"] == "dhcp"
    assert values["pppoe_username"] is None
    assert values["pppoe_password"] is None
    assert values["wifi_ssid"] is None


def test_pppoe_health_ignores_legacy_assignment_username() -> None:
    from types import SimpleNamespace

    from app.services.network.pppoe_health import _row_pppoe_username

    row = SimpleNamespace(desired_config={}, ont_pppoe_username="stale-user")

    assert _row_pppoe_username(row) is None


def test_pending_delivery_marker_queues_apply_on_recent_inform(
    db_session, monkeypatch
) -> None:
    from datetime import UTC, datetime

    from app.models.network import OntAssignment, OntUnit
    from app.services.network.ont_desired_config import set_desired_config_values
    from app.services.tr069 import _queue_saved_service_apply_after_stale_inform

    ont = OntUnit(serial_number="PENDING-INFORM-001", is_active=True)
    set_desired_config_values(
        ont,
        {
            "delivery.pending_apply": True,
            "wifi.ssid": "PENDING-INFORM",
        },
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(OntAssignment(ont_unit_id=ont.id, active=True))
    db_session.commit()

    queued = {}

    def fake_enqueue_task(*args, **kwargs):
        queued["args"] = args
        queued["kwargs"] = kwargs
        return type("Dispatch", (), {"queued": True})()

    monkeypatch.setattr("app.services.queue_adapter.enqueue_task", fake_enqueue_task)
    now = datetime.now(UTC)

    result = _queue_saved_service_apply_after_stale_inform(
        db_session,
        ont_id=ont.id,
        previous_last_inform_at=now,
        now=now,
    )

    assert result is True
    assert queued["args"][0] == "app.tasks.tr069.apply_saved_ont_service_config"


def test_successful_saved_service_apply_clears_pending_marker(
    db_session, monkeypatch
) -> None:
    from types import SimpleNamespace

    from app.models.network import OntUnit
    from app.services.network.ont_desired_config import set_desired_config_values
    from app.services.network.ont_provision_steps import apply_saved_service_config

    ont = OntUnit(serial_number="CLEAR-PENDING-001", is_active=True)
    set_desired_config_values(
        ont,
        {
            "delivery.pending_apply": True,
            "wifi.ssid": "CLEAR-PENDING",
        },
    )
    db_session.add(ont)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.genieacs_service",
        SimpleNamespace(
            set_wifi_config=lambda *a, **kw: SimpleNamespace(
                success=True, message="wifi ok"
            )
        ),
    )

    result = apply_saved_service_config(db_session, str(ont.id))

    assert result.success is True
    assert "delivery" not in (ont.desired_config or {})


def test_queued_saved_service_apply_remains_pending(db_session, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.models.network import OntProvisioningStatus, OntUnit
    from app.services.network.ont_desired_config import set_desired_config_values
    from app.services.network.ont_provision_steps import apply_saved_service_config

    ont = OntUnit(serial_number="QUEUED-PENDING-001", is_active=True)
    set_desired_config_values(
        ont,
        {
            "delivery.pending_apply": True,
            "wifi.ssid": "QUEUED-PENDING",
        },
    )
    db_session.add(ont)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.genieacs_service",
        SimpleNamespace(
            set_wifi_config=lambda *a, **kw: SimpleNamespace(
                success=False,
                message="Delivery deferred by ACS.",
                waiting=True,
                data={"delivery_status": "queued", "task_id": "abc"},
            )
        ),
    )

    result = apply_saved_service_config(db_session, str(ont.id))

    assert result.success is False
    assert result.waiting is True
    assert result.data and result.data["pending_deliveries"]
    assert ont.provisioning_status == OntProvisioningStatus.pending_service_config
    assert (ont.desired_config or {})["delivery"]["pending_apply"] is True


def test_structured_acs_queued_write_remains_pending_when_wording_changes(
    db_session, monkeypatch
) -> None:
    from types import SimpleNamespace

    from app.models.network import OntProvisioningStatus, OntUnit
    from app.services.network.ont_desired_config import set_desired_config_values
    from app.services.network.ont_provision_steps import apply_saved_service_config

    ont = OntUnit(serial_number="QUEUED-ACS-401", is_active=True)
    set_desired_config_values(ont, {"wifi.ssid": "WAIT-FOR-INFORM"})
    db_session.add(ont)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.genieacs_service",
        SimpleNamespace(
            set_wifi_config=lambda *a, **kw: SimpleNamespace(
                success=False,
                waiting=False,
                data=None,
                error_code="acs_connection_request_failed",
                message="Upstream wording changed completely.",
            )
        ),
    )

    result = apply_saved_service_config(db_session, str(ont.id))

    assert result.success is False
    assert result.waiting is True
    assert result.data and result.data["pending_deliveries"]
    assert ont.provisioning_status == OntProvisioningStatus.pending_service_config


def test_acs_error_text_without_structured_code_fails_loudly(
    db_session, monkeypatch
) -> None:
    from types import SimpleNamespace

    from app.models.network import OntProvisioningStatus, OntUnit
    from app.services.network.ont_desired_config import set_desired_config_values
    from app.services.network.ont_provision_steps import apply_saved_service_config

    ont = OntUnit(serial_number="UNSTRUCTURED-ACS-401", is_active=True)
    set_desired_config_values(ont, {"wifi.ssid": "FAIL-LOUDLY"})
    db_session.add(ont)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.genieacs_service",
        SimpleNamespace(
            set_wifi_config=lambda *a, **kw: SimpleNamespace(
                success=False,
                waiting=False,
                data=None,
                error_code=None,
                message=(
                    "setParameterValues queued but Connection Request failed: "
                    "HTTP 401 Unauthorized"
                ),
            )
        ),
    )

    result = apply_saved_service_config(db_session, str(ont.id))

    assert result.success is False
    assert result.waiting is False
    assert result.data and "pending_deliveries" not in result.data
    assert ont.provisioning_status == OntProvisioningStatus.failed
