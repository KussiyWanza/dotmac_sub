"""Tests for ONT write service (Phase 5)."""

from __future__ import annotations

import importlib
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.network.olt_dependency_preflight import OltDependencyPreflightResult
from app.services.network.ont_write import OntWriteService
from app.services.network.provisioning_events import provisioning_correlation

FAKE_UUID = str(uuid.uuid4())
FAKE_UUID2 = str(uuid.uuid4())


def _config_pack(tag: int = 203) -> SimpleNamespace:
    return SimpleNamespace(management_vlan=SimpleNamespace(tag=tag))


@pytest.fixture(autouse=True)
def _pass_olt_dependency_preflight(monkeypatch):
    monkeypatch.setattr(
        "app.services.network.olt_dependency_preflight.validate_olt_profile_dependencies",
        lambda *args, **kwargs: OltDependencyPreflightResult(
            success=True,
            message="OLT profile dependencies are valid.",
            audit={"is_valid": True},
        ),
    )


class TestUpdateSpeedProfile:
    """Speed profile update tests."""

    def test_ont_not_found(self):
        db = MagicMock()
        db.get.return_value = None
        result = OntWriteService.update_speed_profile(
            db, "missing-id", download_profile_id=FAKE_UUID
        )
        assert result.success is False
        assert "not found" in result.message.lower()

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_updates_download_profile(self, mock_get, mock_emit):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        db = MagicMock()
        result = OntWriteService.update_speed_profile(
            db, "ont-1", download_profile_id=FAKE_UUID
        )
        assert result.success is True
        db.commit.assert_called()


class TestUpdateExternalId:
    """External ID update tests."""

    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_updates_external_id(self, mock_get):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        db = MagicMock()
        result = OntWriteService.update_external_id(db, "ont-1", external_id="42")
        assert result.success is True
        assert ont.external_id == "42"
        db.commit.assert_called()

    def test_ont_not_found(self):
        db = MagicMock()
        db.get.return_value = None
        result = OntWriteService.update_external_id(db, "missing", external_id="42")
        assert result.success is False


