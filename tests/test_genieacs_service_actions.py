from __future__ import annotations

from types import SimpleNamespace


def test_genieacs_service_receives_inform(monkeypatch) -> None:
    from app.services import tr069 as tr069_service
    from app.services.genieacs_service import GenieAcsService

    calls = {}

    def fake_receive_inform(db, **kwargs):
        calls["db"] = db
        calls["kwargs"] = kwargs
        return {"status": "ok", "source": "fake"}

    monkeypatch.setattr(tr069_service, "receive_inform", fake_receive_inform)

    result = GenieAcsService().receive_inform(
        object(),
        serial_number="ABC123",
        device_id_raw="OUI-Model-ABC123",
        event="periodic",
        raw_payload={"serial": "ABC123"},
        request_id="req-1",
    )

    assert result == {"status": "ok", "source": "fake"}
    assert calls["kwargs"]["serial_number"] == "ABC123"
    assert calls["kwargs"]["device_id_raw"] == "OUI-Model-ABC123"
    assert calls["kwargs"]["request_id"] == "req-1"


def test_tr069_inform_route_uses_genieacs_service(monkeypatch) -> None:
    from app.api import tr069_inform

    calls = {}

    class FakeAcsService:
        def receive_inform(self, db, **kwargs):
            calls["db"] = db
            calls["kwargs"] = kwargs
            return {"status": "ok", "source": "factory"}

    class FakeClient:
        host = "192.0.2.10"

    class FakeRequest:
        client = FakeClient()
        headers = {
            "x-request-id": "req-route",
            "user-agent": "pytest",
        }

    monkeypatch.setattr(tr069_inform, "genieacs_service", FakeAcsService())

    payload = tr069_inform.InformPayload(
        serial_number="ABC123",
        device_id="OUI-Model-ABC123",
        event="periodic",
    )
    result = tr069_inform.receive_inform(FakeRequest(), payload, object())

    assert result == {"status": "ok", "source": "factory"}
    assert calls["kwargs"]["serial_number"] == "ABC123"
    assert calls["kwargs"]["device_id_raw"] == "OUI-Model-ABC123"
    assert calls["kwargs"]["request_id"] == "req-route"


def test_device_config_includes_wan_resolution_hints(monkeypatch) -> None:
    from app.api import tr069_inform

    onu_type = SimpleNamespace(
        id="onu-type-1",
        name="Huawei HG8546M",
        adapter_name=None,
        tr069_data_model="tr098",
        wan_pppoe_username_path=(
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2."
            "WANPPPConnection.1.Username"
        ),
        wan_pppoe_password_path=(
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2."
            "WANPPPConnection.1.Password"
        ),
    )
    ont = SimpleNamespace(id="ont-1", onu_type=onu_type)

    monkeypatch.setattr(
        tr069_inform,
        "find_unique_active_ont_by_serial",
        lambda db, serial_number: ont,
    )
    monkeypatch.setattr(
        tr069_inform,
        "resolve_effective_ont_config",
        lambda db, ont: {
            "values": {
                "wan_mode": "pppoe",
                "wan_vlan": 203,
                "pppoe_wcd_index": 2,
                "pppoe_username": "100025868",
                "pppoe_password": "plain-secret",
            }
        },
    )

    result = tr069_inform.get_device_config("HWTC600AC29C", object())

    assert result["wan"]["vlan"] == 203
    assert result["wan"]["wcd_index"] == 2
    assert result["wan"]["pppoe_username"] == "100025868"
    assert result["paths"]["wan_pppoe_username"].endswith(
        "WANConnectionDevice.2.WANPPPConnection.1.Username"
    )


