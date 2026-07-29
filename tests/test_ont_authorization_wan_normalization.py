from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models.network import (
    IpBlock,
    IpPool,
    IPv4Address,
    IPVersion,
    OLTDevice,
    OntUnit,
)
from app.services.network import ont_authorization
from app.services.network.ont_desired_config import desired_config
from app.services.network.ont_management_ipam import allocate_ont_management_ip


def _allocate_management_ip_for_ont(
    db_session,
    *,
    ont_unit_id: str,
    olt_id: str,
) -> tuple[bool, str, str | None]:
    ont = db_session.get(OntUnit, ont_unit_id)
    olt = db_session.get(OLTDevice, olt_id)
    if ont is None:
        return False, "ONT not found.", None
    if olt is None:
        return False, "OLT not found.", None
    try:
        allocation = allocate_ont_management_ip(db_session, ont=ont, olt=olt)
    except ValueError as exc:
        return False, str(exc), None
    return (
        True,
        (
            f"ONT already has management IP {allocation.address}."
            if allocation.reused
            else f"Allocated management IP {allocation.address}."
        ),
        allocation.address,
    )


def test_authorization_applies_olt_baseline_but_not_followup_tasks_inline(
    db_session, monkeypatch
):
    """Authorization registers the ONT and applies OLT-side service plumbing."""
    from app.services.network.ont_provisioning.result import StepResult

    result = ont_authorization.AuthorizationWorkflowResult(
        success=True,
        message="ONT authorization completed.",
        status="success",
        ont_unit_id="ont-1",
        ont_id_on_olt=7,
        completed_authorization=True,
    )

    monkeypatch.setattr(
        ont_authorization,
        "authorize_autofind_ont",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.apply_authorization_baseline",
        lambda *args, **kwargs: StepResult(
            "authorization_baseline", True, "baseline ok"
        ),
    )
    response = ont_authorization.authorize_ont(
        db_session,
        "olt-1",
        "0/1/1",
        "HWTCBOOTSTRAP",
    )

    assert response.success is True
    assert response.completed_authorization is True
    assert response.partial_success is False
    assert response.message == "ONT authorization completed."
    assert [step.name for step in response.steps] == ["Apply Authorization Baseline"]


def test_management_ip_allocation_rejects_released_address_from_different_pool(
    db_session,
):
    """Allocator must not claim an IPv4 row that belongs to another pool."""
    olt = OLTDevice(name="OLT-Mgmt-Pool-Guard", is_active=True)
    ont = OntUnit(serial_number="HWTCPOOLGUARD", olt_device=olt, is_active=True)
    other_pool = IpPool(
        name="Other Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.201.0/24",
        gateway="172.16.201.1",
        is_active=True,
    )
    active_pool = IpPool(
        name="Active Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.201.0/24",
        gateway="172.16.201.1",
        is_active=True,
        olt_device=olt,
    )
    db_session.add_all([olt, ont, other_pool, active_pool])
    db_session.flush()
    db_session.add(
        IpBlock(pool_id=active_pool.id, cidr="172.16.201.0/30", is_active=True)
    )
    db_session.add(
        IPv4Address(
            address="172.16.201.2",
            pool_id=other_pool.id,
            is_reserved=False,
        )
    )
    olt.mgmt_ip_pool_id = active_pool.id
    db_session.commit()

    ok, message, allocated_ip = _allocate_management_ip_for_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
    )

    assert ok is False
    assert allocated_ip is None
    assert "different pool" in message


def test_management_ip_allocation_fails_without_olt_pool(db_session):
    """Authorization cannot guarantee ACS reachability without an OLT mgmt pool."""
    olt = OLTDevice(name="OLT-No-Mgmt-Pool", is_active=True)
    ont = OntUnit(serial_number="HWTCNOPool01", olt_device=olt, is_active=True)
    db_session.add_all([olt, ont])
    db_session.commit()

    ok, message, allocated_ip = _allocate_management_ip_for_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
    )

    assert ok is False
    assert allocated_ip is None
    assert "No management IP pool configured" in message