class TestUpdateManagementIp:
    @patch("app.services.network.ont_write.resolve_olt_config_pack")
    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_writes_management_intent_to_overrides(
        self,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
        mock_config_pack,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        olt = MagicMock(id=uuid.uuid4())
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )
        mock_adapter = MagicMock()
        mock_adapter.configure_iphost.return_value = OltOperationResult(
            success=True, message="ok", data={}
        )
        mock_get_adapter.return_value = mock_adapter
        mock_config_pack.return_value = _config_pack()
        db = MagicMock()
        db.get.return_value = MagicMock(id=uuid.uuid4(), tag=203, olt_device_id=olt.id)

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
            mgmt_ip_address="172.16.201.10",
        )

        assert result.success is True
        assert db.commit.called

    @patch("app.services.network.ont_write.resolve_olt_config_pack")
    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    @patch("app.services.network.cpe.Vlans.get")
    def test_accepts_huawei_dotted_external_id(
        self,
        mock_vlan_get,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
        mock_config_pack,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="huawei:4194320640.5")
        mock_get.return_value = (ont, None)
        olt = MagicMock(id=uuid.uuid4())
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )
        mock_vlan_get.return_value = MagicMock(tag=203)

        mock_adapter = MagicMock()
        mock_adapter.configure_iphost.return_value = OltOperationResult(
            success=True, message="ok", data={}
        )
        mock_get_adapter.return_value = mock_adapter
        mock_config_pack.return_value = _config_pack()
        db = MagicMock()
        db.get.return_value = MagicMock(id=uuid.uuid4(), tag=203, olt_device_id=olt.id)

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
        )

        assert result.success is True
        mock_adapter.configure_iphost.assert_called_once()
        # Verify ont_id is passed correctly
        assert mock_adapter.configure_iphost.call_args.args[1] == 5

    @patch("app.services.network.ont_write.resolve_olt_config_pack")
    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    @patch("app.services.network.cpe.Vlans.get")
    def test_uses_scanned_board_port_context(
        self,
        mock_vlan_get,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
        mock_config_pack,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        olt = MagicMock(id=uuid.uuid4())
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )
        mock_vlan_get.return_value = MagicMock(tag=203)

        mock_adapter = MagicMock()
        mock_adapter.configure_iphost.return_value = OltOperationResult(
            success=True, message="ok", data={}
        )
        mock_get_adapter.return_value = mock_adapter
        mock_config_pack.return_value = _config_pack()
        db = MagicMock()
        db.get.return_value = MagicMock(id=uuid.uuid4(), tag=203, olt_device_id=olt.id)

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
        )

        assert result.success is True
        # Verify fsp and ont_id are passed correctly
        assert mock_adapter.configure_iphost.call_args.args[0] == "0/1/3"
        assert mock_adapter.configure_iphost.call_args.args[1] == 5

    @patch("app.services.network.ont_write.resolve_olt_config_pack")
    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_accepts_management_vlan_tag_without_vlan_row(
        self,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
        mock_config_pack,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        olt = MagicMock(id=uuid.uuid4())
        mock_resolve_context.return_value = (
            SimpleNamespace(
                olt=olt,
                fsp="0/1/3",
                ont_id_on_olt=5,
            ),
            None,
        )

        mock_adapter = MagicMock()
        mock_adapter.configure_iphost.return_value = OltOperationResult(
            success=True, message="ok", data={}
        )
        mock_get_adapter.return_value = mock_adapter
        mock_config_pack.return_value = _config_pack(201)
        db = MagicMock()
        db.scalars.return_value.first.return_value = None

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
        )

        assert result.success is True
        # Verify vlan is passed correctly in kwargs
        assert mock_adapter.configure_iphost.call_args.kwargs["vlan"] == 201
        assert "mgmt_vlan_id" not in ont.__dict__

    @patch("app.services.network.ont_write.resolve_olt_config_pack")
    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    @patch("app.services.network.cpe.Vlans.get")
    def test_ssh_timeout_does_not_persist_management_ip(
        self,
        mock_vlan_get,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
        mock_config_pack,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        olt = MagicMock(id=uuid.uuid4())
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )
        mock_vlan_get.return_value = MagicMock(tag=203)

        mock_adapter = MagicMock()
        mock_adapter.configure_iphost.return_value = OltOperationResult(
            success=False,
            message="SSH IPHOST configuration failed: timed out",
            data={},
            error_code="TimeoutError",
        )
        mock_get_adapter.return_value = mock_adapter
        mock_config_pack.return_value = _config_pack()
        db = MagicMock()
        db.get.return_value = MagicMock(id=uuid.uuid4(), tag=203, olt_device_id=olt.id)

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
        )

        assert result.success is False
        assert "timed out" in result.message
        db.commit.assert_not_called()
        mock_emit.assert_not_called()

    @patch("app.services.network.ont_write.resolve_olt_config_pack")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    @patch(
        "app.services.network.olt_dependency_preflight.validate_olt_profile_dependencies"
    )
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_management_ip_fails_before_adapter_when_dependencies_invalid(
        self,
        mock_get,
        mock_resolve_context,
        mock_preflight,
        mock_get_adapter,
        mock_config_pack,
    ):
        olt_id = uuid.uuid4()
        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        mock_resolve_context.return_value = (
            SimpleNamespace(
                olt=SimpleNamespace(id=olt_id), fsp="0/1/3", ont_id_on_olt=5
            ),
            None,
        )
        mock_preflight.return_value = OltDependencyPreflightResult(
            success=False,
            message="OLT management IP config dependency audit failed: missing TR-069 profile(s): 1",
            audit={"is_valid": False},
            errors=["missing TR-069 profile(s): 1"],
        )
        db = MagicMock()

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
        )

        assert result.success is False
        assert "dependency audit failed" in result.message
        mock_get_adapter.assert_not_called()
        mock_config_pack.assert_not_called()

    @patch("app.services.network.ont_write.resolve_olt_config_pack")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_rejects_global_management_vlan_id(
        self,
        mock_get,
        mock_resolve_context,
        mock_config_pack,
    ):
        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        olt = MagicMock(id=uuid.uuid4())
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )
        vlan = MagicMock(id=uuid.uuid4(), tag=203, olt_device_id=None)
        db = MagicMock()
        db.get.return_value = vlan
        mock_config_pack.return_value = SimpleNamespace(management_vlan=None)

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
        )

        assert result.success is False
        assert "config pack" in result.message.lower()
        db.commit.assert_not_called()