def test_set_pppoe_credentials_names_internet_ppp_slot(monkeypatch) -> None:
    from app.services.network import ont_action_wan

    calls = {}
    ont = SimpleNamespace(serial_number="4857544306351E9C")

    monkeypatch.setattr(
        ont_action_wan,
        "get_ont_client_or_error",
        lambda db, ont_id: ((ont, object(), "device-1"), None),
    )
    monkeypatch.setattr(
        ont_action_wan,
        "detect_data_model_root",
        lambda db, ont, client, device_id: "InternetGatewayDevice",
    )
    monkeypatch.setattr(ont_action_wan, "persist_data_model_root", lambda *a: None)
    monkeypatch.setattr(
        ont_action_wan,
        "_ensure_igd_ppp_wan_service",
        lambda **kwargs: (1, None),
    )

    def fake_set_and_verify(client, device_id, params, *, expected=None, **kwargs):
        calls["params"] = params
        calls["expected"] = expected
        return {"_id": "task-1"}

    monkeypatch.setattr(ont_action_wan, "set_and_verify", fake_set_and_verify)

    result = ont_action_wan.set_pppoe_credentials(
        object(),
        "ont-1",
        username="100025868",
        password="secret",
        instance_index=1,
        wan_vlan=203,
    )

    name_path = (
        "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1."
        "WANPPPConnection.1.Name"
    )
    assert result.success is True
    assert calls["params"][name_path] == ont_action_wan.INTERNET_PPP_CONNECTION_NAME
    assert calls["expected"][name_path] == ont_action_wan.INTERNET_PPP_CONNECTION_NAME


def test_internet_ppp_vlan_mismatch_is_repairable() -> None:
    from app.services.network.ont_action_wan import _igd_ppp_container_conflict

    assert (
        _igd_ppp_container_conflict(
            details={"ppp_service": "INTERNET", "ppp_vlan": "1"},
            wan_vlan=203,
        )
        is None
    )
    assert _igd_ppp_container_conflict(
        details={"ppp_service": "TR069", "ppp_vlan": "201"},
        wan_vlan=203,
    )


def test_set_pppoe_credentials_creates_missing_igd_ppp_object(monkeypatch) -> None:
    from app.services.network import ont_action_wan

    calls = {}
    ont = SimpleNamespace(
        serial_number="4857544306351E9C",
        tr069_last_snapshot={},
    )

    initial_device = {
        "InternetGatewayDevice": {
            "WANDevice": {
                "1": {
                    "WANConnectionDevice": {
                        "2": {
                            "WANPPPConnectionNumberOfEntries": {"_value": 0},
                            "WANIPConnectionNumberOfEntries": {"_value": 0},
                        }
                    }
                }
            }
        }
    }
    created_device = {
        "InternetGatewayDevice": {
            "WANDevice": {
                "1": {
                    "WANConnectionDevice": {
                        "2": {
                            "WANPPPConnectionNumberOfEntries": {"_value": 1},
                            "WANIPConnectionNumberOfEntries": {"_value": 0},
                            "WANPPPConnection": {
                                "3": {
                                    "Username": {"_value": "old-user"},
                                }
                            },
                        }
                    }
                }
            }
        }
    }

    class FakeClient:
        def __init__(self):
            self.object_added = False

        def get_device(self, device_id):
            assert device_id == "device-1"
            return created_device if self.object_added else initial_device

        def add_object(self, device_id, object_path):
            assert device_id == "device-1"
            calls["object_path"] = object_path
            self.object_added = True
            return {"_id": "task-add-1"}

        def refresh_object(self, device_id, object_path, allow_when_pending=False):
            assert device_id == "device-1"
            calls.setdefault("refreshes", []).append((object_path, allow_when_pending))
            return {"_id": "task-refresh-1"}

        def extract_parameter_value(self, device, parameter_path):
            current = device
            for part in parameter_path.split("."):
                if not isinstance(current, dict):
                    return None
                current = current.get(part)
                if current is None:
                    return None
            if isinstance(current, dict) and "_value" in current:
                return current["_value"]
            if isinstance(current, dict):
                return None
            return current

    monkeypatch.setattr(
        ont_action_wan,
        "get_ont_client_or_error",
        lambda db, ont_id: ((ont, FakeClient(), "device-1"), None),
    )
    monkeypatch.setattr(
        ont_action_wan,
        "detect_data_model_root",
        lambda db, ont, client, device_id: "InternetGatewayDevice",
    )
    monkeypatch.setattr(ont_action_wan, "persist_data_model_root", lambda *a: None)
    monkeypatch.setattr(
        ont_action_wan, "_persist_runtime_capabilities", lambda *a: None
    )
    monkeypatch.setattr(ont_action_wan.time, "sleep", lambda *_: None)

    def fake_set_and_verify(client, device_id, params, *, expected=None, **kwargs):
        calls["params"] = params
        calls["expected"] = expected
        return {"_id": "task-1"}

    monkeypatch.setattr(ont_action_wan, "set_and_verify", fake_set_and_verify)

    result = ont_action_wan.set_pppoe_credentials(
        object(),
        "ont-1",
        username="100025868",
        password="secret",
        instance_index=2,
        wan_vlan=203,
    )

    username_path = (
        "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2."
        "WANPPPConnection.3.Username"
    )
    assert result.success is True
    assert calls["object_path"] == (
        "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2.WANPPPConnection."
    )
    assert calls["params"][username_path] == "100025868"
    assert calls["expected"][username_path] == "100025868"


