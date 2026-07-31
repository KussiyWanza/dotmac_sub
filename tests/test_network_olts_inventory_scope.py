from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from starlette.requests import Request

from app.models.network import OLTDevice, OntUnit
from app.models.ont_autofind import OltAutofindCandidate
from app.models.subscriber import UserType
from app.services.network.ont_authorization_contracts import OntAuthorizationAdmission


def _request(*, htmx: bool = False) -> Request:
    headers = [(b"hx-request", b"true")] if htmx else []
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/network/olts/authorize-ont",
            "headers": headers,
            "query_string": b"",
        }
    )


def test_direct_ont_authorize_rejects_mismatched_olt(monkeypatch) -> None:
    from app.web.admin import network_olts_inventory

    olt_id = uuid4()
    other_olt_id = uuid4()
    ont_id = uuid4()

    class FakeDb:
        def get(self, *_args):
            return SimpleNamespace(
                olt_device_id=other_olt_id,
                serial_number="HWTC12345678",
            )

    def _unexpected_queue(*_args, **_kwargs):
        raise AssertionError("mismatched direct ONT authorization must not authorize")

    monkeypatch.setattr(
        network_olts_inventory,
        "request_ont_authorization",
        _unexpected_queue,
    )

    response = network_olts_inventory.olt_authorize_ont(
        _request(),
        str(olt_id),
        fsp="0/1/1",
        serial_number="HWTC12345678",
        ont_id=str(ont_id),
        db=FakeDb(),
    )

    assert response.status_code == 303
    assert "authorization+scope+check+failed" in response.headers["location"].lower()


def test_returned_inventory_ont_authorize_allows_active_candidate(
    db_session, monkeypatch
) -> None:
    from app.web.admin import network_olts_inventory

    olt = OLTDevice(name="Scope OLT", mgmt_ip="10.0.0.10", is_active=True)
    ont = OntUnit(serial_number="HWTC600AC29C", is_active=True)
    db_session.add_all([olt, ont])
    db_session.flush()
    candidate = OltAutofindCandidate(
        olt_id=olt.id,
        ont_unit_id=ont.id,
        fsp="0/2/1",
        serial_number="HWTC-600AC29C",
        serial_hex="48575443600AC29C",
        is_active=True,
    )
    db_session.add(candidate)
    db_session.commit()

    captured: dict[str, object] = {}

    def _fake_request(_db, command):
        captured["command"] = command
        return OntAuthorizationAdmission(True, True, "accepted")

    monkeypatch.setattr(
        network_olts_inventory,
        "request_ont_authorization",
        _fake_request,
    )
    monkeypatch.setattr(
        network_olts_inventory,
        "_authorization_detail_redirect_url",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        network_olts_inventory.web_admin_service,
        "get_current_user",
        lambda _request: {"name": "Alice Admin"},
    )

    request = _request()
    request.state.auth = {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "roles": [],
        "scopes": ["network:write"],
    }
    response = network_olts_inventory.olt_authorize_ont(
        request,
        str(olt.id),
        fsp="0/2/1",
        serial_number="HWTC-600AC29C",
        ont_id=str(ont.id),
        return_to="/admin/network/onts?view=unconfigured",
        preset_id="",
        db=db_session,
    )

    assert response.status_code == 303
    assert "Authorization+started" in response.headers["location"]
    assert captured["command"].ont_id == ont.id


