"""Tests for ONT return-to-inventory flow.

Tests the complete flow of returning an ONT to inventory:
- OLT cleanup (service port deletion, deauthorization)
- Database state reset (assignments, service state)
- Event emission and audit logging
- Autofind refresh
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

from app.models.compensation_failure import CompensationFailure
from app.models.network import (
    IPAssignment,
    IpBlock,
    IpPool,
    IPv4Address,
    IPVersion,
    MgmtIpMode,
    OLTDevice,
    OltServicePort,
    OntAssignment,
    OntProvisioningStatus,
    OntSyncStatus,
    OntUnit,
    OntWanServiceInstance,
    OntWanServiceLifecycle,
    OnuOnlineStatus,
    WanServiceType,
)
from app.services.network.ont_actions import ActionResult
from app.services.network.ont_inventory import (
    reset_ont_service_state,
    return_ont_to_inventory,
)


def _add_imported_service_port(
    db_session,
    sample_olt,
    sample_ont,
    *,
    port_index: int = 100,
    fsp: str = "0/1/2",
    ont_id_on_olt: int = 5,
) -> OltServicePort:
    service_port = OltServicePort(
        olt_device_id=sample_olt.id,
        ont_unit_id=sample_ont.id,
        port_index=port_index,
        fsp=fsp,
        ont_id_on_olt=ont_id_on_olt,
        vlan_id=203,
        gem_index=1,
        flow_type="vlan",
        flow_para="203",
        state="up",
        source="test",
    )
    db_session.add(service_port)
    db_session.commit()
    return service_port


@pytest.fixture
def sample_olt(db_session):
    """Create a sample OLT for testing."""
    olt = OLTDevice(
        name="Return-Test-OLT",
        mgmt_ip="10.0.0.50",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()
    return olt


@pytest.fixture
def sample_ont(db_session, sample_olt):
    """Create a sample ONT assigned to OLT."""
    ont = OntUnit(
        serial_number="HWTC12345678",
        is_active=True,
        olt_device_id=sample_olt.id,
        board="0/1",
        port="2",
        external_id="0/1/2:5",
        provisioning_status=OntProvisioningStatus.provisioned,
        olt_status=OnuOnlineStatus.online,
        mac_address="AA:BB:CC:DD:EE:FF",
        observed_wan_ip="192.168.1.100",
        desired_config={"wan_mode": "pppoe", "vlan": 100},
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OltServicePort(
            olt_device_id=sample_olt.id,
            port_index=9999,
            fsp="0/1/9",
            ont_id_on_olt=999,
            vlan_id=203,
            gem_index=1,
            flow_type="vlan",
            flow_para="203",
            state="up",
            source="test",
        )
    )
    db_session.commit()
    return ont


@pytest.fixture
def sample_assignment(db_session, sample_ont):
    """Create an active assignment for the ONT."""
    assignment = OntAssignment(
        ont_unit_id=sample_ont.id,
        active=True,
        assigned_at=datetime.now(UTC),
    )
    db_session.add(assignment)
    db_session.commit()
    return assignment


@pytest.fixture
def sample_wan_service(db_session, sample_ont):
    """Create a WAN service instance for the ONT."""
    wan_service = OntWanServiceInstance(
        ont_id=sample_ont.id,
        service_type=WanServiceType.internet,
        connection_type="pppoe",
    )
    db_session.add(wan_service)
    db_session.commit()
    return wan_service


class TestResetOntServiceState:
    """Tests for reset_ont_service_state function."""

    def test_clears_desired_config(self, db_session, sample_ont):
        """Test that desired_config is cleared."""
        assert sample_ont.desired_config == {"wan_mode": "pppoe", "vlan": 100}

        reset_ont_service_state(db_session, sample_ont, reason="test")
        db_session.flush()

        assert sample_ont.desired_config == {}

    def test_clears_provisioning_status(self, db_session, sample_ont):
        """Test that provisioning_status is reset to unprovisioned."""
        assert sample_ont.provisioning_status == OntProvisioningStatus.provisioned

        reset_ont_service_state(db_session, sample_ont, reason="test")
        db_session.flush()

        assert sample_ont.provisioning_status == OntProvisioningStatus.unprovisioned

    def test_clears_network_state(self, db_session, sample_ont):
        """Test that network state fields are cleared."""
        assert sample_ont.mac_address == "AA:BB:CC:DD:EE:FF"
        assert sample_ont.observed_wan_ip == "192.168.1.100"

        reset_ont_service_state(db_session, sample_ont, reason="test")
        db_session.flush()

        assert sample_ont.mac_address is None
        assert sample_ont.observed_wan_ip is None
        assert sample_ont.observed_pppoe_status is None
        assert sample_ont.observed_lan_mode is None
        assert sample_ont.tr069_last_snapshot == {}
        assert sample_ont.olt_observed_snapshot == {}

    def test_clears_olt_status(self, db_session, sample_ont):
        """Test that OLT status is reset to unknown."""
        assert sample_ont.olt_status == OnuOnlineStatus.online

        reset_ont_service_state(db_session, sample_ont, reason="test")
        db_session.flush()

        assert sample_ont.olt_status == OnuOnlineStatus.offline
        assert sample_ont.last_seen_at is None

    def test_retires_wan_service_instances_without_destroying_them(
        self, db_session, sample_ont, sample_wan_service
    ):
        """The reset closes WAN service intents; it no longer deletes them.

        This test previously asserted the row count fell to zero. Deletion threw
        away the only record of what a service had been declared to be, which is
        exactly the evidence a later "did this ONT ever legitimately terminate
        PPP for this subscription?" adjudication needs. The row now survives in
        ``retired``, carrying the reason the reset was performed.
        """
        rows = db_session.query(OntWanServiceInstance).filter(
            OntWanServiceInstance.ont_id == sample_ont.id
        )
        assert rows.count() == 1

        reset_ont_service_state(db_session, sample_ont, reason="test")
        db_session.flush()

        assert rows.count() == 1, "history must survive the reset"
        instance = rows.one()
        assert instance.lifecycle_state is OntWanServiceLifecycle.retired
        assert instance.retired_at is not None
        assert instance.retired_reason == "test"
        # is_active is derived from lifecycle_state, never a second authority.
        assert instance.is_active is False


def test_return_to_inventory_releases_management_ip_for_reauthorization(
    db_session, sample_ont, sample_olt, sample_assignment
):
    """Returned ONTs release management IP reservations before reuse."""
    from app.services.network.ont_management_ipam import allocate_ont_management_ip

    pool = IpPool(
        name="Return Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.201.0/24",
        gateway="172.16.201.1",
        olt_device_id=sample_olt.id,
        is_active=True,
    )
    db_session.add(pool)
    db_session.flush()
    db_session.add(IpBlock(pool_id=pool.id, cidr="172.16.201.0/30", is_active=True))
    sample_olt.mgmt_ip_pool_id = pool.id
    sample_assignment.mgmt_ip_mode = MgmtIpMode.static_ip
    sample_assignment.mgmt_ip_address = "172.16.201.2"
    reserved = IPv4Address(
        address="172.16.201.2",
        pool_id=pool.id,
        is_reserved=True,
        notes=f"ont:{sample_ont.id}",
        ont_unit_id=sample_ont.id,
        allocation_type="management",
    )
    db_session.add(reserved)
    db_session.commit()

    mock_adapter = MagicMock()
    mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
        success=True,
        message="OK",
        data={"service_ports": []},
    )
    mock_adapter.deauthorize_ont.return_value = ActionResult(
        success=True,
        message="ONT deauthorized",
    )

    with (
        patch(
            "app.services.network.olt_protocol_adapters.get_protocol_adapter",
            return_value=mock_adapter,
        ),
    ):
        result = return_ont_to_inventory(db_session, str(sample_ont.id))

    assert result.success is True
    assert "management IP released" in result.message
    db_session.refresh(sample_assignment)
    db_session.refresh(reserved)
    assert sample_assignment.mgmt_ip_address is None
    assert sample_assignment.mgmt_ip_mode == MgmtIpMode.inactive
    assert reserved.is_reserved is False
    assert reserved.notes is None
    assert reserved.ont_unit_id is None
    assert reserved.allocation_type is None

    sample_ont.olt_device_id = sample_olt.id
    sample_assignment.active = True
    db_session.commit()
    allocation = allocate_ont_management_ip(
        db_session,
        ont=sample_ont,
        olt=sample_olt,
    )

    assert allocation.address == "172.16.201.2"


def test_return_to_inventory_clears_historical_assignment_management_ips(
    db_session, sample_ont, sample_olt, sample_assignment
):
    """Returned ONTs do not retain stale management IPs in assignment history."""
    sample_assignment.active = False
    sample_assignment.released_at = datetime.now(UTC)
    sample_assignment.release_reason = "previous_return"
    sample_assignment.mgmt_ip_mode = MgmtIpMode.static_ip
    sample_assignment.mgmt_ip_address = "172.16.201.3"
    sample_assignment.mgmt_subnet = "255.255.255.0"
    sample_assignment.mgmt_gateway = "172.16.201.1"
    db_session.commit()

    mock_adapter = MagicMock()
    mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
        success=True,
        message="OK",
        data={"service_ports": []},
    )
    mock_adapter.deauthorize_ont.return_value = ActionResult(
        success=True,
        message="ONT deauthorized",
    )

    with (
        patch(
            "app.services.network.olt_protocol_adapters.get_protocol_adapter",
            return_value=mock_adapter,
        ),
    ):
        result = return_ont_to_inventory(db_session, str(sample_ont.id))

    assert result.success is True
    db_session.refresh(sample_assignment)
    assert sample_assignment.mgmt_ip_address is None
    assert sample_assignment.mgmt_ip_mode == MgmtIpMode.inactive


def test_return_to_inventory_does_not_release_management_ip_owned_by_other_ont(
    db_session, sample_ont, sample_olt, sample_assignment
):
    """Historical management IP cleanup must not clear another ONT's reservation."""
    other_ont = OntUnit(
        serial_number="HWTC87654321",
        is_active=True,
    )
    db_session.add(other_ont)
    db_session.flush()
    sample_assignment.active = False
    sample_assignment.released_at = datetime.now(UTC)
    sample_assignment.release_reason = "previous_return"
    sample_assignment.mgmt_ip_mode = MgmtIpMode.static_ip
    sample_assignment.mgmt_ip_address = "172.16.201.9"
    sample_assignment.mgmt_subnet = "255.255.255.0"
    sample_assignment.mgmt_gateway = "172.16.201.1"
    other_record = IPv4Address(
        address="172.16.201.9",
        is_reserved=True,
        notes=f"ont:{other_ont.id}",
        ont_unit_id=other_ont.id,
        allocation_type="management",
    )
    db_session.add(other_record)
    db_session.commit()

    mock_adapter = MagicMock()
    mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
        success=True,
        message="OK",
        data={"service_ports": []},
    )
    mock_adapter.deauthorize_ont.return_value = ActionResult(
        success=True,
        message="ONT deauthorized",
    )

    with (
        patch(
            "app.services.network.olt_protocol_adapters.get_protocol_adapter",
            return_value=mock_adapter,
        ),
    ):
        result = return_ont_to_inventory(db_session, str(sample_ont.id))

    assert result.success is True
    db_session.refresh(sample_assignment)
    db_session.refresh(other_record)
    assert sample_assignment.mgmt_ip_address is None
    assert sample_assignment.mgmt_ip_mode == MgmtIpMode.inactive
    assert other_record.is_reserved is True
    assert other_record.notes == f"ont:{other_ont.id}"
    assert other_record.ont_unit_id == other_ont.id
    assert other_record.allocation_type == "management"