def test_set_pppoe_credentials_prefers_existing_primary_igd_ppp_child(
    monkeypatch,
) -> None:
    from app.services.network import ont_action_wan

    calls = {}
    ont = SimpleNamespace(serial_number="4857544306351E9C")
    device = {
        "InternetGatewayDevice": {
            "WANDevice": {
                "1": {
                    "WANConnectionDevice": {
                        "2": {
                            "WANPPPConnectionNumberOfEntries": {"_value": 2},
                            "WANIPConnectionNumberOfEntries": {"_value": 0},
                            "WANPPPConnection": {
                                "1": {
                                    "Username": {"_value": "live-user"},
                                    "X_HW_VLAN": {"_value": "203"},
                                },
                                "3": {
                                    "Username": {"_value": "stale-user"},
                                    "X_HW_VLAN": {"_value": "203"},
                                },
                            },
                        }
                    }
                }
            }
        }
    }

    class FakeClient:
        def get_device(self, device_id):
            assert device_id == "device-1"
            return device

        def extract_parameter_value(self, current, parameter_path):
            node = current
            for part in parameter_path.split("."):
                if not isinstance(node, dict):
                    return None
                node = node.get(part)
                if node is None:
                    return None
            if isinstance(node, dict) and "_value" in node:
                return node["_value"]
            return node if not isinstance(node, dict) else None

    monkeypatch.setattr(
        ont_action_wan,
        "get_ont_client_or_error",
        lambda db, ont_id: ((ont, FakeClient(), "device-1"), None),
    )
    monkeypatch.setattr(
        ont_action_wan,
        "detect_data_model_root",
        lambda db, ont, client, device_id: "InternetGatewayDevice",
    )
    monkeypatch.setattr(ont_action_wan, "persist_data_model_root", lambda *a: None)
    monkeypatch.setattr(
        ont_action_wan, "_persist_runtime_capabilities", lambda *a: None
    )

    def fake_set_and_verify(client, device_id, params, *, expected=None, **kwargs):
        calls["params"] = params
        calls["expected"] = expected
        return {"_id": "task-1"}

    monkeypatch.setattr(ont_action_wan, "set_and_verify", fake_set_and_verify)

    result = ont_action_wan.set_pppoe_credentials(
        object(),
        "ont-1",
        username="100025868",
        password="secret",
        instance_index=2,
        wan_vlan=203,
    )

    username_path = (
        "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2."
        "WANPPPConnection.1.Username"
    )
    assert result.success is True
    assert calls["params"][username_path] == "100025868"
    assert username_path in calls["expected"]


def test_genieacs_service_delegates_wifi_config(monkeypatch) -> None:
    from app.services.genieacs_service import genieacs_service
    from app.services.network.ont_action_common import ActionResult
    from app.services.network.ont_features import OntFeatureService

    calls = {}

    def fake_set_wifi_config(db, ont_id, **kwargs):
        calls["db"] = db
        calls["ont_id"] = ont_id
        calls["kwargs"] = kwargs
        return ActionResult(success=True, message="ok", data={"service": "genieacs"})

    monkeypatch.setattr(OntFeatureService, "set_wifi_config", fake_set_wifi_config)

    result = genieacs_service.set_wifi_config(
        object(),
        "ont-1",
        enabled=True,
        ssid="DOTMAC-1001",
        password="Secret123",
        channel=6,
        security_mode="WPA2-Personal",
    )

    assert result.success is True
    assert calls["ont_id"] == "ont-1"
    assert calls["kwargs"] == {
        "enabled": True,
        "ssid": "DOTMAC-1001",
        "password": "Secret123",
        "channel": 6,
        "security_mode": "WPA2-Personal",
    }


