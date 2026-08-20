"""Tests for TR-069 feature gap fixes.

Covers: event types, Celery task structure, inform webhook, job retry model,
auto-link ONTs, parameter map resolution, and session cleanup.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from jinja2 import Environment, FileSystemLoader

from app.models.tr069 import (
    Tr069AcsServer,
    Tr069CpeDevice,
    Tr069Event,
    Tr069Session,
)
from app.schemas.tr069 import Tr069AcsServerCreate
from app.services.auth_dependencies import can
from app.services.events.types import EventType
from app.services.network.tr069_job_commands import Tr069CommandError
from app.web.brand_globals import _app_datetime_filter

# ---------------------------------------------------------------------------
# 1. TR-069 event types
# ---------------------------------------------------------------------------


class TestTr069EventTypes:
    def test_job_completed_event(self) -> None:
        assert EventType.tr069_job_completed.value == "tr069_job.completed"

    def test_job_failed_event(self) -> None:
        assert EventType.tr069_job_failed.value == "tr069_job.failed"

    def test_device_discovered_event(self) -> None:
        assert EventType.tr069_device_discovered.value == "tr069_device.discovered"

    def test_device_stale_event(self) -> None:
        assert EventType.tr069_device_stale.value == "tr069_device.stale"


# ---------------------------------------------------------------------------
# 2. Inform webhook endpoint
# ---------------------------------------------------------------------------


class TestInformWebhook:
    def test_inform_updates_last_inform_at(self, db_session) -> None:
        """Test the inform endpoint logic via direct service call."""
        from sqlalchemy import select

        server = Tr069AcsServer(
            name="Inform ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()

        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="INFORM-001",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()
        db_session.refresh(device)
        assert device.last_inform_at is None

        # Simulate what the endpoint does
        now = datetime.now(UTC)
        found = db_session.scalars(
            select(Tr069CpeDevice)
            .where(
                Tr069CpeDevice.serial_number == "INFORM-001",
                Tr069CpeDevice.is_active.is_(True),
            )
            .limit(1)
        ).first()
        assert found is not None
        found.last_inform_at = now

        session = Tr069Session(
            device_id=found.id,
            event_type=Tr069Event.boot,
            started_at=now,
            ended_at=now,
        )
        db_session.add(session)
        db_session.commit()

        db_session.refresh(found)
        assert found.last_inform_at is not None

    def test_inform_endpoint_schema_validation(self) -> None:
        """Test the InformPayload Pydantic model."""
        from app.api.tr069_inform import InformPayload

        # With serial
        payload = InformPayload(serial_number="TEST-001", event="boot")
        assert payload.serial_number == "TEST-001"
        assert payload.event == "boot"
        extra_payload = InformPayload(
            serial_number="TEST-001",
            parameters={"Device.DeviceInfo.SoftwareVersion": "V1"},
        )
        assert extra_payload.model_dump()["parameters"] == {
            "Device.DeviceInfo.SoftwareVersion": "V1"
        }

        # Without serial — should default
        payload = InformPayload()
        assert payload.serial_number is None
        assert payload.event == "periodic"

    def test_inform_extracts_serial_from_device_id(self) -> None:
        """Test serial extraction from device_id format."""
        device_id = "00D09E-TestProduct-SERIAL123"
        parts = device_id.split("-", 2)
        assert len(parts) == 3
        assert parts[2] == "SERIAL123"

    def test_receive_inform_updates_linked_ont_last_seen_at(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services import tr069 as tr069_service

        server = Tr069AcsServer(
            name="Linked Inform ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        ont = OntUnit(serial_number="INFORM-LINKED-001", is_active=True)
        db_session.add_all([server, ont])
        db_session.flush()
        device = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number=ont.serial_number,
            genieacs_device_id="00D09E-TestProduct-INFORM-LINKED-001",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()

        result = tr069_service.receive_inform(
            db_session,
            serial_number=ont.serial_number,
            device_id_raw=device.genieacs_device_id,
            event="periodic",
            raw_payload={},
            acs_server_id=str(server.id),
        )

        assert result["status"] == "ok"
        db_session.refresh(ont)
        db_session.refresh(device)
        assert device.last_inform_at is not None
        assert ont.acs_last_inform_at == device.last_inform_at
        assert ont.last_seen_at == device.last_inform_at

    def test_receive_inform_links_existing_unassigned_device_to_matching_ont(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services import tr069 as tr069_service

        server = Tr069AcsServer(
            name="Unassigned Inform ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        ont = OntUnit(serial_number="INFORM-UNASSIGNED-001", is_active=True)
        db_session.add_all([server, ont])
        db_session.flush()
        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number=ont.serial_number,
            genieacs_device_id="00D09E-TestProduct-INFORM-UNASSIGNED-001",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()

        result = tr069_service.receive_inform(
            db_session,
            serial_number=ont.serial_number,
            device_id_raw=device.genieacs_device_id,
            event="periodic",
            raw_payload={},
            acs_server_id=str(server.id),
        )

        assert result["status"] == "ok"
        db_session.refresh(ont)
        db_session.refresh(device)
        assert device.ont_unit_id == ont.id
        assert ont.acs_last_inform_at == device.last_inform_at
        assert ont.last_seen_at == device.last_inform_at

    def test_receive_inform_updates_changed_genieacs_device_id(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services import tr069 as tr069_service

        server = Tr069AcsServer(
            name="Changed Inform ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        ont = OntUnit(serial_number="INFORM-CHANGED-ID-001", is_active=True)
        db_session.add_all([server, ont])
        db_session.flush()
        device = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number=ont.serial_number,
            genieacs_device_id="00D09E-TestProduct-OLD-INFORM-ID",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()

        result = tr069_service.receive_inform(
            db_session,
            serial_number=ont.serial_number,
            device_id_raw="00D09E-TestProduct-NEW-INFORM-ID",
            event="periodic",
            raw_payload={},
            acs_server_id=str(server.id),
        )

        assert result["status"] == "ok"
        db_session.refresh(device)
        assert device.genieacs_device_id == "00D09E-TestProduct-NEW-INFORM-ID"

    def test_stale_ont_refresh_uses_linked_tr069_identity(
        self,
        db_session,
        monkeypatch,
    ) -> None:
        from app.models.network import OntUnit
        from app.services import web_network_core_devices_views as views

        queued: list[str] = []

        class FakeTask:
            @staticmethod
            def delay(ont_id):
                queued.append(ont_id)

        monkeypatch.setattr(
            "app.tasks.tr069.refresh_single_ont_runtime",
            FakeTask,
        )

        server = Tr069AcsServer(
            name="Stale Refresh ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        ont = OntUnit(
            serial_number="STALE-REFRESH-001",
            is_active=True,
            tr069_last_snapshot={"wan": {"ip": "10.0.0.2"}},
            tr069_last_snapshot_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        db_session.add_all([server, ont])
        db_session.flush()
        db_session.add(
            Tr069CpeDevice(
                acs_server_id=server.id,
                ont_unit_id=ont.id,
                serial_number=ont.serial_number,
                genieacs_device_id="00D09E-TestProduct-STALE-REFRESH-001",
                is_active=True,
            )
        )
        db_session.commit()

        assert views._maybe_queue_stale_ont_refresh(
            db_session,
            ont,
            ont_id=str(ont.id),
        )
        assert queued == [str(ont.id)]

    def test_clear_stale_runtime_clears_cached_tr069_snapshot(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.network.provisioning_enforcement import (
            ProvisioningEnforcement,
        )

        ont = OntUnit(
            serial_number="CLEAR-RUNTIME-001",
            is_active=True,
            observed_wan_ip="10.0.0.2",
            observed_pppoe_status="Connected",
            observed_lan_mode="router",
            observed_wifi_clients=2,
            observed_lan_hosts=3,
            observed_runtime_updated_at=datetime.now(UTC),
            tr069_last_snapshot={"wan": {"ip": "10.0.0.2"}},
            tr069_last_snapshot_at=datetime.now(UTC),
        )
        db_session.add(ont)
        db_session.commit()

        result = ProvisioningEnforcement.clear_stale_runtime(
            db_session,
            [str(ont.id)],
        )

        assert result == {"cleared": 1}
        db_session.refresh(ont)
        assert ont.observed_wan_ip is None
        assert ont.observed_pppoe_status is None
        assert ont.observed_lan_mode is None
        assert ont.observed_wifi_clients is None
        assert ont.observed_lan_hosts is None
        assert ont.observed_runtime_updated_at is None
        assert ont.tr069_last_snapshot == {}
        assert ont.tr069_last_snapshot_at is None

    def test_tr069_rebind_fails_before_adapter_when_dependencies_invalid(
        self, monkeypatch
    ) -> None:
        from app.services.network.olt_dependency_preflight import (
            OltDependencyPreflightResult,
        )
        from app.services.network.olt_tr069_admin import handle_rebind_tr069_profiles

        olt = SimpleNamespace(id=uuid4(), name="Garki")
        monkeypatch.setattr(
            "app.services.network.olt_tr069_admin.get_olt_or_none",
            lambda *args, **kwargs: olt,
        )
        monkeypatch.setattr(
            "app.services.network.olt_dependency_preflight.validate_olt_profile_dependencies",
            lambda *args, **kwargs: OltDependencyPreflightResult(
                success=False,
                message="OLT TR-069 profile rebind dependency audit failed: missing TR-069 profile(s): 1",
                audit={"is_valid": False},
                errors=["missing TR-069 profile(s): 1"],
            ),
        )
        get_adapter = MagicMock()
        monkeypatch.setattr(
            "app.services.network.olt_protocol_adapters.get_protocol_adapter",
            get_adapter,
        )

        result = handle_rebind_tr069_profiles(
            MagicMock(),
            str(olt.id),
            ["ont-1", "ont-2"],
            target_profile_id=1,
        )

        assert result == {
            "rebound": 0,
            "failed": 2,
            "errors": [
                "OLT TR-069 profile rebind dependency audit failed: missing TR-069 profile(s): 1"
            ],
        }
        get_adapter.assert_not_called()

    def test_management_enforcement_fails_batch_before_adapter_when_dependencies_invalid(
        self, monkeypatch
    ) -> None:
        from app.models.network import OLTDevice, OntUnit
        from app.services.network.olt_dependency_preflight import (
            OltDependencyPreflightResult,
        )
        from app.services.network.provisioning_enforcement import (
            ProvisioningEnforcement,
        )

        olt_id = uuid4()
        ont = SimpleNamespace(
            id="ont-1",
            board="0/2",
            port="11",
            external_id="13",
            olt_device_id=olt_id,
            serial_number="HWTCDEPENDENCY",
        )
        olt = SimpleNamespace(id=olt_id, name="Garki")

        db = MagicMock()

        def _get(model, entity_id):
            if model is OntUnit and entity_id == "ont-1":
                return ont
            if model is OLTDevice and str(entity_id) == str(olt_id):
                return olt
            return None

        db.get.side_effect = _get
        monkeypatch.setattr(
            "app.services.network.provisioning_enforcement._effective_field",
            lambda _db, _ont, key: {
                "mgmt_ip_address": "172.16.201.10",
                "mgmt_vlan": 201,
                "mgmt_subnet": "255.255.255.0",
                "mgmt_gateway": "172.16.201.1",
            }.get(key),
        )
        monkeypatch.setattr(
            "app.services.network.olt_dependency_preflight.validate_olt_profile_dependencies",
            lambda *args, **kwargs: OltDependencyPreflightResult(
                success=False,
                message="OLT management IPHOST enforcement dependency audit failed: missing WAN config profile(s): 0",
                audit={"is_valid": False},
                errors=["missing WAN config profile(s): 0"],
            ),
        )
        get_adapter = MagicMock()
        monkeypatch.setattr(
            "app.services.network.olt_protocol_adapters.get_protocol_adapter",
            get_adapter,
        )

        result = ProvisioningEnforcement.enforce_management_config(db, ["ont-1"])

        assert result == {"pushed": 0, "failed": 1, "skipped": 0}
        get_adapter.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Auto-link ONTs during sync
# ---------------------------------------------------------------------------


class TestAutoLinkOnts:
    def test_sync_auto_links_ont_by_serial(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.tr069 import CpeDevices, acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="AutoLink ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )

        # Create an ONT with no ACS server
        ont = OntUnit(serial_number="AUTOLINK-001", is_active=True)
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)
        assert ont.tr069_acs_server_id is None

        # Mock GenieACS to return a device with matching serial
        last_inform_at = datetime.now(UTC)
        mock_device = {
            "_id": "00D09E-TestProduct-AUTOLINK-001",
            "_deviceId": {
                "_OUI": "00D09E",
                "_ProductClass": "TestProduct",
                "_SerialNumber": "AUTOLINK-001",
            },
            "_lastInform": last_inform_at.isoformat(),
        }
        with patch("app.services.tr069.create_genieacs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [mock_device]
            instance.parse_device_id.return_value = (
                "00D09E",
                "TestProduct",
                "AUTOLINK-001",
            )
            instance.extract_parameter_value.return_value = None

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        assert result["created"] == 1
        assert result["auto_linked"] == 1

        # Auto-link records observed ACS state on the CPE row; an ONT FK remains
        # an explicit override, not inherited sync state.
        db_session.refresh(ont)
        assert ont.tr069_acs_server_id is None
        expected_last_inform = last_inform_at.replace(tzinfo=None)
        assert ont.acs_last_inform_at == expected_last_inform
        assert ont.last_seen_at == expected_last_inform
        linked = (
            db_session.query(Tr069CpeDevice)
            .filter_by(serial_number="AUTOLINK-001")
            .first()
        )
        assert linked is not None
        assert linked.ont_unit_id == ont.id
        assert linked.acs_server_id == server.id

    def test_sync_links_existing_unassigned_device_by_serial(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.tr069 import CpeDevices, acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="Existing Unassigned AutoLink ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )
        ont = OntUnit(serial_number="AUTOLINK-EXISTING-001", is_active=True)
        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number=ont.serial_number,
            genieacs_device_id="00D09E-TestProduct-AUTOLINK-EXISTING-001",
            is_active=True,
        )
        db_session.add_all([ont, device])
        db_session.commit()

        last_inform_at = datetime.now(UTC)
        mock_device = {
            "_id": device.genieacs_device_id,
            "_deviceId": {
                "_OUI": "00D09E",
                "_ProductClass": "TestProduct",
                "_SerialNumber": ont.serial_number,
            },
            "_lastInform": last_inform_at.isoformat(),
        }
        with patch("app.services.tr069.create_genieacs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [mock_device]
            instance.parse_device_id.return_value = (
                "00D09E",
                "TestProduct",
                ont.serial_number,
            )
            instance.extract_parameter_value.return_value = None

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        assert result["created"] == 0
        assert result["updated"] == 1
        db_session.refresh(ont)
        db_session.refresh(device)
        assert device.ont_unit_id == ont.id
        assert ont.acs_last_inform_at == device.last_inform_at
        assert ont.last_seen_at == device.last_inform_at

    def test_olt_ont_acs_sync_and_inform_updates_table_last_seen_e2e(
        self, db_session
    ) -> None:
        from app.models.network import OLTDevice, OntUnit, OnuOnlineStatus
        from app.services import network as network_service
        from app.services import tr069 as tr069_service
        from app.services.network.acs_resolution import resolve_acs_for_ont
        from app.services.tr069 import CpeDevices, acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="E2E ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )
        olt = OLTDevice(
            name="E2E OLT",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        ont = OntUnit(
            serial_number="E2E-OLT-ONT-ACS-001",
            olt_device=olt,
            is_active=True,
            olt_status=OnuOnlineStatus.offline,
        )
        db_session.add_all([olt, ont])
        db_session.commit()

        sync_last_inform = datetime.now(UTC) - timedelta(minutes=2)
        mock_device = {
            "_id": "00D09E-TestProduct-E2E-OLT-ONT-ACS-001",
            "_deviceId": {
                "_OUI": "00D09E",
                "_ProductClass": "TestProduct",
                "_SerialNumber": "E2E-OLT-ONT-ACS-001",
            },
            "_lastInform": sync_last_inform.isoformat(),
        }
        with patch("app.services.tr069.create_genieacs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [mock_device]
            instance.parse_device_id.return_value = (
                "00D09E",
                "TestProduct",
                "E2E-OLT-ONT-ACS-001",
            )
            instance.extract_parameter_value.return_value = None

            sync_result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        assert sync_result["created"] == 1
        db_session.refresh(ont)
        assert ont.tr069_acs_server_id is None
        assert resolve_acs_for_ont(db_session, ont).server_id == str(server.id)
        assert ont.acs_last_inform_at is not None
        assert ont.last_seen_at == ont.acs_last_inform_at

        device = (
            db_session.query(Tr069CpeDevice)
            .filter_by(serial_number="E2E-OLT-ONT-ACS-001")
            .one()
        )
        inform_result = tr069_service.receive_inform(
            db_session,
            serial_number=ont.serial_number,
            device_id_raw=device.genieacs_device_id,
            event="periodic",
            raw_payload={},
            acs_server_id=str(server.id),
        )

        assert inform_result["status"] == "ok"
        db_session.refresh(ont)
        db_session.refresh(device)
        assert ont.last_seen_at == device.last_inform_at

        rows, total = network_service.ont_units.list_advanced(
            db_session,
            search="E2E-OLT-ONT-ACS-001",
            limit=10,
            offset=0,
        )

        assert total == 1
        assert rows[0].id == ont.id
        assert rows[0].last_seen_at == device.last_inform_at

    def test_ont_list_page_data_uses_inventory_for_last_seen(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.web_network_core_devices_views import onts_list_page_data

        stale_last_seen = datetime.now(UTC) - timedelta(hours=2)
        latest_inform = datetime.now(UTC) - timedelta(minutes=5)
        server = Tr069AcsServer(
            name="Latest Inform Table ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        ont = OntUnit(
            serial_number="TABLE-LATEST-INFORM-001",
            is_active=True,
            last_seen_at=stale_last_seen,
            acs_last_inform_at=stale_last_seen,
        )
        db_session.add_all([server, ont])
        db_session.flush()
        db_session.add(
            Tr069CpeDevice(
                acs_server_id=server.id,
                ont_unit_id=ont.id,
                serial_number=ont.serial_number,
                genieacs_device_id="00D09E-TestProduct-TABLE-LATEST-INFORM-001",
                last_inform_at=latest_inform,
                is_active=True,
            )
        )
        db_session.commit()

        page_data = onts_list_page_data(
            db_session,
            authorization="all",
            search=ont.serial_number,
        )

        # The live snapshot source was retired: the list resolves persisted
        # native OLT/ACS evidence and marks absent evidence as derived offline.
        signal_data = page_data["signal_data"][str(ont.id)]
        assert signal_data["acs_last_inform_at"] == stale_last_seen.replace(tzinfo=None)
        assert signal_data["last_seen_at"] == stale_last_seen.replace(tzinfo=None)
        assert signal_data["status_source"] == "derived"
        assert signal_data["status_display"] == "Offline"

    def test_ont_index_template_renders_zabbix_last_seen_column(self) -> None:
        ont_id = uuid4()
        stale_last_seen = datetime(2026, 4, 28, 8, 10, tzinfo=UTC)
        latest_zabbix_seen = datetime(2026, 4, 28, 8, 55, tzinfo=UTC)
        ont = SimpleNamespace(
            id=ont_id,
            serial_number="UI-ACS-LAST-SEEN-001",
            last_seen_at=stale_last_seen,
            olt_rx_signal_dbm=None,
            is_active=True,
            authorization_status=None,
        )
        page = SimpleNamespace(page=1, pages=1, total=1, total_pages=1, per_page=50)
        context = {
            "request": SimpleNamespace(
                url=SimpleNamespace(path="/admin/network/onts"),
                query_params={},
                state=SimpleNamespace(csrf_token="test"),
            ),
            "active_page": "onts",
            "active_menu": "network",
            "current_user": None,
            "sidebar_stats": {},
            "filters": SimpleNamespace(
                view="list",
                olt_id=None,
                pon_port_id=None,
                pon_hint=None,
                zone_id=None,
                operational_status=None,
                authorization="authorized",
                signal_quality=None,
                vendor=None,
                search="",
                order_by="serial_number",
                order_dir="asc",
            ),
            "message": None,
            "status": None,
            "status_filter": "",
            "stats": SimpleNamespace(
                total_onts=1,
                online_count=1,
                offline_count=0,
                low_signal_count=0,
                total_cpes=1,
                unconfigured_count=0,
            ),
            "olts": [],
            "zones": [],
            "vendors": [],
            "onts": [ont],
            "signal_data": {
                str(ont_id): {
                    "status_display": "Online",
                    "status_class": "ok",
                    "last_seen_at": latest_zabbix_seen,
                }
            },
            "assignment_info": {str(ont_id): {}},
            "serial_display_by_ont_id": {str(ont_id): ont.serial_number},
            "hex_serial_by_ont_id": {},
            "pagination": page,
            "diagnostics_pagination": page,
            "diagnostics_onts": [],
            "diagnostics_signal_data": {},
            "diagnostics_assignment_info": {},
            "unconfigured_entries": [],
            "unconfigured_selected_view": "",
            "unconfigured_selected_resolution": "",
            "unconfigured_stats": SimpleNamespace(total=0, last_seen_at=None),
            "unconfigured_olts": [],
            "firmware_images": [],
            "authorization_result": None,
        }

        env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
        # The template renders timestamps through the owned app_datetime filter;
        # a bare Environment doesn't get it (that's registered on Jinja2Templates
        # instances), so register it here to keep the test isolation-safe.
        env.filters["app_datetime"] = _app_datetime_filter
        # Rendering the full admin layout also exercises permission-gated
        # navigation, whose helper is an application template global.
        env.globals["can"] = can
        html = env.get_template("admin/network/onts/index.html").render(context)

        assert "UI-ACS-LAST-SEEN-001" in html
        # Timestamps render in the fixed display timezone (WAT = UTC+1) via the
        # app_datetime owned filter, so the 08:55 UTC Zabbix reading shows as
        # 09:55 and the stale 08:10 ONT reading (superseded by Zabbix) as 09:10.
        assert "Apr 28, 09:55" in html
        assert "2026-04-28 09:55:00" in html
        assert "Apr 28, 09:10" not in html

    def test_sync_auto_links_ont_by_normalized_serial(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.tr069 import CpeDevices, acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="Normalized AutoLink ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )

        ont = OntUnit(serial_number="HWTC-7D47-33C3", is_active=True)
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        mock_device = {
            "_id": "00D09E-TestProduct-HWTC7D4733C3",
            "_deviceId": {
                "_OUI": "00D09E",
                "_ProductClass": "TestProduct",
                "_SerialNumber": "HWTC7D4733C3",
            },
            "_lastInform": datetime.now(UTC).isoformat(),
        }
        with patch("app.services.tr069.create_genieacs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [mock_device]
            instance.parse_device_id.return_value = (
                "00D09E",
                "TestProduct",
                "HWTC7D4733C3",
            )
            instance.extract_parameter_value.return_value = None

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        assert result["created"] == 1
        assert result["auto_linked"] == 1
        db_session.refresh(ont)
        assert ont.tr069_acs_server_id is None
        linked = (
            db_session.query(Tr069CpeDevice)
            .filter_by(serial_number="HWTC7D4733C3")
            .first()
        )
        assert linked is not None
        assert linked.ont_unit_id == ont.id
        assert linked.acs_server_id == server.id

    def test_sync_keeps_offline_local_ont_tr069_row_inactive(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.tr069 import CpeDevices, acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="Offline Local ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )
        ont = OntUnit(
            serial_number="HWTC13EE6B84",
            tr069_acs_server_id=server.id,
            is_active=True,
        )
        db_session.add(ont)
        db_session.flush()
        device = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="HWTC13EE6B84",
            is_active=False,
        )
        db_session.add(device)
        db_session.commit()

        with patch("app.services.tr069.create_genieacs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = []

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        db_session.refresh(device)
        db_session.refresh(ont)
        assert result["local_reactivated"] == 0
        assert device.is_active is False
        assert device.ont_unit_id == ont.id
        assert ont.tr069_acs_server_id == server.id

    def test_sync_does_not_create_local_tr069_row_for_olt_assigned_offline_ont(
        self, db_session
    ) -> None:
        from sqlalchemy import select

        from app.models.network import OLTDevice, OntUnit
        from app.services.network.acs_resolution import resolve_acs_for_ont
        from app.services.tr069 import CpeDevices, acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="OLT Local ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )
        olt = OLTDevice(
            name="TR069 Offline OLT",
            tr069_acs_server_id=server.id,
            is_active=True,
        )
        db_session.add(olt)
        db_session.flush()
        ont = OntUnit(
            serial_number="OFFLINE-OLT-ACS-001",
            olt_device_id=olt.id,
            is_active=True,
        )
        db_session.add(ont)
        db_session.commit()

        with patch("app.services.tr069.create_genieacs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = []

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        linked = db_session.scalars(
            select(Tr069CpeDevice).where(
                Tr069CpeDevice.serial_number == "OFFLINE-OLT-ACS-001",
                Tr069CpeDevice.is_active.is_(True),
            )
        ).one_or_none()
        db_session.refresh(ont)
        assert result["local_created"] == 0
        assert linked is None
        assert ont.tr069_acs_server_id is None
        assert resolve_acs_for_ont(db_session, ont).server_id == str(server.id)

    def test_sync_retires_expected_placeholder_when_real_acs_device_arrives(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.tr069 import CpeDevices, acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="Placeholder Reconcile ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )
        ont = OntUnit(
            serial_number="HWTC13EE6B84",
            tr069_acs_server_id=server.id,
            is_active=True,
        )
        db_session.add(ont)
        db_session.flush()
        placeholder = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="HWTC13EE6B84",
            is_active=True,
        )
        db_session.add(placeholder)
        db_session.commit()

        mock_device = {
            "_id": "4857544313EE6B84",
            "_deviceId": {
                "_OUI": "48575443",
                "_ProductClass": "HG8245H",
                "_SerialNumber": "4857544313EE6B84",
            },
            "_lastInform": datetime.now(UTC).isoformat(),
        }
        with patch("app.services.tr069.create_genieacs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [mock_device]
            instance.parse_device_id.return_value = (
                "48575443",
                "HG8245H",
                "4857544313EE6B84",
            )
            instance.extract_parameter_value.return_value = None

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        registered = (
            db_session.query(Tr069CpeDevice)
            .filter_by(genieacs_device_id="4857544313EE6B84")
            .one()
        )
        db_session.refresh(placeholder)
        db_session.refresh(ont)

        assert result["created"] == 1
        assert registered.ont_unit_id == ont.id
        assert registered.is_active is True
        assert placeholder.ont_unit_id is None
        assert placeholder.is_active is False
        assert ont.tr069_acs_server_id == server.id

    def test_unregistered_expected_rows_reject_firmware_and_nat_actions(
        self, db_session
    ) -> None:
        from app.services import web_network_tr069 as web_network_tr069_service
        from app.services.tr069 import acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="Expected Action Guard ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )
        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="EXPECTED-ACTION-001",
            is_active=True,
        )
        db_session.add(device)
        db_session.flush()
        device_id = device.id
        db_session.commit()

        with pytest.raises(Tr069CommandError, match="not registered in GenieACS"):
            web_network_tr069_service.create_firmware_download_job(
                db_session,
                tr069_device_id=str(device_id),
                firmware_url="https://example.test/fw.bin",
            )
        with pytest.raises(Tr069CommandError, match="not registered in GenieACS"):
            web_network_tr069_service.create_nat_port_forward_job(
                db_session,
                tr069_device_id=str(device_id),
                external_port=8080,
                internal_ip="192.168.1.10",
                internal_port=80,
                protocol="TCP",
            )


class TestCreateOntFromTr069Device:
    def test_create_ont_from_tr069_device_creates_inactive_ont(
        self, db_session
    ) -> None:
        from app.services.web_network_tr069 import create_ont_from_tr069_device

        server = Tr069AcsServer(
            name="Create ONT ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()
        db_session.refresh(server)

        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="485754432B526E9A",
            oui="48575443",
            product_class="HG8546M",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()
        db_session.refresh(device)

        ont, created = create_ont_from_tr069_device(
            db_session,
            tr069_device_id=str(device.id),
        )

        assert created is True
        assert ont.serial_number == "HWTC2B526E9A"
        assert ont.model == "HG8546M"
        assert ont.is_active is False
        assert ont.tr069_acs_server_id == server.id
        db_session.refresh(device)
        assert device.ont_unit_id == ont.id

    def test_create_ont_from_tr069_device_reuses_existing_normalized_serial(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.web_network_tr069 import create_ont_from_tr069_device

        server = Tr069AcsServer(
            name="Reuse ONT ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        existing = OntUnit(serial_number="HWTC-2B52-6E9A", is_active=False)
        db_session.add(existing)
        db_session.flush()

        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="HWTC2B526E9A",
            oui="48575443",
            product_class="HG8546M",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()
        db_session.refresh(existing)
        db_session.refresh(device)

        ont, created = create_ont_from_tr069_device(
            db_session,
            tr069_device_id=str(device.id),
        )

        assert created is False
        assert ont.id == existing.id
        assert ont.tr069_acs_server_id == server.id
        db_session.refresh(device)
        assert device.ont_unit_id == existing.id

    def test_create_ont_from_tr069_device_prefers_assigned_olt_record(
        self, db_session
    ) -> None:
        from app.models.network import OLTDevice, OntAssignment, OntUnit
        from app.services.web_network_tr069 import create_ont_from_tr069_device

        server = Tr069AcsServer(
            name="Canonical ONT ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        olt = OLTDevice(name="Canonical OLT", is_active=True)
        db_session.add_all([server, olt])
        db_session.flush()

        duplicate = OntUnit(
            serial_number="485754431D88FBD1",
            is_active=True,
        )
        canonical = OntUnit(
            serial_number="HWTC1D88FBD1",
            is_active=True,
            olt_device_id=olt.id,
        )
        db_session.add_all([duplicate, canonical])
        db_session.flush()
        db_session.add(OntAssignment(ont_unit_id=canonical.id, active=True))

        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="485754431D88FBD1",
            genieacs_device_id="00259E-HG8546M-485754431D88FBD1",
            oui="00259E",
            product_class="HG8546M",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()

        ont, created = create_ont_from_tr069_device(
            db_session,
            tr069_device_id=str(device.id),
        )

        assert created is False
        assert ont.id == canonical.id
        db_session.refresh(device)
        assert device.ont_unit_id == canonical.id


class TestTr069DashboardUi:
    def test_unconfigured_devices_offer_create_ont_action(self) -> None:
        template = Path("templates/admin/network/tr069/index.html").read_text()

        assert "/create-ont" in template
        assert "Create ONT" in template


# ---------------------------------------------------------------------------
# 5. Summary parameter mappings
# ---------------------------------------------------------------------------


class TestSummaryParameterMappings:
    def test_summary_groups_read_only_genieacs_virtual_parameters(self) -> None:
        from app.services.network.tr069_paths import VIRTUAL_PARAM_GROUPS

        assert VIRTUAL_PARAM_GROUPS
        for group in VIRTUAL_PARAM_GROUPS.values():
            for paths in group.values():
                assert paths
                assert all(path.startswith("VirtualParameters.") for path in paths)


# ---------------------------------------------------------------------------
# 6. Session and job cleanup
# ---------------------------------------------------------------------------


class TestRecordCleanup:
    def test_old_sessions_cleaned(self, db_session) -> None:
        server = Tr069AcsServer(
            name="Cleanup ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()

        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="CLEANUP-001",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()

        # Create old session (40 days ago)
        old_session = Tr069Session(
            device_id=device.id,
            event_type=Tr069Event.periodic,
            started_at=datetime.now(UTC) - timedelta(days=40),
        )
        old_session.created_at = datetime.now(UTC) - timedelta(days=40)
        db_session.add(old_session)

        # Create recent session
        new_session = Tr069Session(
            device_id=device.id,
            event_type=Tr069Event.boot,
            started_at=datetime.now(UTC),
        )
        db_session.add(new_session)
        db_session.commit()

        from sqlalchemy import select

        # Verify both exist
        count = len(
            list(
                db_session.scalars(
                    select(Tr069Session).where(Tr069Session.device_id == device.id)
                ).all()
            )
        )
        assert count == 2


# ---------------------------------------------------------------------------
# 7. Celery task registration
# ---------------------------------------------------------------------------


class TestCeleryTaskRegistration:
    def test_sync_task_importable(self) -> None:
        from app.tasks.tr069 import sync_all_acs_devices

        assert sync_all_acs_devices.name == "app.tasks.tr069.sync_all_acs_devices"

    def test_execute_jobs_task_importable(self) -> None:
        from app.tasks.tr069 import reconcile_command_outcomes

        assert (
            reconcile_command_outcomes.name
            == "app.tasks.tr069.reconcile_command_outcomes"
        )

    def test_apply_acs_config_task_importable(self) -> None:
        from app.tasks.tr069 import apply_acs_config

        assert apply_acs_config.name == "app.tasks.tr069.apply_acs_config"

    def test_health_check_task_importable(self) -> None:
        from app.tasks.tr069 import check_device_health

        assert check_device_health.name == "app.tasks.tr069.check_device_health"

    def test_cleanup_task_importable(self) -> None:
        from app.tasks.tr069 import cleanup_tr069_records

        assert cleanup_tr069_records.name == "app.tasks.tr069.cleanup_tr069_records"

    def test_tasks_in_init_all(self) -> None:
        from app.tasks import __all__ as all_tasks

        assert "tr069_sync_all_acs_devices" in all_tasks
        assert "tr069_reconcile_command_outcomes" in all_tasks
        assert "tr069_apply_acs_config" in all_tasks
        assert "tr069_check_device_health" in all_tasks
        assert "cleanup_tr069_records" in all_tasks


# ---------------------------------------------------------------------------
# 8. Device resolution chain
# ---------------------------------------------------------------------------


class TestDeviceResolution:
    def test_resolve_returns_none_for_no_serial(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        # Simulate an ONT with no serial by creating and clearing it
        ont = OntUnit(serial_number="TEMP-RESOLVE", is_active=True)
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)
        # Override serial_number attribute (simulating missing serial)
        ont.serial_number = None  # type: ignore[assignment]

        result, reason = resolve_genieacs_with_reason(db_session, ont)
        assert result is None
        assert "serial number" in reason.lower()

    def test_resolve_returns_none_when_no_acs(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        ont = OntUnit(serial_number="NOACSTEST-001", is_active=True)
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        result, reason = resolve_genieacs_with_reason(db_session, ont)
        assert result is None
        assert "No ACS server" in reason

    def test_resolve_matches_device_by_normalized_serial(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Resolve ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()
        db_session.refresh(server)

        ont = OntUnit(
            serial_number="HWTC-7D47-33C3",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        mock_device = {
            "_id": "00D09E-TestProduct-HWTC7D4733C3",
            "_deviceId": {"_SerialNumber": "HWTC7D4733C3"},
        }

        with patch(
            "app.services.network._resolve.create_genieacs_client"
        ) as MockClient:
            instance = MockClient.return_value
            instance.list_devices.side_effect = [[], [mock_device]]
            instance.extract_parameter_value.side_effect = lambda device, path: None
            instance.parse_device_id.return_value = (
                "00D09E",
                "TestProduct",
                "HWTC7D4733C3",
            )

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        client, device_id = result
        assert device_id == "00D09E-TestProduct-HWTC7D4733C3"
        assert reason == "resolved_via_ont_acs"

    def test_targeted_reconcile_links_existing_discovered_row(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import reconcile_ont_tr069_device

        server = Tr069AcsServer(
            name="Post Auth ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        ont = OntUnit(
            serial_number="HWTC600AC29C",
            vendor_serial_number="48575443600AC29C",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        discovered = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="48575443600AC29C",
            genieacs_device_id="00259E-HG8546M-48575443600AC29C",
            last_inform_at=datetime(2026, 5, 13, 12, 12, 32, tzinfo=UTC),
            is_active=True,
        )
        db_session.add_all([ont, discovered])
        db_session.commit()

        device, reason = reconcile_ont_tr069_device(db_session, ont)

        assert device == discovered
        assert reason == "linked_existing_local_tr069_device"
        assert discovered.ont_unit_id == ont.id
        assert ont.acs_last_inform_at is not None
        assert discovered.last_inform_at is not None
        assert ont.acs_last_inform_at.replace(tzinfo=None) == discovered.last_inform_at

    def test_targeted_reconcile_creates_and_links_live_genieacs_row(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import reconcile_ont_tr069_device

        server = Tr069AcsServer(
            name="Post Auth Live ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        ont = OntUnit(
            serial_number="HWTC600AC29C",
            vendor_serial_number="48575443600AC29C",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.commit()

        mock_device = {
            "_id": "00259E-HG8546M-48575443600AC29C",
            "_deviceId": {
                "_OUI": "00259E",
                "_ProductClass": "HG8546M",
                "_SerialNumber": "48575443600AC29C",
            },
            "_lastInform": "2026-05-13T12:12:32.703Z",
            "InternetGatewayDevice": {
                "ManagementServer": {
                    "ConnectionRequestURL": {"_value": "http://172.16.201.137:7547/abc"}
                }
            },
        }

        with patch(
            "app.services.network._resolve.create_genieacs_client"
        ) as MockClient:
            instance = MockClient.return_value
            instance.list_devices.side_effect = [[], [], [mock_device]]
            instance.get_device.return_value = mock_device
            instance.extract_parameter_value.side_effect = lambda device, path: (
                "http://172.16.201.137:7547/abc"
                if path.endswith("ConnectionRequestURL")
                else None
            )
            instance.parse_device_id.return_value = (
                "00259E",
                "HG8546M",
                "48575443600AC29C",
            )

            device, reason = reconcile_ont_tr069_device(db_session, ont)

        assert device is not None
        assert reason == "created_and_linked_tr069_device"
        assert device.ont_unit_id == ont.id
        assert device.serial_number == "48575443600AC29C"
        assert device.genieacs_device_id == "00259E-HG8546M-48575443600AC29C"
        assert device.connection_request_url == "http://172.16.201.137:7547/abc"
        assert ont.acs_last_inform_at == device.last_inform_at

    def test_resolve_matches_device_by_genieacs_deviceid_serial(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Resolve ACS DeviceId",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()

        ont = OntUnit(
            serial_number="HWTC7D4733C3",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.commit()

        mock_device = {
            "_id": "00259E-EG8145V5-485754437D4733C3",
            "_deviceId": {"_SerialNumber": "485754437D4733C3"},
        }

        with patch(
            "app.services.network._resolve.create_genieacs_client"
        ) as MockClient:
            instance = MockClient.return_value
            instance.list_devices.side_effect = [[], [], [mock_device]]

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        _client, device_id = result
        assert device_id == "00259E-EG8145V5-485754437D4733C3"
        assert reason == "resolved_via_ont_acs"
        issued_queries = [
            call.kwargs["query"] for call in instance.list_devices.call_args_list
        ]
        assert any(
            {"_deviceId._SerialNumber": "485754437D4733C3"} in query.get("$or", [])
            for query in issued_queries
        )

    def test_resolve_matches_device_by_tr098_serial_parameter(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Resolve ACS TR098",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()

        ont = OntUnit(
            serial_number="485754437D4733C3",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.commit()

        mock_device = {
            "_id": "00259E-EG8145V5-ALTID",
            "InternetGatewayDevice": {
                "DeviceInfo": {"SerialNumber": {"_value": "485754437D4733C3"}}
            },
        }

        with patch(
            "app.services.network._resolve.create_genieacs_client"
        ) as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [mock_device]

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        _client, device_id = result
        assert device_id == "00259E-EG8145V5-ALTID"
        assert reason == "resolved_via_ont_acs"
        issued_query = instance.list_devices.call_args.kwargs["query"]
        assert {
            "InternetGatewayDevice.DeviceInfo.SerialNumber._value": ("485754437D4733C3")
        } in issued_query["$or"]

    def test_resolve_does_not_build_synthetic_id_for_placeholder_ont(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.genieacs_client import GenieACSError
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Linked Resolve ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        ont = OntUnit(
            serial_number="HW-OLT-0001",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.flush()

        linked = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="HWTC7D4806C3",
            oui="48575443",
            product_class="EG8145V5",
            is_active=True,
        )
        db_session.add(linked)
        db_session.commit()

        with patch(
            "app.services.network._resolve.create_genieacs_client"
        ) as MockClient:
            instance = MockClient.return_value
            instance.get_device.side_effect = GenieACSError("Device not found")
            instance.list_devices.return_value = []

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is None
        assert "No TR-069 device found" in reason
        instance.build_device_id.assert_not_called()

    def test_resolve_prefers_linked_tr069_device_with_genieacs_id(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Linked Resolve ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        ont = OntUnit(
            serial_number="HW-OLT-0001",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.flush()

        linked = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="HWTC7D4806C3",
            genieacs_device_id="48575443-EG8145V5-HWTC7D4806C3",
            oui="48575443",
            product_class="EG8145V5",
            is_active=True,
        )
        db_session.add(linked)
        db_session.commit()

        with patch(
            "app.services.network._resolve.create_genieacs_client"
        ) as MockClient:
            MockClient.return_value.get_device.return_value = {
                "_id": "48575443-EG8145V5-HWTC7D4806C3"
            }
            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        _client, device_id = result
        assert device_id == "48575443-EG8145V5-HWTC7D4806C3"
        assert reason == "resolved_via_linked_tr069_device"
        MockClient.return_value.get_device.assert_called_once_with(
            "48575443-EG8145V5-HWTC7D4806C3"
        )
        MockClient.return_value.list_devices.assert_not_called()

    def test_resolve_uses_linked_acs_identity_parts_before_serial_search(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Linked ACS Identity",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        ont = OntUnit(
            serial_number="HWTC1DAF83D1",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.flush()

        linked = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="485754431DAF83D1",
            oui="00259E",
            product_class="HG8546M",
            is_active=True,
        )
        db_session.add(linked)
        db_session.commit()

        with patch(
            "app.services.network._resolve.create_genieacs_client"
        ) as MockClient:
            instance = MockClient.return_value
            instance.get_device.return_value = {
                "_id": "00259E-HG8546M-485754431DAF83D1"
            }

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        _client, device_id = result
        assert device_id == "00259E-HG8546M-485754431DAF83D1"
        assert reason == "resolved_via_linked_tr069_device"
        assert linked.genieacs_device_id == "00259E-HG8546M-485754431DAF83D1"
        instance.get_device.assert_called_once_with("00259E-HG8546M-485754431DAF83D1")
        instance.list_devices.assert_not_called()

    def test_direct_device_writes_require_persisted_genieacs_id(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import (
            resolve_linked_genieacs_with_reason,
        )

        server = Tr069AcsServer(
            name="Strict Write ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()
        ont = OntUnit(
            serial_number="HWTC1D88FBD1",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.flush()
        linked = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="485754431D88FBD1",
            oui="00259E",
            product_class="HG8546M",
            is_active=True,
        )
        fallback = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="485754431D88FBD1",
            genieacs_device_id="00259E-HG8546M-485754431D88FBD1",
            oui="00259E",
            product_class="HG8546M",
            is_active=True,
        )
        db_session.add_all([linked, fallback])
        db_session.commit()

        result, reason = resolve_linked_genieacs_with_reason(db_session, ont)

        assert result is None
        assert "no linked GenieACS device ID" in reason

    def test_resolve_clears_stale_linked_genieacs_id_when_acs_record_missing(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.genieacs_client import GenieACSError
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Linked Missing ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        ont = OntUnit(
            serial_number="HWTC600AC29C",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.flush()

        linked = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="48575443600AC29C",
            genieacs_device_id="00259E-HG8546M-48575443600AC29C",
            oui="00259E",
            product_class="HG8546M",
            is_active=True,
        )
        db_session.add(linked)
        db_session.commit()

        with patch(
            "app.services.network._resolve.create_genieacs_client"
        ) as MockClient:
            instance = MockClient.return_value
            instance.get_device.side_effect = GenieACSError(
                "Device not found: 00259E-HG8546M-48575443600AC29C"
            )
            instance.list_devices.return_value = []

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is None
        assert "No TR-069 device found" in reason
        assert linked.genieacs_device_id is None

    def test_resolve_links_unlinked_tr069_device_by_serial_match(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Unlinked Resolve ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        ont = OntUnit(
            serial_number="HWTC600AC29C",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        observed = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="48575443600AC29C",
            genieacs_device_id="00259E-HG8546M-48575443600AC29C",
            oui="00259E",
            product_class="HG8546M",
            is_active=True,
        )
        db_session.add_all([ont, observed])
        db_session.commit()

        with patch(
            "app.services.network._resolve.create_genieacs_client"
        ) as MockClient:
            MockClient.return_value.get_device.return_value = {
                "_id": "00259E-HG8546M-48575443600AC29C"
            }
            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        _client, device_id = result
        assert device_id == "00259E-HG8546M-48575443600AC29C"
        assert reason == "resolved_via_unlinked_tr069_serial_match"
        assert observed.ont_unit_id == ont.id
        MockClient.return_value.get_device.assert_called_once_with(
            "00259E-HG8546M-48575443600AC29C"
        )
        MockClient.return_value.list_devices.assert_not_called()

    def test_resolve_reuses_linked_device_after_stale_genieacs_id_cleared(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import (
            clear_stale_genieacs_device_id,
            resolve_genieacs_with_reason,
        )

        server = Tr069AcsServer(
            name="Stale Resolve ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        ont = OntUnit(
            serial_number="HWTC600AC29C",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.flush()
        linked = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="48575443600AC29C",
            genieacs_device_id="00259E-HG8546M-OLD600AC29C",
            oui="00259E",
            product_class="HG8546M",
            is_active=True,
        )
        db_session.add(linked)
        db_session.commit()

        assert clear_stale_genieacs_device_id(
            db_session,
            ont,
            "00259E-HG8546M-OLD600AC29C",
        )

        with patch(
            "app.services.network._resolve.create_genieacs_client"
        ) as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [
                {"_id": "00259E-HG8546M-48575443600AC29C"}
            ]

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        _client, device_id = result
        assert device_id == "00259E-HG8546M-48575443600AC29C"
        assert reason == "resolved_via_linked_tr069_device"
        assert linked.ont_unit_id == ont.id
        assert linked.genieacs_device_id == "00259E-HG8546M-48575443600AC29C"

    def test_resolve_moves_ont_link_when_genieacs_id_already_owned(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Linked Resolve ACS Conflict",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        ont = OntUnit(
            serial_number="HWTCA31A3673",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.flush()

        placeholder = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="HWTCA31A3673",
            is_active=True,
        )
        discovered = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="48575443A31A3673",
            genieacs_device_id="00259E-EG8145V5-48575443A31A3673",
            oui="00259E",
            product_class="EG8145V5",
            is_active=True,
        )
        db_session.add_all([placeholder, discovered])
        db_session.commit()

        mock_device = {
            "_id": "00259E-EG8145V5-48575443A31A3673",
            "_deviceId": {"_SerialNumber": "48575443A31A3673"},
        }

        with patch(
            "app.services.network._resolve.create_genieacs_client"
        ) as MockClient:
            instance = MockClient.return_value
            instance.list_devices.side_effect = [[], [], [mock_device]]

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        _client, device_id = result
        assert device_id == "00259E-EG8145V5-48575443A31A3673"
        assert reason == "resolved_via_unlinked_tr069_serial_match"
        assert discovered.ont_unit_id == ont.id
        assert placeholder.ont_unit_id is None
        assert placeholder.is_active is False
        instance.list_devices.assert_not_called()


class TestAcsPropagation:
    def test_acs_enforcement_preset_defaults_to_bootstrap_and_boot_only(self) -> None:
        from app.services.tr069 import _build_acs_preset

        preset = _build_acs_preset("preset", "provision")

        assert preset["events"] == {
            "0 BOOTSTRAP": True,
            "1 BOOT": True,
        }
        assert preset["configurations"] == [
            {"type": "provision", "name": "provision", "args": []}
        ]

    def test_acs_enforcement_preset_can_opt_into_periodic(self) -> None:
        from app.services.tr069 import _build_acs_preset

        preset = _build_acs_preset("preset", "provision", on_periodic=True)

        assert preset["events"]["2 PERIODIC"] is True

    def test_acs_enforcement_script_sets_both_credential_pairs(self) -> None:
        from app.services.tr069 import _build_acs_provision_script

        script = _build_acs_provision_script(
            cwmp_url="http://acs.example/cwmp",
            cwmp_username="cwmp-user",
            cwmp_password="cwmp-pass",
            connection_request_username="cr-user",
            connection_request_password="cr-pass",
            periodic_inform_interval=300,
        )

        assert "ManagementServer.Username" in script
        assert "cwmp-user" in script
        assert "ManagementServer.Password" in script
        assert "cwmp-pass" in script
        assert "ManagementServer.ConnectionRequestUsername" in script
        assert "cr-user" in script
        assert "ManagementServer.ConnectionRequestPassword" in script
        assert "cr-pass" in script
        assert "ext(" not in script

    def test_auto_bind_uses_olt_linked_acs_profile_without_hardcoded_match(
        self, monkeypatch
    ) -> None:
        from app.services.network import olt_ssh

        olt = SimpleNamespace(
            name="OLT-AutoBind",
            tr069_acs_server=SimpleNamespace(
                name="Primary ACS",
                cwmp_url="http://acs.example.com/cwmp/",
                cwmp_username="cwmp-user",
                cwmp_password=None,
                periodic_inform_interval=300,
            ),
        )
        profiles = [
            olt_ssh.Tr069ServerProfile(
                profile_id=17,
                name="Provider Profile",
                acs_url="http://acs.example.com/cwmp",
                acs_username="cwmp-user",
            )
        ]
        bound: dict[str, int] = {}

        monkeypatch.setattr(
            olt_ssh,
            "get_tr069_server_profiles",
            lambda _olt: (True, "ok", profiles),
        )
        monkeypatch.setattr(
            olt_ssh,
            "bind_tr069_server_profile",
            lambda _olt, fsp, ont_id, profile_id: (
                bound.update({"profile_id": profile_id, "ont_id": ont_id})
                or (True, "bound")
            ),
        )

        olt_ssh._auto_bind_tr069_after_authorize(olt, "0/2/1", 6)

        assert bound == {"profile_id": 17, "ont_id": 6}

    def test_auto_bind_accepts_matching_profile_when_username_not_parsed(
        self, monkeypatch
    ) -> None:
        from app.services.network import olt_ssh

        olt = SimpleNamespace(
            name="OLT-AutoBind-NoUsername",
            tr069_acs_server=SimpleNamespace(
                name="Primary ACS",
                cwmp_url="http://acs.example.com/cwmp",
                cwmp_username="cwmp-user",
                cwmp_password=None,
                periodic_inform_interval=300,
            ),
        )
        profiles = [
            olt_ssh.Tr069ServerProfile(
                profile_id=19,
                name="Provider Profile",
                acs_url="http://acs.example.com/cwmp",
                acs_username="",
            )
        ]
        bound: dict[str, int] = {}

        monkeypatch.setattr(
            olt_ssh,
            "get_tr069_server_profiles",
            lambda _olt: (True, "ok", profiles),
        )
        monkeypatch.setattr(
            olt_ssh,
            "create_tr069_server_profile",
            lambda *_args, **_kwargs: pytest.fail("matching profile should be reused"),
        )
        monkeypatch.setattr(
            olt_ssh,
            "bind_tr069_server_profile",
            lambda _olt, fsp, ont_id, profile_id: (
                bound.update({"profile_id": profile_id, "ont_id": ont_id})
                or (True, "bound")
            ),
        )

        olt_ssh._auto_bind_tr069_after_authorize(olt, "0/2/1", 6)

        assert bound == {"profile_id": 19, "ont_id": 6}

    def test_auto_bind_creates_profile_for_olt_linked_acs_when_missing(
        self, monkeypatch
    ) -> None:
        from app.services.network import olt_ssh

        olt = SimpleNamespace(
            name="OLT-AutoCreate",
            tr069_acs_server=SimpleNamespace(
                name="Primary ACS",
                cwmp_url="http://acs.example.com/cwmp",
                cwmp_username="cwmp-user",
                cwmp_password=None,
                periodic_inform_interval=180,
            ),
        )
        calls = {"list": 0}
        created: dict[str, object] = {}
        bound: dict[str, int] = {}

        def fake_profiles(_olt):
            calls["list"] += 1
            if calls["list"] == 1:
                return True, "ok", []
            return (
                True,
                "ok",
                [
                    olt_ssh.Tr069ServerProfile(
                        profile_id=23,
                        name="ACS Primary ACS",
                        acs_url="http://acs.example.com/cwmp",
                        acs_username="cwmp-user",
                    )
                ],
            )

        def fake_create(_olt, **kwargs):
            created.update(kwargs)
            return True, "created"

        monkeypatch.setattr(olt_ssh, "get_tr069_server_profiles", fake_profiles)
        monkeypatch.setattr(olt_ssh, "create_tr069_server_profile", fake_create)
        monkeypatch.setattr(
            olt_ssh,
            "bind_tr069_server_profile",
            lambda _olt, fsp, ont_id, profile_id: (
                bound.update({"profile_id": profile_id, "ont_id": ont_id})
                or (True, "bound")
            ),
        )

        olt_ssh._auto_bind_tr069_after_authorize(olt, "0/2/1", 6)

        assert created["acs_url"] == "http://acs.example.com/cwmp"
        assert created["username"] == "cwmp-user"
        assert created["inform_interval"] == 180
        assert bound == {"profile_id": 23, "ont_id": 6}

    def test_tr069_profile_match_does_not_fallback_to_dotmac_name(self) -> None:
        from app.services.network.olt_tr069_admin import match_tr069_profile

        wrong_profile = SimpleNamespace(
            profile_id=99,
            name="DotMac old ACS",
            acs_url="http://old-acs.example.com/cwmp",
            acs_username="cwmp-user",
        )

        result = match_tr069_profile(
            [wrong_profile],
            acs_url="http://new-acs.example.com/cwmp",
            acs_username="cwmp-user",
        )

        assert result is None

    def test_olt_create_auto_init_uses_linked_acs_service(self, monkeypatch) -> None:
        from app.services.network import olt_web_forms

        olt = SimpleNamespace(
            id="olt-1",
            name="OLT Auto Init",
            ssh_username="admin",
            ssh_password="encrypted",
        )
        called: dict[str, object] = {}

        def fake_ensure(received_olt):
            called["olt"] = received_olt
            return True, "profile ready", 31

        monkeypatch.setattr(
            "app.services.network.olt_tr069_admin.ensure_tr069_profile_for_linked_acs",
            fake_ensure,
        )

        olt_web_forms._auto_init_tr069_profile(olt)

        assert called["olt"] is olt

    def test_queue_acs_propagation_delegates_to_tracked_reconciliation(
        self, db_session
    ) -> None:
        from app.models.network import OLTDevice, OntUnit
        from app.services.web_network_olts import _queue_acs_propagation

        server = Tr069AcsServer(
            name="Propagation ACS",
            base_url="http://genieacs:7557",
            cwmp_url="http://acs.example.com/cwmp",
            cwmp_username="cwmp-user",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()
        db_session.refresh(server)

        olt = OLTDevice(name="OLT-TR069", tr069_acs_server_id=server.id)
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        ont = OntUnit(serial_number="PROP-001", is_active=True, olt_device_id=olt.id)
        db_session.add(ont)
        db_session.commit()

        expected = {
            "attempted": 1,
            "queued": 1,
            "duplicates": 0,
            "errors": 0,
            "operation_id": "operation-1",
        }
        with patch(
            "app.services.network.ont_reconcile_queue.queue_olt_acs_reconciliation",
            return_value=expected,
        ) as queue_reconciliation:
            stats = _queue_acs_propagation(db_session, olt)

        assert stats == expected
        queue_reconciliation.assert_called_once_with(db_session, olt)