class TestReturnOntToInventory:
    """Tests for return_ont_to_inventory function."""

    def test_success_clears_olt_binding(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test successful return clears OLT binding fields."""
        # Mock external dependencies
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(sample_ont)
        assert sample_ont.olt_device_id is None
        assert sample_ont.board is None
        assert sample_ont.port is None
        assert sample_ont.external_id is None

    def test_success_closes_assignment(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test successful return closes active assignment."""
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(sample_assignment)
        assert sample_assignment.active is False
        assert sample_assignment.released_at is not None
        assert sample_assignment.release_reason == "returned_to_inventory"

    def test_success_clears_subscriber_assignment_links(
        self,
        db_session,
        sample_ont,
        sample_olt,
        sample_assignment,
        subscriber,
        subscription,
    ):
        """Returned inventory ONTs must not keep stale subscriber links."""
        sample_assignment.subscriber_id = subscriber.id
        sample_assignment.subscription_id = subscription.id
        sample_assignment.pon_port_id = sample_ont.pon_port_id
        sample_assignment.pppoe_username = "old-user"
        sample_assignment.pppoe_password = "old-password"
        sample_assignment.wifi_ssid = "old-wifi"
        sample_assignment.static_ip = "100.64.10.10"
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        assert "subscriber assignment links cleared" in result.message
        db_session.refresh(sample_assignment)
        assert sample_assignment.active is False
        assert sample_assignment.subscription_id is None
        assert sample_assignment.subscriber_id is None
        assert sample_assignment.service_address_id is None
        assert sample_assignment.pon_port_id is None
        assert sample_assignment.pppoe_username is None
        assert sample_assignment.pppoe_password is None
        assert sample_assignment.wifi_ssid is None
        assert sample_assignment.static_ip is None

    def test_success_releases_subscriber_wan_static_ipam_assignment(
        self, db_session, sample_ont, sample_olt, sample_assignment, subscriber
    ):
        """Returned inventory ONTs release their subscriber WAN static IPAM claim."""
        pool = IpPool(
            name="Return WAN Pool",
            ip_version=IPVersion.ipv4,
            cidr="100.64.30.0/24",
            gateway="100.64.30.1",
            is_active=True,
        )
        db_session.add(pool)
        db_session.flush()
        db_session.add(IpBlock(pool_id=pool.id, cidr="100.64.30.0/30", is_active=True))
        address = IPv4Address(
            address="100.64.30.2",
            pool_id=pool.id,
            allocation_type="wan",
        )
        db_session.add(address)
        db_session.flush()
        ip_assignment = IPAssignment(
            subscriber_id=subscriber.id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
            is_active=True,
        )
        sample_assignment.subscriber_id = subscriber.id
        sample_assignment.static_ip = "100.64.30.2"
        sample_ont.desired_config = {"wan": {"static_ip": "100.64.30.2"}}
        db_session.add(ip_assignment)
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        assert "static WAN IP released" in result.message
        db_session.refresh(ip_assignment)
        db_session.refresh(address)
        assert ip_assignment.is_active is False
        assert address.allocation_type is None

    def test_success_keeps_ont_active(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that ONT remains active for reuse (not decommissioned)."""
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(sample_ont)
        assert sample_ont.is_active is True

    def test_success_resets_service_state(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that service state is reset."""
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(sample_ont)
        assert sample_ont.desired_config == {}
        assert sample_ont.provisioning_status == OntProvisioningStatus.unprovisioned

    def test_success_returns_data_with_previous_olt(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that result data includes previous OLT info."""
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        assert result.data is not None
        assert result.data["olt_id"] == str(sample_olt.id)
        assert result.data["serial_number"] == "HWTC12345678"
        assert result.data["unconfigured_candidate_ready"] is True
        assert "view=unconfigured" in result.data["unconfigured_url"]

    def test_return_creates_unconfigured_candidate_without_autofind_refresh(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Return creates a reusable candidate without polling the OLT."""
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        assert "unconfigured candidate ready" in result.message.lower()
        db_session.refresh(sample_ont)
        db_session.refresh(sample_assignment)
        assert sample_ont.olt_device_id is None
        assert sample_assignment.active is False

    def test_db_update_failure_rolls_back_local_state(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """After OLT cleanup, DB failures return a controlled error and rollback."""
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.network.ont_inventory.ensure_cpe_for_ont",
                side_effect=RuntimeError("inventory subscriber missing"),
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is False
        assert "db inventory update failed" in result.message.lower()
        db_session.refresh(sample_ont)
        db_session.refresh(sample_assignment)
        assert sample_ont.olt_device_id == sample_olt.id
        assert sample_ont.board == "0/1"
        assert sample_ont.port == "2"
        assert sample_ont.external_id == "0/1/2:5"
        assert sample_assignment.active is True
        assert sample_assignment.released_at is None
        assert sample_assignment.release_reason is None
        failure = db_session.query(CompensationFailure).one()
        assert failure.operation_type == "return_to_inventory"
        assert failure.step_name == "manual_return_cleanup_review"
        assert failure.ont_unit_id == sample_ont.id
        assert failure.olt_device_id == sample_olt.id
        assert "inventory subscriber missing" in failure.error_message

    def test_failure_olt_cleanup_stops_return(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that failure during OLT cleanup stops the return process."""
        sample_ont.sync_status = OntSyncStatus.out_of_sync
        sample_ont.last_error = "current assignment delivery failed"
        db_session.commit()
        _add_imported_service_port(db_session, sample_olt, sample_ont)
        mock_adapter = MagicMock()
        mock_adapter.delete_service_port.return_value = ActionResult(
            success=False,
            message="SSH delete failed",
        )

        with patch(
            "app.services.network.olt_protocol_adapters.get_protocol_adapter",
            return_value=mock_adapter,
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is False
        assert "cleanup" in result.message.lower() or "ssh" in result.message.lower()

        # ONT state should NOT be changed
        db_session.refresh(sample_ont)
        assert sample_ont.olt_device_id == sample_olt.id
        assert sample_ont.sync_status is OntSyncStatus.out_of_sync
        assert sample_ont.last_error == "current assignment delivery failed"
        db_session.refresh(sample_assignment)
        assert sample_assignment.active is True

    def test_deletes_service_ports_on_olt(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that service ports are deleted from OLT."""
        _add_imported_service_port(db_session, sample_olt, sample_ont)
        mock_adapter = MagicMock()
        mock_adapter.delete_service_port.return_value = ActionResult(
            success=True,
            message="Deleted",
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        mock_adapter.delete_service_port.assert_called_once_with(100)
        mock_adapter.deauthorize_ont.assert_called_once()
        assert (
            db_session.query(OltServicePort)
            .filter(OltServicePort.port_index == 100)
            .count()
            == 0
        )

    @pytest.mark.skipif(
        not bool(__import__("os").getenv("TEST_DATABASE_URL")),
        reason="Multiple active assignments test requires PostgreSQL (partial index)",
    )
    def test_handles_multiple_assignments(self, db_session, sample_ont, sample_olt):
        """Test that multiple active assignments are all closed.

        Note: This test only works with PostgreSQL due to partial unique index.
        SQLite treats the partial index as a full unique constraint.
        """
        # Create multiple assignments (edge case)
        assignment1 = OntAssignment(
            ont_unit_id=sample_ont.id,
            active=True,
            assigned_at=datetime.now(UTC),
        )
        assignment2 = OntAssignment(
            ont_unit_id=sample_ont.id,
            active=True,
            assigned_at=datetime.now(UTC),
        )
        db_session.add_all([assignment1, assignment2])
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(assignment1)
        db_session.refresh(assignment2)
        assert assignment1.active is False
        assert assignment2.active is False


class TestReturnToInventoryWebAction:
    """Tests for the web action version of return_to_inventory."""

    def test_success_emits_events(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that success emits deauthorization and service port events."""
        from app.services.network.ont_inventory import return_ont_to_inventory

        _add_imported_service_port(db_session, sample_olt, sample_ont)
        mock_adapter = MagicMock()
        mock_adapter.delete_service_port.return_value = ActionResult(
            success=True,
            message="Deleted",
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch("app.services.network.ont_inventory.emit_event") as mock_emit,
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        # Check events were emitted
        assert mock_emit.call_count >= 2  # service port deleted + ont deauthorized

    def test_clears_tr069_binding(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that TR-069 device binding is cleared."""
        from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
        from app.services.network.ont_inventory import return_ont_to_inventory

        # Create ACS server first (required FK)
        acs_server = Tr069AcsServer(
            name="Test ACS",
            base_url="http://localhost:7557",
        )
        db_session.add(acs_server)
        db_session.flush()

        # Create TR-069 device linked to ONT
        tr069_device = Tr069CpeDevice(
            serial_number="TR069-12345",
            acs_server_id=acs_server.id,
            ont_unit_id=sample_ont.id,
        )
        db_session.add(tr069_device)
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )
        mock_client = MagicMock()
        mock_client.list_devices.return_value = []

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.genieacs_client.create_genieacs_client",
                return_value=mock_client,
            ),
            patch("app.services.network.ont_inventory.emit_event"),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(tr069_device)
        assert tr069_device.ont_unit_id is None

    def test_deletes_genieacs_device_before_clearing_tr069_binding(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Return to inventory deletes the ACS device record when one is linked."""
        from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
        from app.services.network.ont_inventory import return_ont_to_inventory

        acs_server = Tr069AcsServer(
            name="Return ACS",
            base_url="http://genieacs.example:7557",
        )
        db_session.add(acs_server)
        db_session.flush()
        tr069_device = Tr069CpeDevice(
            serial_number=sample_ont.serial_number,
            acs_server_id=acs_server.id,
            ont_unit_id=sample_ont.id,
            genieacs_device_id="ABC-ONT-HWTC12345678",
        )
        db_session.add(tr069_device)
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )
        mock_client = MagicMock()
        mock_client.list_devices.return_value = []

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.genieacs_client.create_genieacs_client",
                return_value=mock_client,
            ) as create_client,
            patch("app.services.network.ont_inventory.emit_event"),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        create_client.assert_called_once_with("http://genieacs.example:7557")
        mock_client.delete_device.assert_called_once_with("ABC-ONT-HWTC12345678")
        db_session.refresh(tr069_device)
        assert tr069_device.ont_unit_id is None
        assert tr069_device.genieacs_device_id is None

    def test_genieacs_delete_failure_stops_after_olt_cleanup(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Do not clear local inventory if ACS cleanup fails after OLT cleanup."""
        from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
        from app.services.genieacs_client import GenieACSError
        from app.services.network.ont_inventory import return_ont_to_inventory

        acs_server = Tr069AcsServer(
            name="Return ACS Failure",
            base_url="http://genieacs.example:7557",
        )
        db_session.add(acs_server)
        db_session.flush()
        tr069_device = Tr069CpeDevice(
            serial_number=sample_ont.serial_number,
            acs_server_id=acs_server.id,
            ont_unit_id=sample_ont.id,
            genieacs_device_id="ABC-ONT-HWTC12345678",
        )
        db_session.add(tr069_device)
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )
        mock_client = MagicMock()
        mock_client.delete_device.side_effect = GenieACSError("API error: 502")

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.genieacs_client.create_genieacs_client",
                return_value=mock_client,
            ),
            patch("app.services.network.ont_inventory.emit_event"),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is False
        assert "acs device" in result.message.lower()
        mock_adapter.get_service_ports_for_ont.assert_called_once()
        mock_adapter.deauthorize_ont.assert_called_once()
        db_session.refresh(sample_ont)
        db_session.refresh(sample_assignment)
        db_session.refresh(tr069_device)
        assert sample_ont.olt_device_id == sample_olt.id
        assert sample_assignment.active is True
        assert tr069_device.ont_unit_id == sample_ont.id

    def test_genieacs_same_serial_device_is_removed_without_tr069_link(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Return cleanup removes same-serial GenieACS records even if no local link exists."""
        from app.models.tr069 import Tr069AcsServer
        from app.services.network.ont_inventory import return_ont_to_inventory

        acs_server = Tr069AcsServer(
            name="Return ACS Search",
            base_url="http://genieacs.example:7557",
            is_active=True,
        )
        db_session.add(acs_server)
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )
        mock_client = MagicMock()
        mock_client.list_devices.return_value = [{"_id": "ABC-ONT-HWTC12345678"}]

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.genieacs_client.create_genieacs_client",
                return_value=mock_client,
            ),
            patch("app.services.network.ont_inventory.emit_event"),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        mock_client.delete_device.assert_called_once_with("ABC-ONT-HWTC12345678")
        assert mock_client.list_devices.call_count >= 1

    def test_return_to_inventory_clears_same_serial_local_acs_identity(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Return cleanup clears stale same-serial local ACS identity rows."""
        from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
        from app.services.network.ont_inventory import return_ont_to_inventory

        acs_server = Tr069AcsServer(
            name="Return ACS Local Identity",
            base_url="http://genieacs.example:7557",
            is_active=True,
        )
        db_session.add(acs_server)
        db_session.flush()
        tr069_device = Tr069CpeDevice(
            serial_number=sample_ont.serial_number,
            acs_server_id=acs_server.id,
            genieacs_device_id="ABC-ONT-HWTC12345678",
            connection_request_url="http://192.0.2.10:7547/",
            is_active=True,
        )
        db_session.add(tr069_device)
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )
        mock_client = MagicMock()
        mock_client.list_devices.return_value = []

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.genieacs_client.create_genieacs_client",
                return_value=mock_client,
            ),
            patch("app.services.network.ont_inventory.emit_event"),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(tr069_device)
        assert tr069_device.genieacs_device_id is None
        assert tr069_device.connection_request_url is None

    def test_olt_cleanup_failure_stops_before_acs_delete(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """If OLT cleanup fails, ACS and local cleanup are not attempted."""
        from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
        from app.services.network.ont_inventory import return_ont_to_inventory

        acs_server = Tr069AcsServer(
            name="Return ACS Partial",
            base_url="http://genieacs.example:7557",
            is_active=True,
        )
        db_session.add(acs_server)
        db_session.flush()
        tr069_device = Tr069CpeDevice(
            serial_number=sample_ont.serial_number,
            acs_server_id=acs_server.id,
            ont_unit_id=sample_ont.id,
            genieacs_device_id="ABC-ONT-HWTC12345678",
        )
        db_session.add(tr069_device)
        db_session.commit()
        _add_imported_service_port(db_session, sample_olt, sample_ont)

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.delete_service_port.return_value = ActionResult(
            success=False,
            message="SSH delete failed",
        )
        mock_client = MagicMock()
        mock_client.list_devices.return_value = []

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.genieacs_client.create_genieacs_client",
                return_value=mock_client,
            ),
            patch("app.services.network.ont_inventory.emit_event"),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is False
        assert "local cleanup" in result.message.lower()
        mock_client.delete_device.assert_not_called()
        mock_adapter.get_service_ports_for_ont.assert_called_once()
        db_session.refresh(sample_ont)
        db_session.refresh(sample_assignment)
        db_session.refresh(tr069_device)
        assert sample_ont.olt_device_id == sample_olt.id
        assert sample_assignment.active is True
        assert tr069_device.ont_unit_id == sample_ont.id

        assert db_session.query(CompensationFailure).count() == 0

    def test_records_compensation_when_acs_cleanup_fails_after_olt_cleanup(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """If OLT cleanup succeeds but ACS cleanup fails, record manual review."""
        from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
        from app.services.genieacs_client import GenieACSError
        from app.services.network.ont_inventory import return_ont_to_inventory

        acs_server = Tr069AcsServer(
            name="Return ACS Partial",
            base_url="http://genieacs.example:7557",
            is_active=True,
        )
        db_session.add(acs_server)
        db_session.flush()
        tr069_device = Tr069CpeDevice(
            serial_number=sample_ont.serial_number,
            acs_server_id=acs_server.id,
            ont_unit_id=sample_ont.id,
            genieacs_device_id="ABC-ONT-HWTC12345678",
        )
        db_session.add(tr069_device)
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )
        mock_client = MagicMock()
        mock_client.delete_device.side_effect = GenieACSError("API error: 502")

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.genieacs_client.create_genieacs_client",
                return_value=mock_client,
            ),
            patch("app.services.network.ont_inventory.emit_event"),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is False
        assert "acs device" in result.message.lower()
        mock_adapter.deauthorize_ont.assert_called_once()
        mock_client.delete_device.assert_called_once_with("ABC-ONT-HWTC12345678")
        db_session.refresh(sample_ont)
        db_session.refresh(sample_assignment)
        db_session.refresh(tr069_device)
        assert sample_ont.olt_device_id == sample_olt.id
        assert sample_assignment.active is True
        assert tr069_device.ont_unit_id == sample_ont.id

        failure = db_session.query(CompensationFailure).one()
        assert failure.operation_type == "return_to_inventory"
        assert failure.step_name == "manual_return_cleanup_review"
        assert failure.ont_unit_id == sample_ont.id
        assert failure.olt_device_id == sample_olt.id
        assert failure.interface_path == "0/1/2"

    def test_releases_service_port_allocations(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that service port DB allocations are released."""
        from app.services.network.ont_inventory import return_ont_to_inventory

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch("app.services.network.ont_inventory.emit_event"),
            patch(
                "app.services.network.service_port_allocator.release_all_for_ont",
                return_value=2,
            ) as mock_release,
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        mock_release.assert_called_once_with(db_session, str(sample_ont.id))


class TestReturnToInventoryForWeb:
    """Tests for the route-friendly wrapper."""

    def test_returns_not_found_for_missing_ont(self, db_session):
        """Test that missing ONT returns failure result."""
        from app.services.web_network_ont_actions.inventory import (
            return_to_inventory_for_web,
        )

        result = return_to_inventory_for_web(
            db_session,
            "00000000-0000-0000-0000-000000000000",
        )

        assert result.success is False
        assert "not found" in result.message.lower()

    def test_logs_audit_event(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that audit event is logged."""
        from app.services.web_network_ont_actions.inventory import (
            return_to_inventory_for_web,
        )

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch("app.services.network.ont_inventory.emit_event"),
            patch(
                "app.services.web_network_ont_actions.inventory._log_action_audit"
            ) as mock_audit,
        ):
            result = return_to_inventory_for_web(db_session, str(sample_ont.id))

        assert result.success is True
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["action"] == "return_to_inventory"
        assert call_kwargs["ont_id"] == sample_ont.id


def test_admin_return_to_inventory_runs_synchronously(monkeypatch):
    from app.web.admin import network_onts_actions

    calls = {}

    class Request:
        headers = {"hx-request": "true"}

    def fail_enqueue(*args, **kwargs):
        raise AssertionError("return-to-inventory must not enqueue Celery")

    def fake_return_to_inventory_for_web(db, ont_id, *, request=None):
        calls["db"] = db
        calls["ont_id"] = ont_id
        calls["request"] = request
        return SimpleNamespace(success=True, message="ONT returned to inventory")

    monkeypatch.setattr(
        network_onts_actions,
        "_ensure_ont_write_scope",
        lambda request, db, ont_id: None,
    )
    monkeypatch.setattr(
        network_onts_actions.web_network_ont_actions_service,
        "return_to_inventory_for_web",
        fake_return_to_inventory_for_web,
    )
    monkeypatch.setattr(
        network_onts_actions, "enqueue_task", fail_enqueue, raising=False
    )

    response = network_onts_actions.ont_return_to_inventory(
        Request(),
        "ont-1",
        object(),
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/admin/network/onts?view=unconfigured"
    assert calls["ont_id"] == "ont-1"


class TestCleanupOltStateForReturn:
    """Tests for the OLT cleanup helper."""

    def test_returns_success_when_no_olt_context(self, db_session):
        """Test that cleanup succeeds when ONT has no OLT context."""
        from app.services.web_network_ont_actions.inventory import (
            _cleanup_olt_state_for_return,
        )

        # Create ONT without OLT binding
        ont = OntUnit(
            serial_number="UNBOUND-ONT",
            is_active=True,
            olt_device_id=None,
            board=None,
            port=None,
        )
        db_session.add(ont)
        db_session.commit()

        success, completed, errors = _cleanup_olt_state_for_return(
            db_session, str(ont.id)
        )

        assert success is True
        assert errors == []

    def test_failure_when_service_port_read_fails(
        self, db_session, sample_ont, sample_olt
    ):
        """Test that failure to delete imported service ports stops cleanup."""
        from app.services.web_network_ont_actions.inventory import (
            _cleanup_olt_state_for_return,
        )

        _add_imported_service_port(db_session, sample_olt, sample_ont)
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.delete_service_port.return_value = ActionResult(
            success=False,
            message="Connection timeout",
        )

        with patch(
            "app.services.network.olt_protocol_adapters.get_protocol_adapter",
            return_value=mock_adapter,
        ):
            success, completed, errors = _cleanup_olt_state_for_return(
                db_session, str(sample_ont.id)
            )

        assert success is False
        assert any("service-port" in e.lower() for e in errors)
        mock_adapter.get_service_ports_for_ont.assert_called_once()

    def test_reconciles_stale_imported_service_port_when_absent_on_olt(
        self, db_session, sample_ont, sample_olt
    ):
        """Delete stale imported service-port rows when live OLT state is already clean."""
        from app.services.web_network_ont_actions.inventory import (
            _cleanup_olt_state_for_return,
        )

        _add_imported_service_port(
            db_session,
            sample_olt,
            sample_ont,
            port_index=42,
        )
        mock_adapter = MagicMock()
        mock_adapter.delete_service_port.return_value = ActionResult(
            success=False,
            message="OLT rejected: Failure: Service virtual port does not exist",
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="OK",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.network.olt_ssh_service_ports.get_service_port_by_index",
                return_value=(
                    False,
                    "OLT rejected: Failure: Service virtual port does not exist",
                    None,
                ),
            ),
            patch(
                "app.services.network.service_port_allocator.release_all_for_ont",
                return_value=0,
            ),
        ):
            success, completed, errors = _cleanup_olt_state_for_return(
                db_session, str(sample_ont.id)
            )

        assert success is True
        assert errors == []
        assert any("already absent" in c.lower() for c in completed)
        assert (
            db_session.query(OltServicePort)
            .filter(OltServicePort.port_index == 42)
            .first()
            is None
        )
        mock_adapter.deauthorize_ont.assert_called_once_with("0/1/2", 5)

    def test_failure_when_deauthorization_fails(
        self, db_session, sample_ont, sample_olt
    ):
        """Test that deauthorization failure stops cleanup."""
        from app.services.web_network_ont_actions.inventory import (
            _cleanup_olt_state_for_return,
        )

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=False,
            message="Permission denied",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.network.service_port_allocator.release_all_for_ont",
                return_value=1,
            ) as mock_release,
        ):
            success, completed, errors = _cleanup_olt_state_for_return(
                db_session, str(sample_ont.id)
            )

        assert success is False
        assert any("deauthorize" in e.lower() for e in errors)
        mock_release.assert_not_called()

    def test_success_when_ont_already_absent_on_olt(
        self, db_session, sample_ont, sample_olt
    ):
        """Return cleanup is idempotent when the OLT has already removed the ONT."""
        from app.services.web_network_ont_actions.inventory import (
            _cleanup_olt_state_for_return,
        )

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=False,
            message="OLT rejected: Failure: The ONT does not exist",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.network.service_port_allocator.release_all_for_ont",
                return_value=1,
            ),
        ):
            success, completed, errors = _cleanup_olt_state_for_return(
                db_session, str(sample_ont.id)
            )

        assert success is True
        assert errors == []
        assert any("already absent" in c.lower() for c in completed)
        assert any("allocation" in c.lower() for c in completed)

    def test_tracks_completed_steps(self, db_session, sample_ont, sample_olt):
        """Test that completed steps are tracked."""
        from app.services.web_network_ont_actions.inventory import (
            _cleanup_olt_state_for_return,
        )

        _add_imported_service_port(
            db_session,
            sample_olt,
            sample_ont,
            port_index=42,
        )
        mock_adapter = MagicMock()
        mock_adapter.delete_service_port.return_value = ActionResult(
            success=True,
            message="Deleted",
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="OK",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.network.service_port_allocator.release_all_for_ont",
                return_value=1,
            ),
        ):
            success, completed, errors = _cleanup_olt_state_for_return(
                db_session, str(sample_ont.id)
            )

        assert success is True
        assert any("service-port 42" in c.lower() for c in completed)
        assert any("deauthorized" in c.lower() for c in completed)
        assert any("allocation" in c.lower() for c in completed)
        mock_adapter.get_service_ports_for_ont.assert_called_once()