def test_moved_ont_authorize_allows_active_candidate_with_previous_olt(
    db_session, monkeypatch
) -> None:
    from app.web.admin import network_olts_inventory

    previous_olt = OLTDevice(name="Previous OLT", mgmt_ip="10.0.0.9", is_active=True)
    target_olt = OLTDevice(name="Target OLT", mgmt_ip="10.0.0.10", is_active=True)
    db_session.add_all([previous_olt, target_olt])
    db_session.flush()
    ont = OntUnit(
        serial_number="HWTC2A73E384",
        vendor_serial_number="485754432A73E384",
        olt_device_id=previous_olt.id,
        board="0/1",
        port="5",
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    candidate = OltAutofindCandidate(
        olt_id=target_olt.id,
        ont_unit_id=ont.id,
        fsp="0/2/5",
        serial_number="HWTC-2A73E384",
        serial_hex="485754432A73E384",
        is_active=True,
    )
    db_session.add(candidate)
    db_session.commit()

    captured: dict[str, object] = {}

    def _fake_request(_db, command):
        captured["command"] = command
        return OntAuthorizationAdmission(True, True, "accepted")

    monkeypatch.setattr(
        network_olts_inventory,
        "request_ont_authorization",
        _fake_request,
    )
    monkeypatch.setattr(
        network_olts_inventory,
        "_authorization_detail_redirect_url",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        network_olts_inventory.web_admin_service,
        "get_current_user",
        lambda _request: {"name": "Alice Admin"},
    )

    request = _request()
    request.state.auth = {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "roles": [],
        "scopes": ["network:write"],
    }
    response = network_olts_inventory.olt_authorize_ont(
        request,
        str(target_olt.id),
        fsp="0/2/5",
        serial_number="HWTC-2A73E384",
        ont_id=str(ont.id),
        return_to="/admin/network/onts?view=unconfigured",
        preset_id="",
        db=db_session,
    )

    assert response.status_code == 303
    assert "Authorization+started" in response.headers["location"]
    assert captured["command"].ont_id == ont.id


def test_returned_inventory_hex_ont_authorize_allows_ascii_candidate(
    db_session, monkeypatch
) -> None:
    from app.web.admin import network_olts_inventory

    olt = OLTDevice(name="Scope OLT", mgmt_ip="10.0.0.10", is_active=True)
    ont = OntUnit(serial_number="48575443D7AC5310", is_active=True)
    db_session.add_all([olt, ont])
    db_session.flush()
    candidate = OltAutofindCandidate(
        olt_id=olt.id,
        ont_unit_id=ont.id,
        fsp="0/2/1",
        serial_number="HWTCD7AC5310",
        serial_hex="48575443D7AC5310",
        is_active=True,
    )
    db_session.add(candidate)
    db_session.commit()

    captured: dict[str, object] = {}

    def _fake_request(_db, command):
        captured["command"] = command
        return OntAuthorizationAdmission(True, True, "accepted")

    monkeypatch.setattr(
        network_olts_inventory,
        "request_ont_authorization",
        _fake_request,
    )
    monkeypatch.setattr(
        network_olts_inventory,
        "_authorization_detail_redirect_url",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        network_olts_inventory.web_admin_service,
        "get_current_user",
        lambda _request: {"name": "Alice Admin"},
    )

    request = _request()
    request.state.auth = {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "roles": [],
        "scopes": ["network:write"],
    }
    response = network_olts_inventory.olt_authorize_ont(
        request,
        str(olt.id),
        fsp="0/2/1",
        serial_number="HWTCD7AC5310",
        ont_id=str(ont.id),
        return_to="/admin/network/onts?view=unconfigured",
        preset_id="",
        db=db_session,
    )

    assert response.status_code == 303
    assert "Authorization+started" in response.headers["location"]
    assert captured["command"].ont_id == ont.id


def test_reseller_authorize_without_scoped_ont_is_rejected(monkeypatch) -> None:
    from app.web.admin import network_olts_inventory

    actor_id = uuid4()

    class FakeDb:
        def get(self, *_args):
            return SimpleNamespace(
                id=actor_id,
                user_type=UserType.reseller,
                reseller_id=uuid4(),
            )

    def _unexpected_queue(*_args, **_kwargs):
        raise AssertionError("unscoped reseller authorization must not authorize")

    monkeypatch.setattr(
        network_olts_inventory,
        "request_ont_authorization",
        _unexpected_queue,
    )

    request = _request()
    request.state.auth = {
        "principal_id": str(actor_id),
        "principal_type": "subscriber",
        "roles": [],
        "scopes": ["network:write"],
    }
    response = network_olts_inventory.olt_authorize_ont(
        request,
        str(uuid4()),
        fsp="0/1/1",
        serial_number="HWTC12345678",
        ont_id="",
        db=FakeDb(),
    )

    assert response.status_code == 303
    assert "authorization+scope+check+failed" in response.headers["location"].lower()


def test_authorize_without_request_auth_is_rejected(monkeypatch) -> None:
    from app.web.admin import network_olts_inventory

    def _unexpected_queue(*_args, **_kwargs):
        raise AssertionError("missing-auth authorization must not authorize")

    monkeypatch.setattr(
        network_olts_inventory,
        "request_ont_authorization",
        _unexpected_queue,
    )

    response = network_olts_inventory.olt_authorize_ont(
        _request(),
        str(uuid4()),
        fsp="0/1/1",
        serial_number="HWTC12345678",
        ont_id=str(uuid4()),
        db=SimpleNamespace(get=lambda *_args, **_kwargs: None),
    )

    assert response.status_code == 303
    assert "authorization+scope+check+failed" in response.headers["location"].lower()


def test_filter_manageable_ont_ids_requires_request_auth() -> None:
    from app.services.network.ont_scope import filter_manageable_ont_ids_from_request

    request = _request()

    scoped = filter_manageable_ont_ids_from_request(
        request,
        db=SimpleNamespace(),
        ont_ids=[str(uuid4()), str(uuid4())],
    )

    assert scoped == []