def test_genieacs_service_push_config_urgent_uses_verified_connection_request(
    monkeypatch,
) -> None:
    from app.services.genieacs_service import genieacs_service
    from app.services.network import ont_action_common

    calls = {}

    class FakeClient:
        pass

    def fake_get_ont_client_or_error(db, ont_id):
        calls["db"] = db
        calls["ont_id"] = ont_id
        return (object(), FakeClient(), "device-1"), None

    def fake_set_and_verify(
        client,
        device_id,
        parameters,
        *,
        expected=None,
        connection_request_attempts=3,
        connection_request_backoff_sec=1.0,
    ):
        calls["client"] = client
        calls["device_id"] = device_id
        calls["parameters"] = parameters
        calls["expected"] = expected
        calls["connection_request_attempts"] = connection_request_attempts
        calls["connection_request_backoff_sec"] = connection_request_backoff_sec
        return {"_id": "task-1"}

    monkeypatch.setattr(
        ont_action_common,
        "get_ont_client_or_error",
        fake_get_ont_client_or_error,
    )
    monkeypatch.setattr(ont_action_common, "set_and_verify", fake_set_and_verify)

    result = genieacs_service.push_config_urgent(
        object(),
        "ont-1",
        {"Device.WiFi.SSID.1.SSID": "DOTMAC"},
        expected={"Device.WiFi.SSID.1.SSID": "DOTMAC"},
    )

    assert result.success is True
    assert result.data["device_id"] == "device-1"
    assert calls["ont_id"] == "ont-1"
    assert calls["parameters"] == {"Device.WiFi.SSID.1.SSID": "DOTMAC"}
    assert calls["expected"] == {"Device.WiFi.SSID.1.SSID": "DOTMAC"}
    assert calls["connection_request_attempts"] == 3
    assert calls["connection_request_backoff_sec"] == 1.0


def test_verified_write_retries_connection_request_and_keeps_spv_on_success(
    monkeypatch,
) -> None:
    from app.services.network.ont_action_common import set_and_verify

    calls = {"gpv": 0, "deleted": []}

    class FakeClient:
        def set_parameter_values(self, device_id, params):
            calls["spv"] = (device_id, params)
            return {"_id": "spv-task"}

        def get_parameter_values(self, device_id, paths):
            calls["gpv"] += 1
            if calls["gpv"] < 3:
                return {
                    "_id": f"gpv-task-{calls['gpv']}",
                    "connectionRequestError": "Device is offline",
                }
            return {"_id": "gpv-task-3"}

        def get_device(self, _device_id):
            return {
                "Device": {
                    "WiFi": {
                        "SSID": {
                            "1": {
                                "SSID": {
                                    "_value": "DOTMAC",
                                    "_timestamp": "2026-04-19T00:00:00Z",
                                }
                            }
                        }
                    }
                }
            }

        def delete_task(self, task_id):
            calls["deleted"].append(task_id)

    monkeypatch.setattr(
        "app.services.network.ont_action_common.time.sleep", lambda _: None
    )

    result = set_and_verify(
        FakeClient(),
        "device-1",
        {"Device.WiFi.SSID.1.SSID": "DOTMAC"},
        connection_request_attempts=3,
    )

    assert result == {"_id": "spv-task"}
    assert calls["gpv"] == 3
    assert calls["deleted"] == ["gpv-task-1", "gpv-task-2"]


def test_verified_write_deletes_queued_spv_when_connection_request_never_recovers(
    monkeypatch,
) -> None:
    import pytest

    from app.services.genieacs_client import GenieACSError
    from app.services.network.ont_action_common import set_and_verify

    deleted = []

    class FakeClient:
        def set_parameter_values(self, _device_id, _params):
            return {"_id": "spv-task"}

        def get_parameter_values(self, _device_id, _paths):
            return {
                "_id": "gpv-task",
                "connectionRequestError": "EHOSTUNREACH",
            }

        def delete_task(self, task_id):
            deleted.append(task_id)

    monkeypatch.setattr(
        "app.services.network.ont_action_common.time.sleep", lambda _: None
    )

    with pytest.raises(
        GenieACSError, match="Connection request failed after 2 attempts"
    ):
        set_and_verify(
            FakeClient(),
            "device-1",
            {"Device.WiFi.SSID.1.SSID": "DOTMAC"},
            connection_request_attempts=2,
        )

    assert deleted == ["gpv-task", "gpv-task", "spv-task"]