class TestOntWithoutOltBinding:
    """Tests for ONTs that have no OLT binding."""

    def test_return_works_without_olt(self, db_session):
        """Test that return works for ONT without OLT binding."""
        # Create ONT without OLT binding (e.g., manually added)
        ont = OntUnit(
            serial_number="STANDALONE-ONT",
            is_active=True,
            olt_device_id=None,
            board=None,
            port=None,
            provisioning_status=OntProvisioningStatus.provisioned,
            desired_config={"some": "config"},
        )
        db_session.add(ont)
        db_session.flush()  # Get ont.id before using in assignment

        assignment = OntAssignment(
            ont_unit_id=ont.id,
            active=True,
        )
        db_session.add(assignment)
        db_session.commit()

        result = return_ont_to_inventory(db_session, str(ont.id))

        assert result.success is True
        db_session.refresh(ont)
        db_session.refresh(assignment)
        assert ont.desired_config == {}
        assert ont.provisioning_status == OntProvisioningStatus.unprovisioned
        assert assignment.active is False


def test_return_to_inventory_htmx_buttons_send_csrf_header():
    """Single-ONT HTMX return actions must pass CSRF middleware before service code runs."""
    hero_template = Path("templates/admin/network/onts/_hero_header.html").read_text()
    health_template = Path(
        "templates/admin/network/onts/_operational_health.html"
    ).read_text()

    for template in (hero_template, health_template):
        assert "/return-to-inventory" in template
        assert '"X-CSRF-Token": "{{ request.state.csrf_token }}"' in template