def test_management_ip_allocation_reuses_existing_ip_only_when_in_current_pool(
    db_session,
):
    """Existing management IPs are valid only when they belong to the OLT pool."""
    olt = OLTDevice(name="OLT-Existing-Mgmt-Pool", is_active=True)
    ont = OntUnit(serial_number="HWTCVALIDMGMT", olt_device=olt, is_active=True)
    pool = IpPool(
        name="Existing Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.202.0/24",
        gateway="172.16.202.1",
        is_active=True,
        olt_device=olt,
    )
    db_session.add_all([olt, ont, pool])
    db_session.flush()
    db_session.add(IpBlock(pool_id=pool.id, cidr="172.16.202.0/30", is_active=True))
    db_session.add(
        IPv4Address(
            address="172.16.202.2",
            pool_id=pool.id,
            is_reserved=True,
            notes=f"ont:{ont.id}",
            ont_unit_id=ont.id,
            allocation_type="management",
        )
    )
    olt.mgmt_ip_pool_id = pool.id
    db_session.commit()

    ok, message, allocated_ip = _allocate_management_ip_for_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
    )

    assert ok is True
    assert allocated_ip == "172.16.202.2"
    assert "already has management IP" in message
    record = db_session.query(IPv4Address).filter_by(address="172.16.202.2").one()
    assert record.pool_id == pool.id
    assert record.ont_unit_id == ont.id
    assert record.allocation_type == "management"


def test_management_ip_allocation_uses_valid_cached_next_ip(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="OLT-Cached-Mgmt-Pool", is_active=True)
    ont = OntUnit(serial_number="HWTCCACHEMGMT", olt_device=olt, is_active=True)
    pool = IpPool(
        name="Cached Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.203.0/24",
        gateway="172.16.203.1",
        is_active=True,
        olt_device=olt,
        next_available_ip="172.16.203.2",
        available_count=2,
    )
    db_session.add_all([olt, ont, pool])
    db_session.flush()
    db_session.add(IpBlock(pool_id=pool.id, cidr="172.16.203.0/29", is_active=True))
    olt.mgmt_ip_pool_id = pool.id
    db_session.commit()

    ok, message, allocated_ip = _allocate_management_ip_for_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
    )

    assert ok is True
    assert allocated_ip == "172.16.203.2"
    assert "Allocated management IP" in message
    assert ont.assignments == []
    assert desired_config(ont)["management"] == {
        "ip_address": "172.16.203.2",
        "ip_mode": "static_ip",
        "subnet": "255.255.255.0",
        "gateway": "172.16.203.1",
    }
    db_session.refresh(pool)
    assert pool.next_available_ip == "172.16.203.3"
    # /29 block has 6 usable hosts (.1-.6), minus gateway (.1) and allocated (.2) = 4 remaining
    assert pool.available_count == 4


def test_management_ip_allocation_refreshes_stale_cached_next_ip(
    db_session,
):
    olt = OLTDevice(name="OLT-Stale-Cache-Mgmt-Pool", is_active=True)
    ont = OntUnit(serial_number="HWTCSTALECACHE", olt_device=olt, is_active=True)
    pool = IpPool(
        name="Stale Cache Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.204.0/24",
        gateway="172.16.204.1",
        is_active=True,
        olt_device=olt,
        next_available_ip="172.16.204.2",
        available_count=2,
    )
    db_session.add_all([olt, ont, pool])
    db_session.flush()
    db_session.add(IpBlock(pool_id=pool.id, cidr="172.16.204.0/29", is_active=True))
    db_session.add(
        IPv4Address(
            address="172.16.204.2",
            pool_id=pool.id,
            is_reserved=True,
            notes="held",
        )
    )
    olt.mgmt_ip_pool_id = pool.id
    db_session.commit()

    ok, _message, allocated_ip = _allocate_management_ip_for_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
    )

    assert ok is True
    assert allocated_ip == "172.16.204.3"
    db_session.refresh(pool)
    assert pool.next_available_ip == "172.16.204.4"