class TestUpdateServicePort:
    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_accepts_huawei_dotted_external_id(
        self,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="huawei:4194320640.5")
        mock_get.return_value = (ont, None)
        olt = MagicMock()
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )

        mock_adapter = MagicMock()
        mock_adapter.create_service_port.return_value = OltOperationResult(
            success=True, message="created", data={"port_index": 700}
        )
        mock_get_adapter.return_value = mock_adapter
        db = MagicMock()

        result = OntWriteService.update_service_port(
            db,
            "ont-1",
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is True
        mock_adapter.create_service_port.assert_called_once()
        # Verify ont_id is passed correctly
        assert mock_adapter.create_service_port.call_args.args[1] == 5

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_uses_scanned_board_port_context_and_generic_external_id(
        self,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        olt = MagicMock()
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )

        mock_adapter = MagicMock()
        mock_adapter.create_service_port.return_value = OltOperationResult(
            success=True, message="created", data={"port_index": 700}
        )
        mock_get_adapter.return_value = mock_adapter
        db = MagicMock()

        result = OntWriteService.update_service_port(
            db,
            "ont-1",
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is True
        # Verify fsp and ont_id are passed correctly
        assert mock_adapter.create_service_port.call_args.args[0] == "0/1/3"
        assert mock_adapter.create_service_port.call_args.args[1] == 5

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_ssh_timeout_does_not_persist_service_port(
        self,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        olt = MagicMock()
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )

        mock_adapter = MagicMock()
        mock_adapter.create_service_port.return_value = OltOperationResult(
            success=False,
            message="SSH service port creation failed: timed out",
            data={},
            error_code="TimeoutError",
        )
        mock_get_adapter.return_value = mock_adapter
        db = MagicMock()

        result = OntWriteService.update_service_port(
            db,
            "ont-1",
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is False
        assert "timed out" in result.message
        db.commit.assert_not_called()
        mock_emit.assert_not_called()

    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    @patch(
        "app.services.network.olt_dependency_preflight.validate_olt_profile_dependencies"
    )
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_service_port_fails_before_adapter_when_dependencies_invalid(
        self,
        mock_get,
        mock_resolve_context,
        mock_preflight,
        mock_get_adapter,
    ):
        olt_id = uuid.uuid4()
        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        mock_resolve_context.return_value = (
            SimpleNamespace(
                olt=SimpleNamespace(id=olt_id), fsp="0/1/3", ont_id_on_olt=5
            ),
            None,
        )
        mock_preflight.return_value = OltDependencyPreflightResult(
            success=False,
            message="OLT service-port update dependency audit failed: missing WAN config profile(s): 0",
            audit={"is_valid": False},
            errors=["missing WAN config profile(s): 0"],
        )
        db = MagicMock()

        result = OntWriteService.update_service_port(
            db,
            "ont-1",
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is False
        assert "dependency audit failed" in result.message
        mock_get_adapter.assert_not_called()

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_db_backed_path_allocates_index_before_olt_write(
        self,
        mock_get_adapter,
        mock_emit,
        db_session,
    ):
        from sqlalchemy import select

        from app.models.network import (
            OLTDevice,
            OntAssignment,
            OntUnit,
            PonPort,
            ServicePortAllocation,
        )
        from app.services.network.olt_protocol_adapters import OltOperationResult

        olt = OLTDevice(name="Write Service OLT", vendor="Huawei")
        pon = PonPort(olt=olt, name="0/1/3")
        ont = OntUnit(
            serial_number="WRITE-SP-OK",
            board="0/1",
            port="3",
            external_id="5",
        )
        db_session.add_all([olt, pon, ont])
        db_session.flush()
        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.create_service_port.return_value = OltOperationResult(
            success=True, message="created", data={}
        )
        mock_adapter.get_service_ports_for_ont.return_value = OltOperationResult(
            success=True,
            message="ok",
            data={
                "service_ports": [
                    SimpleNamespace(
                        index=321,
                        vlan_id=203,
                        gem_index=1,
                        tag_transform="translate",
                    )
                ]
            },
        )
        mock_get_adapter.return_value = mock_adapter

        result = OntWriteService.update_service_port(
            db_session,
            str(ont.id),
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is True
        port_index = mock_adapter.create_service_port.call_args.kwargs["port_index"]
        assert port_index is not None
        allocation = db_session.scalars(select(ServicePortAllocation)).one()
        assert allocation.port_index == port_index
        assert allocation.vlan_id == 203
        assert allocation.gem_index == 1
        assert allocation.is_active is True
        assert allocation.provisioned_at is not None
        assert allocation.released_at is None
        mock_emit.assert_called_once()

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_db_backed_path_releases_allocation_when_olt_write_fails(
        self,
        mock_get_adapter,
        mock_emit,
        db_session,
    ):
        from sqlalchemy import select

        from app.models.network import (
            OLTDevice,
            OntAssignment,
            OntUnit,
            PonPort,
            ServicePortAllocation,
        )
        from app.services.network.olt_protocol_adapters import OltOperationResult

        olt = OLTDevice(name="Write Service Fail OLT", vendor="Huawei")
        pon = PonPort(olt=olt, name="0/1/3")
        ont = OntUnit(
            serial_number="WRITE-SP-FAIL",
            board="0/1",
            port="3",
            external_id="5",
        )
        db_session.add_all([olt, pon, ont])
        db_session.flush()
        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.create_service_port.return_value = OltOperationResult(
            success=False,
            message="SSH service port creation failed: timed out",
            data={},
            error_code="TimeoutError",
        )
        mock_get_adapter.return_value = mock_adapter

        result = OntWriteService.update_service_port(
            db_session,
            str(ont.id),
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is False
        assert "timed out" in result.message
        allocation = db_session.scalars(select(ServicePortAllocation)).one()
        assert allocation.is_active is False
        assert allocation.provisioned_at is None
        assert allocation.released_at is not None
        mock_emit.assert_not_called()

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_db_backed_path_replays_cached_result_for_same_correlation_key(
        self,
        mock_get_adapter,
        mock_emit,
        db_session,
    ):
        from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
        from app.services.network.olt_protocol_adapters import OltOperationResult

        olt = OLTDevice(name="Write Service Replay OLT", vendor="Huawei")
        pon = PonPort(olt=olt, name="0/1/3")
        ont = OntUnit(
            serial_number="WRITE-SP-REPLAY",
            board="0/1",
            port="3",
            external_id="5",
        )
        db_session.add_all([olt, pon, ont])
        db_session.flush()
        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.create_service_port.side_effect = [
            OltOperationResult(
                success=True, message="created", data={"port_index": 777}
            ),
            OltOperationResult(
                success=True, message="created", data={"port_index": 778}
            ),
        ]
        mock_adapter.get_service_ports_for_ont.return_value = OltOperationResult(
            success=True,
            message="ok",
            data={
                "service_ports": [
                    SimpleNamespace(
                        index=777,
                        vlan_id=203,
                        gem_index=1,
                        tag_transform="translate",
                    ),
                    SimpleNamespace(
                        index=778,
                        vlan_id=204,
                        gem_index=1,
                        tag_transform="translate",
                    ),
                ]
            },
        )
        mock_get_adapter.return_value = mock_adapter

        with provisioning_correlation("svc-port:replay:1"):
            first = OntWriteService.update_service_port(
                db_session,
                str(ont.id),
                vlan_id=203,
                gem_index=1,
            )
            third = OntWriteService.update_service_port(
                db_session,
                str(ont.id),
                vlan_id=204,
                gem_index=1,
            )
            second = OntWriteService.update_service_port(
                db_session,
                str(ont.id),
                vlan_id=203,
                gem_index=1,
            )

        assert first.success is True
        assert second.success is True
        assert third.success is True
        assert first.data == second.data
        assert first.data != third.data
        assert mock_adapter.create_service_port.call_count == 2


class TestOntOltWriteContext:
    def test_requires_scanned_board_port_not_pon_name_fallback(self, db_session):
        from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
        from app.services.network.ont_olt_context import resolve_ont_olt_write_context

        olt = OLTDevice(name="Strict Context OLT", vendor="Huawei")
        db_session.add(olt)
        db_session.flush()

        pon = PonPort(olt_id=olt.id, name="0/1/3")
        ont = OntUnit(serial_number="STRICT-CONTEXT-1", external_id="5")
        db_session.add_all([pon, ont])
        db_session.flush()
        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.commit()

        ctx, message = resolve_ont_olt_write_context(db_session, str(ont.id))

        assert ctx is None
        assert message is not None
        assert "scanned board/port" in message

    def test_resolves_from_scanned_board_port_and_ont_id(self, db_session):
        from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
        from app.services.network.ont_olt_context import resolve_ont_olt_write_context

        olt = OLTDevice(name="Strict Context OLT", vendor="Huawei")
        db_session.add(olt)
        db_session.flush()

        pon = PonPort(olt_id=olt.id, name="display-only-name")
        ont = OntUnit(
            serial_number="STRICT-CONTEXT-2",
            board="0/1",
            port="3",
            external_id="huawei:4194320640.5",
        )
        db_session.add_all([pon, ont])
        db_session.flush()
        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.commit()

        ctx, message = resolve_ont_olt_write_context(db_session, str(ont.id))

        assert message is None
        assert ctx is not None
        assert ctx.fsp == "0/1/3"
        assert ctx.ont_id_on_olt == 5
        assert ctx.olt.id == olt.id

    def test_queries_locked_assignment_instead_of_relationship_cache(self):
        from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
        from app.services.network.ont_olt_context import resolve_ont_olt_write_context

        ont = OntUnit(
            id=uuid.uuid4(),
            serial_number="STRICT-CONTEXT-LOCK",
            board="0/1",
            port="3",
            external_id="huawei:4194320640.5",
        )
        ont.assignments = []
        assignment = OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=uuid.uuid4(),
            active=True,
        )
        pon = PonPort(id=assignment.pon_port_id, olt_id=uuid.uuid4(), name="0/1/3")
        olt = OLTDevice(id=pon.olt_id, name="Strict Context OLT", vendor="Huawei")

        db = MagicMock()

        def _get(model, value):
            if model is OntUnit:
                return ont
            if model is PonPort:
                return pon
            if model is OLTDevice:
                return olt
            return None

        db.get.side_effect = _get
        db.scalars.return_value.first.return_value = assignment

        ctx, message = resolve_ont_olt_write_context(db, str(ont.id))

        assert message is None
        assert ctx is not None
        assert ctx.assignment is assignment
        statement = db.scalars.call_args.args[0]
        assert getattr(statement, "_for_update_arg", None) is not None


class TestMoveOnt:
    """ONT move tests."""

    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_target_port_not_found(self, mock_get):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        db = MagicMock()
        db.get.return_value = None  # port not found
        result = OntWriteService.move_ont(db, "ont-1", target_pon_port_id=FAKE_UUID)
        assert result.success is False
        assert "not found" in result.message.lower()

    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_db_only_move_is_retired(self, mock_get):
        ont = MagicMock()
        ont.id = "ont-1"
        ont.olt_device_id = "old-olt"
        mock_get.return_value = (ont, None)

        target_port = MagicMock()
        target_port.id = "pon-1"
        target_port.olt_id = "new-olt"

        current_assignment = MagicMock()
        current_assignment.subscriber_id = "sub-1"

        scalar_result = MagicMock()
        scalar_result.first.return_value = current_assignment

        db = MagicMock()
        db.get.return_value = target_port
        db.scalars.return_value = scalar_result

        result = OntWriteService.move_ont(
            db,
            "ont-1",
            target_pon_port_id=FAKE_UUID,
            skip_device_ops=True,
        )

        assert result.success is False
        assert "DB-only ONT moves are retired" in result.message
        assert ont.olt_device_id == "old-olt"
        db.commit.assert_not_called()

    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    @patch(
        "app.services.network.olt_dependency_preflight.validate_olt_profile_dependencies"
    )
    @patch("app.services.network.ont_write._strict_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_move_fails_before_adapter_when_dependencies_invalid(
        self,
        mock_get,
        mock_context,
        mock_preflight,
        mock_get_adapter,
    ):
        olt_id = uuid.uuid4()
        ont = MagicMock()
        ont.id = "ont-1"
        mock_get.return_value = (ont, None)

        target_port = MagicMock()
        target_port.id = "pon-1"
        target_port.olt_id = olt_id
        target_port.name = "0/2/13"

        current_assignment = MagicMock()
        current_assignment.subscriber_id = "sub-1"

        db = MagicMock()
        db.get.return_value = target_port
        db.scalars.return_value.first.return_value = current_assignment

        mock_context.return_value = (
            SimpleNamespace(
                olt=SimpleNamespace(id=olt_id),
                fsp="0/2/11",
                ont_id_on_olt=13,
                line_profile_id=10,
                service_profile_id=20,
            ),
            None,
        )
        mock_preflight.return_value = OltDependencyPreflightResult(
            success=False,
            message="OLT ONT move dependency audit failed: missing WAN config profile(s): 0",
            audit={"is_valid": False},
            errors=["missing WAN config profile(s): 0"],
        )

        result = OntWriteService.move_ont(db, "ont-1", target_pon_port_id=FAKE_UUID)

        assert result.success is False
        assert "dependency audit failed" in result.message
        assert result.data == {"dependency_audit": {"is_valid": False}}
        mock_get_adapter.assert_not_called()

    def test_resolve_olt_context_prefers_assignment_pon_port_olt(self):
        from app.services.network.ont_write import _resolve_olt_context

        assignment_olt = MagicMock()
        assignment = MagicMock()
        assignment.pon_port = MagicMock()
        assignment.pon_port.olt = assignment_olt

        ont = MagicMock()
        ont.id = "ont-1"
        ont.olt_device = MagicMock()

        scalar_result = MagicMock()
        scalar_result.first.return_value = assignment
        db = MagicMock()
        db.scalars.return_value = scalar_result

        olt, resolved_assignment, error = _resolve_olt_context(db, ont)

        assert error is None
        assert resolved_assignment is assignment
        assert olt is assignment_olt


class TestMoveOntDeviceSafety:
    """The move is device-first, so it must be device-safe.

    Step 1 deletes a working subscriber ONT from the OLT. Two things made that
    unsafe: the target F/S/P was taken verbatim from ``PonPort.name`` (which
    carries prefixes like ``gpon-0/2/13``, so step 2 built
    ``interface gpon gpon-0/2``), and the deauthorize step carried no
    ``rollback_fn`` — so ``all_or_nothing=True`` did not restore the ONT.
    """

    @staticmethod
    def _harness(monkeypatch, target_name: str, *, authorize_ok: bool):
        olt_id = uuid.uuid4()
        ont = MagicMock()
        ont.id = "ont-1"
        ont.serial_number = "48575443A31A3529"

        target_port = MagicMock()
        target_port.id = "pon-1"
        target_port.olt_id = olt_id
        target_port.name = target_name

        db = MagicMock()
        db.get.return_value = target_port
        db.scalars.return_value.first.return_value = MagicMock(subscriber_id="sub-1")

        calls: list[tuple[str, tuple, dict]] = []

        class _Adapter:
            def deauthorize_ont(self, *args, **kwargs):
                calls.append(("deauthorize_ont", args, kwargs))
                return SimpleNamespace(success=True, message="deleted", ont_id=None)

            def authorize_ont(self, *args, **kwargs):
                calls.append(("authorize_ont", args, kwargs))
                return SimpleNamespace(
                    success=authorize_ok,
                    message="authorized" if authorize_ok else "OLT rejected: Failure",
                    ont_id=7 if authorize_ok else None,
                )

            def create_service_port(self, *args, **kwargs):
                calls.append(("create_service_port", args, kwargs))
                return SimpleNamespace(success=True, message="created", ont_id=None)

            def get_service_ports_for_ont(self, *args, **kwargs):
                return SimpleNamespace(success=True, message="", data={})

        # ``app.services.network.ont_write`` is shadowed by a service instance
        # on the package, so resolve the real modules explicitly.
        ont_write_mod = sys.modules["app.services.network.ont_write"]
        adapters_mod = sys.modules["app.services.network.olt_protocol_adapters"]
        imported_mod = importlib.import_module(
            "app.services.network.imported_service_ports"
        )

        monkeypatch.setattr(
            ont_write_mod, "get_ont_or_error", lambda *a, **k: (ont, None)
        )
        monkeypatch.setattr(
            ont_write_mod,
            "_strict_olt_write_context",
            lambda *a, **k: (
                SimpleNamespace(
                    olt=SimpleNamespace(id=olt_id),
                    fsp="0/2/11",
                    ont_id_on_olt=13,
                    line_profile_id=10,
                    service_profile_id=20,
                ),
                None,
            ),
        )
        monkeypatch.setattr(
            adapters_mod, "get_protocol_adapter", lambda *a, **k: _Adapter()
        )
        monkeypatch.setattr(
            imported_mod, "require_imported_service_port_state", lambda *a, **k: None
        )
        monkeypatch.setattr(
            imported_mod, "list_imported_service_ports", lambda *a, **k: []
        )
        return db, calls

    def test_prefixed_pon_port_name_is_canonicalized_before_the_olt_command(
        self, monkeypatch
    ):
        db, calls = self._harness(monkeypatch, "gpon-0/2/13", authorize_ok=True)

        OntWriteService.move_ont(db, "ont-1", target_pon_port_id=FAKE_UUID)

        authorize = next(call for call in calls if call[0] == "authorize_ont")
        assert authorize[1][0] == "0/2/13"

    def test_unusable_pon_port_name_fails_before_any_device_write(self, monkeypatch):
        db, calls = self._harness(monkeypatch, "uplink-A", authorize_ok=True)

        result = OntWriteService.move_ont(db, "ont-1", target_pon_port_id=FAKE_UUID)

        assert result.success is False
        assert "Frame/Slot/Port" in result.message
        assert calls == []

    def test_failed_authorize_restores_the_ont_on_the_source_port(self, monkeypatch):
        db, calls = self._harness(monkeypatch, "0/2/13", authorize_ok=False)

        result = OntWriteService.move_ont(db, "ont-1", target_pon_port_id=FAKE_UUID)

        assert result.success is False
        authorizes = [call for call in calls if call[0] == "authorize_ont"]
        assert len(authorizes) == 2, "the source-port restore did not run"
        assert authorizes[0][1][0] == "0/2/13"
        assert authorizes[1][1][0] == "0/2/11", "ONT was left deauthorized"
        assert authorizes[1][1][1] == "48575443A31A3529"