def test_factory_reset_clears_stale_observed_runtime(db_session, sample_ont):
    """Successful factory reset invalidates runtime observations from the old config."""
    from app.services.network.ont_action_device import factory_reset

    observed_at = datetime.now(UTC)
    sample_ont.observed_wan_ip = "100.64.10.5"
    sample_ont.observed_pppoe_status = "Connected"
    sample_ont.observed_lan_mode = "router"
    sample_ont.observed_wifi_clients = 3
    sample_ont.observed_lan_hosts = 5
    sample_ont.observed_runtime_updated_at = observed_at
    sample_ont.tr069_last_snapshot = {"wan": {"WAN IP": "100.64.10.5"}}
    sample_ont.tr069_last_snapshot_at = observed_at
    db_session.commit()

    mock_client = MagicMock()
    mock_client.factory_reset_and_wait.return_value = {"task": "completed"}

    with patch(
        "app.services.network.ont_action_device.get_ont_client_or_error",
        return_value=((sample_ont, mock_client, "ABC-ONT-HWTC12345678"), None),
    ):
        result = factory_reset(db_session, str(sample_ont.id))

    assert result.success is True
    mock_client.factory_reset_and_wait.assert_called_once_with("ABC-ONT-HWTC12345678")
    assert sample_ont.observed_wan_ip is None
    assert sample_ont.observed_pppoe_status is None
    assert sample_ont.observed_lan_mode is None
    assert sample_ont.observed_wifi_clients is None
    assert sample_ont.observed_lan_hosts is None
    assert sample_ont.observed_runtime_updated_at is None
    assert sample_ont.tr069_last_snapshot == {}
    assert sample_ont.tr069_last_snapshot_at is None