def test_management_ip_allocation_replaces_stale_existing_ip_from_wrong_pool(
    db_session,
):
    """Stale assignment IPs do not bypass allocation from the current OLT pool."""
    olt = OLTDevice(name="OLT-Stale-Mgmt-Pool", is_active=True)
    ont = OntUnit(serial_number="HWTCSTALEMGMT", olt_device=olt, is_active=True)
    old_pool = IpPool(
        name="Old Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.201.0/24",
        gateway="172.16.201.1",
        is_active=True,
    )
    current_pool = IpPool(
        name="Current Mgmt Pool",
        ip_version=IPVersion.ipv4,
        cidr="172.16.202.0/24",
        gateway="172.16.202.1",
        is_active=True,
        olt_device=olt,
    )
    db_session.add_all([olt, ont, old_pool, current_pool])
    db_session.flush()
    db_session.add(
        IpBlock(pool_id=current_pool.id, cidr="172.16.202.0/30", is_active=True)
    )
    stale_record = IPv4Address(
        address="172.16.201.2",
        pool_id=old_pool.id,
        is_reserved=True,
        notes=f"ont:{ont.id}",
        ont_unit_id=ont.id,
        allocation_type="management",
    )
    db_session.add(stale_record)
    olt.mgmt_ip_pool_id = current_pool.id
    db_session.commit()

    ok, message, allocated_ip = _allocate_management_ip_for_ont(
        db_session,
        ont_unit_id=str(ont.id),
        olt_id=str(olt.id),
    )

    assert ok is True
    assert allocated_ip == "172.16.202.2"
    assert "Allocated management IP" in message
    db_session.refresh(stale_record)
    assert stale_record.is_reserved is False
    assert stale_record.ont_unit_id is None
    assert stale_record.allocation_type is None
    assert desired_config(ont)["management"]["ip_address"] == "172.16.202.2"
    assert ont.assignments == []


def test_authorization_cleans_stale_registration_before_retry(
    db_session,
    monkeypatch,
):
    """Normal authorization handles serial-already-registered on a stale FSP."""
    olt = OLTDevice(name="OLT-Stale-Registration", is_active=True)
    db_session.add(olt)
    db_session.commit()

    calls: list[tuple[str, object]] = []
    existing = SimpleNamespace(fsp="0/1/5", onu_id=3)

    class FakeAdapter:
        def find_ont_by_serial(self, serial_number):
            calls.append(("find", serial_number))
            return SimpleNamespace(
                success=True,
                message="Found existing registration.",
                data={"registration": existing},
            )

        def deauthorize_ont(self, fsp, ont_id):
            calls.append(("deauthorize", fsp, ont_id))
            return SimpleNamespace(
                success=True, message="Deleted existing registration."
            )

        def authorize_ont(
            self,
            fsp,
            serial_number,
            *,
            line_profile_id=None,
            service_profile_id=None,
            description=None,
        ):
            calls.append(("authorize", fsp, serial_number))
            if len([call for call in calls if call[0] == "authorize"]) == 1:
                return SimpleNamespace(
                    success=False,
                    message="SN already exists",
                    ont_id=None,
                )
            return SimpleNamespace(
                success=True,
                message="Authorized on requested port.",
                ont_id=7,
            )

    monkeypatch.setattr(
        ont_authorization,
        "_validate_authorization_dependencies",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda _olt: FakeAdapter(),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack.validate_config_pack_comprehensive",
        lambda db, olt_id: SimpleNamespace(is_valid=True),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack.get_validation_summary",
        lambda validation: "Config pack is complete and ready for provisioning",
    )
    monkeypatch.setattr(
        "app.services.network.olt_profile_resolution.resolve_authorization_profiles_from_import",
        lambda db, olt, *, equipment_id=None: (
            True,
            "Using OLT authorization profiles.",
            SimpleNamespace(
                line_profile_id=10,
                service_profile_id=20,
                message="Using OLT authorization profiles.",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_write_reconciliation.verify_ont_absent",
        lambda *args, **kwargs: SimpleNamespace(
            success=True,
            message="Verified ONT registration is absent on the OLT.",
        ),
    )

    result = ont_authorization.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/6",
        "HWTCSTALE123",
    )

    assert result.success is True
    assert result.ont_id_on_olt == 7
    assert result.completed_authorization is True
    assert ("deauthorize", "0/1/5", 3) in calls
    assert calls.count(("authorize", "0/1/6", "HWTCSTALE123")) == 2
    assert "Removed existing ONT registration" in result.steps[-1].message


def test_authorization_reports_partial_failure_when_local_record_setup_fails(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="OLT-Partial-Local", is_active=True)
    db_session.add(olt)
    db_session.commit()

    class FakeAdapter:
        def authorize_ont(
            self,
            fsp,
            serial_number,
            *,
            line_profile_id=None,
            service_profile_id=None,
            description=None,
        ):
            return SimpleNamespace(
                success=True,
                message="Authorized.",
                ont_id=7,
            )

    monkeypatch.setattr(
        ont_authorization,
        "_validate_authorization_dependencies",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda _olt: FakeAdapter(),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack.validate_config_pack_comprehensive",
        lambda db, olt_id: SimpleNamespace(is_valid=True),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack.get_validation_summary",
        lambda validation: "Config pack is complete and ready for provisioning",
    )
    monkeypatch.setattr(
        "app.services.network.olt_profile_resolution.resolve_authorization_profiles_from_import",
        lambda db, olt, *, equipment_id=None: (
            True,
            "Using OLT authorization profiles.",
            SimpleNamespace(
                line_profile_id=10,
                service_profile_id=20,
                message="Using OLT authorization profiles.",
            ),
        ),
    )
    monkeypatch.setattr(
        ont_authorization,
        "create_or_find_ont_for_authorized_serial",
        lambda *args, **kwargs: (None, "Failed to create ONT record."),
    )

    result = ont_authorization.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/6",
        "HWTCPARTIAL1",
    )

    assert result.success is False
    assert result.completed_authorization is True
    assert result.partial_success is True
    assert result.status == "error"
    assert result.local_inventory_failed is True
    assert result.message.startswith(
        "OLT authorization succeeded; local inventory failed"
    )
    assert "Failed to create ONT record." in result.message


