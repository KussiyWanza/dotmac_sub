"""Tests for OLT operational API endpoints (Phase 2)."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.schemas.network_olt_ops import (
    OltAuthorizeOntRequest,
    OltCliCommandRequest,
    OltDiscoveredOntRead,
    OltOperationResponse,
    OltProfileRead,
    OltServicePortCreateRequest,
    OltServicePortRead,
    OltTr069ProfileCreateRequest,
    OltTr069ProfileRead,
)


class TestOltSchemas:
    """Schema validation tests for OLT operations."""

    def test_authorize_request_valid(self):
        req = OltAuthorizeOntRequest(
            ont_id=UUID("00000000-0000-0000-0000-000000000123"),
            fsp="0/1/0",
            serial_number="HWTC-ABCD1234",
        )
        assert req.fsp == "0/1/0"
        assert req.serial_number == "HWTC-ABCD1234"

    def test_authorize_request_short_serial(self):
        with pytest.raises(Exception):
            OltAuthorizeOntRequest(
                ont_id=UUID("00000000-0000-0000-0000-000000000123"),
                fsp="0/1/0",
                serial_number="ABC",
            )

    def test_service_port_create_request(self):
        req = OltServicePortCreateRequest(
            fsp="0/2/0", ont_id=5, gem_index=0, vlan_id=203
        )
        assert req.tag_transform == "translate"

    def test_cli_command_empty(self):
        with pytest.raises(Exception):
            OltCliCommandRequest(command="")

    def test_cli_command_valid(self):
        req = OltCliCommandRequest(command="display version")
        assert req.command == "display version"

    def test_discovered_ont_read(self):
        read = OltDiscoveredOntRead(
            fsp="0/1/0", serial_number="HWTC-1234", model="EG8145V5"
        )
        assert read.serial_hex is None
        assert read.model == "EG8145V5"

    def test_service_port_read(self):
        read = OltServicePortRead(
            index=1, vlan_id=203, ont_id=5, gem_index=0, state="up"
        )
        assert read.flow_type is None

    def test_profile_read(self):
        read = OltProfileRead(profile_id=10, name="HSI_100M")
        assert read.profile_id == 10

    def test_tr069_profile_read(self):
        read = OltTr069ProfileRead(
            profile_id=1, name="ACS-Default", acs_url="http://acs:7547"
        )
        assert read.username is None

    def test_tr069_create_request(self):
        req = OltTr069ProfileCreateRequest(name="ACS-New", acs_url="http://acs:7547")
        assert req.username == ""
        assert req.inform_interval == 300

    def test_operation_response(self):
        resp = OltOperationResponse(success=True, message="Done", data={"key": "val"})
        assert resp.data == {"key": "val"}


class TestRouterRegistration:
    """Test that OLT ops router is properly registered."""

    def test_olt_ops_routes_exist(self):
        from app.main import app

        paths = [r.path for r in app.routes]
        assert not any("/olt-devices/{olt_id}/discover-onts" in p for p in paths)
        assert any("/olt-devices/{olt_id}/test-connection" in p for p in paths)
        assert any("/olt-devices/{olt_id}/cli-command" in p for p in paths)
        assert any("/olt-devices/{olt_id}/profiles/line" in p for p in paths)
        assert any("/olt-devices/{olt_id}/service-ports" in p for p in paths)


class TestAuthorizeEndpoint:
    """Authorization endpoints submit durable network commands."""

    def test_authorize_ont_submits_command(self, monkeypatch):
        from app.api import network_olt_ops

        captured = {}

        def fake_authorize_ont(db, command):
            captured.update(
                {
                    "db": db,
                    "olt_id": command.target.olt_id,
                    "ont_id": command.ont_id,
                    "fsp": command.target.fsp.value,
                    "serial_number": command.target.serial_number.value,
                    "force_reauthorize": command.force_reauthorize,
                    "actor": command.context.actor,
                }
            )
            return network_olt_ops.olt_api_operations.OltAuthorizationApiResult(
                success=True,
                message="ONT authorization accepted.",
                operation_id=UUID("00000000-0000-0000-0000-000000000124"),
                dispatch_id=UUID("00000000-0000-0000-0000-000000000125"),
                waiting=True,
                duplicate=False,
            )

        monkeypatch.setattr(
            network_olt_ops.olt_api_operations,
            "authorize_and_provision_ont",
            fake_authorize_ont,
        )

        payload = OltAuthorizeOntRequest(
            ont_id=UUID("00000000-0000-0000-0000-000000000123"),
            fsp="0/1/0",
            serial_number="HWTCABCD1234",
            force_reauthorize=True,
        )
        request = object()
        db = object()

        response = network_olt_ops.authorize_and_provision_ont(
            request,
            UUID("00000000-0000-0000-0000-000000000122"),
            payload,
            db=db,
        )

        assert response.success is True
        assert response.data.operation_id == UUID(
            "00000000-0000-0000-0000-000000000124"
        )
        assert response.data.dispatch_id == UUID("00000000-0000-0000-0000-000000000125")
        assert response.data.waiting is True
        assert response.data.duplicate is False
        assert captured == {
            "db": db,
            "olt_id": UUID("00000000-0000-0000-0000-000000000122"),
            "ont_id": UUID("00000000-0000-0000-0000-000000000123"),
            "fsp": "0/1/0",
            "serial_number": "HWTCABCD1234",
            "force_reauthorize": True,
            "actor": "unknown",
        }