def test_factory_reset_buttons_require_confirmation():
    """Every single-ONT factory reset affordance must require explicit confirmation."""
    hero_template = Path("templates/admin/network/onts/_hero_header.html").read_text()
    health_template = Path(
        "templates/admin/network/onts/_operational_health.html"
    ).read_text()

    expected = (
        'hx-confirm="Factory reset will ERASE ALL configuration on this ONT. '
        "The device will reboot and return to factory defaults. Continue?"
    )
    for template in (hero_template, health_template):
        assert "/factory-reset" in template
        assert expected in template


def _request_for_action() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/network/onts/test/reboot",
            "headers": [],
        }
    )


def test_ont_reboot_source_olt_uses_omci_transport(monkeypatch):
    """The OLT reboot button must not fall through to the TR-069 reboot path."""
    from app.web.admin import network_onts_actions

    omci_calls: list[tuple[object, str, object]] = []
    tr069_calls: list[tuple[object, str]] = []

    def fake_omci_reboot(db, ont_id, *, initiated_by=None):
        omci_calls.append((db, ont_id, initiated_by))
        return True, "ONT reboot command sent via OLT."

    def fake_tr069_reboot(db, ont_id, *, request=None):
        tr069_calls.append((db, ont_id))
        return ActionResult(True, "TR-069 reboot sent")

    monkeypatch.setattr(
        network_onts_actions,
        "_ensure_ont_write_scope",
        lambda *args: None,
    )
    monkeypatch.setattr(
        network_onts_actions.web_network_ont_actions_service,
        "execute_omci_reboot",
        fake_omci_reboot,
    )
    monkeypatch.setattr(
        network_onts_actions.web_network_ont_actions_service,
        "execute_reboot",
        fake_tr069_reboot,
    )

    db = object()
    response = network_onts_actions.ont_reboot(
        _request_for_action(),
        "ont-1",
        source="olt",
        db=db,
    )

    assert response.status_code == 200
    assert omci_calls == [(db, "ont-1", None)]
    assert tr069_calls == []