def test_authorization_links_assignment_before_reporting_success(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="OLT-Link-Assignment", is_active=True)
    db_session.add(olt)
    db_session.commit()
    calls = {}

    class FakeAdapter:
        def authorize_ont(
            self,
            fsp,
            serial_number,
            *,
            line_profile_id=None,
            service_profile_id=None,
            description=None,
        ):
            return SimpleNamespace(
                success=True,
                message="Authorized.",
                ont_id=7,
            )

    monkeypatch.setattr(
        ont_authorization,
        "_validate_authorization_dependencies",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda _olt: FakeAdapter(),
    )
    monkeypatch.setattr(
        "app.services.network.olt_profile_resolution.resolve_authorization_profiles_from_import",
        lambda db, olt, *, equipment_id=None: (
            True,
            "Using OLT authorization profiles.",
            SimpleNamespace(
                line_profile_id=10,
                service_profile_id=20,
                message="Using OLT authorization profiles.",
            ),
        ),
    )
    monkeypatch.setattr(
        ont_authorization,
        "create_or_find_ont_for_authorized_serial",
        lambda *args, **kwargs: ("ont-1", "Using existing ONT record."),
    )

    def fake_ensure(db, *, ont_unit_id, olt_id, fsp):
        calls["ensure"] = (ont_unit_id, olt_id, fsp)
        return True, "Linked ONT to PON port 0/1/6."

    monkeypatch.setattr(
        ont_authorization,
        "record_topology_observation_for_authorized_ont",
        fake_ensure,
    )

    result = ont_authorization.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/6",
        "HWTCLINKOK1",
    )

    assert result.success is True
    assert result.partial_success is False
    assert calls["ensure"] == ("ont-1", str(olt.id), "0/1/6")
    assert "Linked ONT to PON port 0/1/6." in result.steps[-1].message