def test_verified_write_exposes_structured_queued_delivery() -> None:
    import pytest

    from app.services.genieacs_client import (
        GenieACSDeliveryCode,
        GenieACSTaskQueuedError,
    )
    from app.services.network.ont_action_common import set_and_verify

    class FakeClient:
        def set_parameter_values(self, _device_id, _params):
            return {
                "_id": "spv-task",
                "connectionRequestError": "HTTP 401",
            }

    with pytest.raises(GenieACSTaskQueuedError) as raised:
        set_and_verify(
            FakeClient(),
            "device-1",
            {"Device.WiFi.SSID.1.SSID": "DOTMAC"},
        )

    assert raised.value.task_id == "spv-task"
    assert raised.value.reason == "HTTP 401"
    assert raised.value.delivery_code is GenieACSDeliveryCode.connection_request_failed


def test_ont_client_resolution_repairs_stale_genieacs_identity(
    db_session,
    monkeypatch,
) -> None:
    from app.models.network import OntUnit
    from app.services.network import ont_action_common

    ont = OntUnit(serial_number="HWTC600AC29C", is_active=True)
    db_session.add(ont)
    db_session.commit()

    calls: dict[str, object] = {"refreshed": False}
    stale_client = object()
    fresh_client = object()

    def fake_resolve_client_or_error(_db, resolved_ont):
        assert resolved_ont.id == ont.id
        return (stale_client, "stale-device"), None

    def fake_refresh(_db, refreshed_ont, client, device_id):
        assert refreshed_ont.id == ont.id
        assert client is stale_client
        assert device_id == "stale-device"
        calls["refreshed"] = True
        return fresh_client, "fresh-device"

    monkeypatch.setattr(
        ont_action_common,
        "resolve_client_or_error",
        fake_resolve_client_or_error,
    )
    monkeypatch.setattr(
        ont_action_common,
        "refresh_stale_ont_genieacs_identity",
        fake_refresh,
    )

    resolved, error = ont_action_common.get_ont_client_or_error(db_session, str(ont.id))

    assert error is None
    assert resolved is not None
    resolved_ont, client, device_id = resolved
    assert resolved_ont.id == ont.id
    assert client is fresh_client
    assert device_id == "fresh-device"
    assert calls["refreshed"] is True


def test_genieacs_service_download_uses_acs_download_rpc(monkeypatch) -> None:
    from app.services.genieacs_service import genieacs_service
    from app.services.network import ont_action_common

    calls = {}

    class FakeOnt:
        serial_number = "ONT123"

    class FakeClient:
        def download(self, device_id, **kwargs):
            calls["device_id"] = device_id
            calls["kwargs"] = kwargs
            return {"_id": "download-task"}

    def fake_get_ont_client_or_error(db, ont_id):
        calls["db"] = db
        calls["ont_id"] = ont_id
        return (FakeOnt(), FakeClient(), "device-1"), None

    monkeypatch.setattr(
        ont_action_common,
        "get_ont_client_or_error",
        fake_get_ont_client_or_error,
    )

    result = genieacs_service.download(
        object(),
        "ont-1",
        file_type="1 Firmware Upgrade Image",
        file_url="https://example.test/fw.bin",
        filename="fw.bin",
    )

    assert result.success is True
    assert result.data["task"] == {"_id": "download-task"}
    assert calls["device_id"] == "device-1"
    assert calls["kwargs"] == {
        "file_type": "1 Firmware Upgrade Image",
        "file_url": "https://example.test/fw.bin",
        "filename": "fw.bin",
    }