def test_ont_reboot_default_uses_tr069_transport(monkeypatch):
    """Plain soft reboot uses ACS/TR-069 when that transport succeeds."""
    from app.web.admin import network_onts_actions

    omci_calls: list[tuple[object, str]] = []
    tr069_calls: list[tuple[object, str]] = []

    def fake_omci_reboot(db, ont_id, *, initiated_by=None):
        omci_calls.append((db, ont_id))
        return True, "ONT reboot command sent via OLT."

    def fake_tr069_reboot(db, ont_id, *, request=None):
        tr069_calls.append((db, ont_id))
        return ActionResult(True, "TR-069 reboot sent")

    monkeypatch.setattr(
        network_onts_actions,
        "_ensure_ont_write_scope",
        lambda *args: None,
    )
    monkeypatch.setattr(
        network_onts_actions.web_network_ont_actions_service,
        "execute_omci_reboot",
        fake_omci_reboot,
    )
    monkeypatch.setattr(
        network_onts_actions.web_network_ont_actions_service,
        "execute_reboot",
        fake_tr069_reboot,
    )

    db = object()
    response = network_onts_actions.ont_reboot(
        _request_for_action(),
        "ont-1",
        db=db,
    )

    assert response.status_code == 200
    assert tr069_calls == [(db, "ont-1")]
    assert omci_calls == []