def test_authorization_reports_partial_failure_when_assignment_link_fails(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="OLT-Link-Fails", is_active=True)
    db_session.add(olt)
    db_session.commit()

    class FakeAdapter:
        def authorize_ont(
            self,
            fsp,
            serial_number,
            *,
            line_profile_id=None,
            service_profile_id=None,
            description=None,
        ):
            return SimpleNamespace(
                success=True,
                message="Authorized.",
                ont_id=7,
            )

    monkeypatch.setattr(
        ont_authorization,
        "_validate_authorization_dependencies",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda _olt: FakeAdapter(),
    )
    monkeypatch.setattr(
        "app.services.network.olt_profile_resolution.resolve_authorization_profiles_from_import",
        lambda db, olt, *, equipment_id=None: (
            True,
            "Using OLT authorization profiles.",
            SimpleNamespace(
                line_profile_id=10,
                service_profile_id=20,
                message="Using OLT authorization profiles.",
            ),
        ),
    )
    monkeypatch.setattr(
        ont_authorization,
        "create_or_find_ont_for_authorized_serial",
        lambda *args, **kwargs: ("ont-1", "Using existing ONT record."),
    )
    monkeypatch.setattr(
        ont_authorization,
        "record_topology_observation_for_authorized_ont",
        lambda *args, **kwargs: (False, "Invalid OLT F/S/P for assignment."),
    )

    result = ont_authorization.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/6",
        "HWTCLINKFAIL1",
    )

    assert result.success is False
    assert result.completed_authorization is True
    assert result.partial_success is True
    assert result.status == "error"
    assert result.local_inventory_failed is True
    assert result.message.startswith(
        "OLT authorization succeeded; local inventory failed"
    )
    assert "Invalid OLT F/S/P for assignment." in result.message


def test_authorization_fails_before_adapter_when_dependency_audit_fails(
    db_session,
    monkeypatch,
):
    olt = OLTDevice(name="OLT-Audit-Fail", is_active=True)
    db_session.add(olt)
    db_session.commit()

    adapter_factory = MagicMock()
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        adapter_factory,
    )
    monkeypatch.setattr(
        ont_authorization,
        "_validate_authorization_dependencies",
        lambda *args, **kwargs: (
            "OLT authorization dependency audit failed: missing WAN config profile(s): 0"
        ),
    )

    result = ont_authorization.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/6",
        "HWTCAUDITFAIL",
        force_reauthorize=True,
    )

    assert result.success is False
    assert result.completed_authorization is False
    assert "dependency audit failed" in result.message
    assert result.steps[0].name == "Validate OLT Profile Dependencies"
    adapter_factory.assert_not_called()


def test_authorization_ignores_explicit_foundation_failures(db_session, monkeypatch):
    """A failed OLT baseline is reported as partial authorization."""
    from app.services.network.ont_provisioning.result import StepResult

    result = ont_authorization.AuthorizationWorkflowResult(
        success=True,
        message="ONT authorization completed.",
        status="success",
        ont_unit_id="ont-1",
        ont_id_on_olt=7,
        completed_authorization=True,
    )

    monkeypatch.setattr(
        ont_authorization,
        "authorize_autofind_ont",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.apply_authorization_baseline",
        lambda *args, **kwargs: StepResult(
            "authorization_baseline", False, "OLT baseline failed"
        ),
    )
    response = ont_authorization.authorize_ont(
        db_session,
        "olt-1",
        "0/1/1",
        "HWTCWARNFOLLOWUP",
    )

    assert response.success is True
    assert response.completed_authorization is True
    assert response.partial_success is True
    assert response.baseline_applied is False
    assert response.status == "warning"
    assert response.message == (
        "ONT authorized, but OLT service baseline failed: OLT baseline failed"
    )
    assert [step.name for step in response.steps] == ["Apply Authorization Baseline"]


def test_authorization_duration_includes_olt_baseline_work(db_session, monkeypatch):
    """The synchronous result duration covers authorization and baseline work."""
    from app.services.network.ont_provisioning.result import StepResult

    result = ont_authorization.AuthorizationWorkflowResult(
        success=True,
        message="ONT authorization completed.",
        status="success",
        ont_unit_id="ont-1",
        ont_id_on_olt=7,
        completed_authorization=True,
        duration_ms=1,
    )
    ticks = iter([10.0] * 11 + [15.0])

    monkeypatch.setattr(ont_authorization, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        ont_authorization,
        "authorize_autofind_ont",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.apply_authorization_baseline",
        lambda *args, **kwargs: StepResult(
            "authorization_baseline", True, "baseline ok"
        ),
    )
    response = ont_authorization.authorize_ont(
        db_session,
        "olt-1",
        "0/1/1",
        "HWTCDURATION",
    )

    assert response.duration_ms == 5000
    assert [step.name for step in response.steps] == ["Apply Authorization Baseline"]
    assert [phase["phase"] for phase in response.phase_timings] == [
        "core_authorization",
        "post_authorization_commit",
        "authorization_baseline",
        "post_baseline_commit",
        "audit",
    ]