def test_genieacs_service_firmware_upgrade_uses_firmware_image(
    db_session, monkeypatch
) -> None:
    from app.services.genieacs_service import genieacs_service
    from app.services.network.ont_action_common import ActionResult

    calls = {}

    def _request(db, ont_id, firmware_image_id):
        calls.update(db=db, ont_id=ont_id, firmware_image_id=firmware_image_id)
        return ActionResult(
            success=True,
            waiting=True,
            message="queued",
            data={"operation_id": "op-1"},
        )

    monkeypatch.setattr(
        "app.services.network.ont_firmware.request_firmware_upgrade", _request
    )

    result = genieacs_service.firmware_upgrade(
        db_session,
        "ont-1",
        "image-1",
    )

    assert result.success is True
    assert result.waiting is True
    assert calls["ont_id"] == "ont-1"
    assert calls["firmware_image_id"] == "image-1"


def test_genieacs_service_queues_wifi_config(monkeypatch) -> None:
    from app.services.genieacs_service import genieacs_service
    from app.services.queue_adapter import QueueDispatchResult

    calls = {}

    def fake_enqueue_task(task_name, **kwargs):
        calls["task_name"] = task_name
        calls["kwargs"] = kwargs
        return QueueDispatchResult(
            queued=True,
            task_id="task-1",
            task_name=task_name,
            queue=kwargs.get("queue"),
        )

    monkeypatch.setattr("app.services.queue_adapter.enqueue_task", fake_enqueue_task)

    result = genieacs_service.queue_set_wifi_config(
        object(),
        "ont-1",
        enabled=True,
        ssid="DOTMAC-1001",
        password="Secret123",
        channel=6,
        security_mode="WPA2-Personal",
        actor_id="admin-1",
    )

    assert result.queued is True
    assert result.task_id == "task-1"
    assert result.queue == "acs"
    assert calls == {
        "task_name": "app.tasks.tr069.apply_acs_config",
        "kwargs": {
            "args": ("set_wifi_config", "ont-1"),
            "kwargs": {
                "args": [],
                "kwargs": {
                    "enabled": True,
                    "ssid": "DOTMAC-1001",
                    "password": "Secret123",
                    "channel": 6,
                    "security_mode": "WPA2-Personal",
                },
            },
            "queue": "acs",
            "correlation_id": "acs_config:ont-1:set_wifi_config",
            "source": "genieacs_service",
            "request_id": None,
            "actor_id": "admin-1",
        },
    }


def test_genieacs_service_rejects_unknown_queue_action() -> None:
    from app.services.genieacs_service import genieacs_service

    result = genieacs_service.queue_config_action(
        object(),
        "delete_everything",
        "ont-1",
    )

    assert result.queued is False
    assert "Unsupported ACS configuration action" in str(result.error)


def test_apply_acs_config_task_executes_genieacs_service_method(monkeypatch) -> None:
    from app.services.network.ont_action_common import ActionResult
    from app.tasks import tr069

    class FakeSession:
        committed = False
        rolled_back = False
        closed = False

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    session = FakeSession()
    calls = {}

    class FakeGenieAcsService:
        @property
        def queueable_actions(self):
            return frozenset({"set_wifi_ssid"})

        def supports_config_action(self, action):
            calls["supports_action"] = action
            return action in self.queueable_actions

        def execute_config_action(self, db, action, ont_id, *, args=None, kwargs=None):
            calls["execute"] = {
                "db": db,
                "action": action,
                "ont_id": ont_id,
                "args": args,
                "kwargs": kwargs,
            }
            return ActionResult(
                success=True,
                message="queued apply ok",
                data={"x": 1},
            )

    def fake_session_local():
        return session

    monkeypatch.setattr(tr069, "SessionLocal", fake_session_local)
    monkeypatch.setattr(tr069, "genieacs_service", FakeGenieAcsService())

    result = tr069.apply_acs_config.run(
        "set_wifi_ssid",
        "ont-1",
        args=["DOTMAC"],
    )

    assert result == {
        "action": "set_wifi_ssid",
        "ont_id": "ont-1",
        "success": True,
        "message": "queued apply ok",
        "waiting": False,
        "data": {"x": 1},
    }
    assert calls == {
        "supports_action": "set_wifi_ssid",
        "execute": {
            "db": session,
            "action": "set_wifi_ssid",
            "ont_id": "ont-1",
            "args": ["DOTMAC"],
            "kwargs": None,
        },
    }
    assert session.committed is True
    assert session.rolled_back is False
    assert session.closed is True