def test_ont_reboot_default_falls_back_to_omci_when_tr069_fails(monkeypatch):
    """The UI reboot action should still work when ACS reboot cannot be delivered."""
    from app.web.admin import network_onts_actions

    omci_calls: list[tuple[object, str, object]] = []
    tr069_calls: list[tuple[object, str]] = []

    def fake_omci_reboot(db, ont_id, *, initiated_by=None):
        omci_calls.append((db, ont_id, initiated_by))
        return True, "ONT reboot command sent via OLT."

    def fake_tr069_reboot(db, ont_id, *, request=None):
        tr069_calls.append((db, ont_id))
        return ActionResult(
            False, "Connection request error: Unexpected status code 401"
        )

    monkeypatch.setattr(
        network_onts_actions,
        "_ensure_ont_write_scope",
        lambda *args: None,
    )
    monkeypatch.setattr(
        network_onts_actions.web_network_ont_actions_service,
        "execute_omci_reboot",
        fake_omci_reboot,
    )
    monkeypatch.setattr(
        network_onts_actions.web_network_ont_actions_service,
        "execute_reboot",
        fake_tr069_reboot,
    )

    db = object()
    response = network_onts_actions.ont_reboot(
        _request_for_action(),
        "ont-1",
        db=db,
    )

    assert response.status_code == 200
    assert tr069_calls == [(db, "ont-1")]
    assert omci_calls == [(db, "ont-1", None)]
