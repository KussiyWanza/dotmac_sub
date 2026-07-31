from __future__ import annotations

from types import SimpleNamespace


def test_validate_olt_profile_dependencies_passes_with_valid_audit(monkeypatch) -> None:
    from app.services.network import olt_dependency_preflight

    olt_dependency_preflight._success_cache.clear()
    monkeypatch.setattr(
        olt_dependency_preflight,
        "audit_olt_config_pack_live",
        lambda *_args, **_kwargs: SimpleNamespace(
            is_valid=True,
            errors=[],
            to_dict=lambda: {"is_valid": True},
        ),
    )

    result = olt_dependency_preflight.validate_olt_profile_dependencies(
        SimpleNamespace(),
        olt_id="olt-1",
        operation="authorization",
    )

    assert result.success is True
    assert result.message == "OLT profile dependencies are valid."
    assert result.audit == {"is_valid": True}


def test_validate_olt_profile_dependencies_fails_with_compact_errors(
    monkeypatch,
) -> None:
    from app.services.network import olt_dependency_preflight

    olt_dependency_preflight._success_cache.clear()
    monkeypatch.setattr(
        olt_dependency_preflight,
        "audit_olt_config_pack_live",
        lambda *_args, **_kwargs: SimpleNamespace(
            is_valid=False,
            errors=["missing WAN config profile(s): 0", "missing DBA profile(s): 50"],
            to_dict=lambda: {"is_valid": False},
        ),
    )

    result = olt_dependency_preflight.validate_olt_profile_dependencies(
        SimpleNamespace(),
        olt_id="olt-1",
        operation="provisioning",
    )

    assert result.success is False
    assert result.audit == {"is_valid": False}
    assert result.errors == [
        "missing WAN config profile(s): 0",
        "missing DBA profile(s): 50",
    ]
    assert result.message == (
        "OLT provisioning dependency audit failed: "
        "missing WAN config profile(s): 0; missing DBA profile(s): 50"
    )


def test_validate_olt_profile_dependencies_uses_recent_success_cache(
    monkeypatch,
) -> None:
    from app.services.network import olt_dependency_preflight

    calls = {"count": 0}

    def _audit(*_args, **_kwargs):
        calls["count"] += 1
        return SimpleNamespace(
            is_valid=True,
            errors=[],
            to_dict=lambda: {"is_valid": True, "calls": calls["count"]},
        )

    monkeypatch.setattr(olt_dependency_preflight, "audit_olt_config_pack_live", _audit)
    olt_dependency_preflight._success_cache.clear()

    first = olt_dependency_preflight.validate_olt_profile_dependencies(
        SimpleNamespace(),
        olt_id="olt-1",
        operation="authorization",
    )
    second = olt_dependency_preflight.validate_olt_profile_dependencies(
        SimpleNamespace(),
        olt_id="olt-1",
        operation="authorization",
    )

    assert first.success is True
    assert second.success is True
    assert calls["count"] == 1
    assert second.audit == {"is_valid": True, "calls": 1}


def test_dependency_success_cache_isolated_by_scope(monkeypatch) -> None:
    from app.services.network import olt_dependency_preflight
    from app.services.network.olt_config_pack_live_audit import (
        OltDependencyAuditScope,
    )

    scopes = []

    def _audit(*_args, scope, **_kwargs):
        scopes.append(scope)
        return SimpleNamespace(
            is_valid=True,
            errors=[],
            to_dict=lambda: {"is_valid": True, "scope": scope.value},
        )

    monkeypatch.setattr(olt_dependency_preflight, "audit_olt_config_pack_live", _audit)
    olt_dependency_preflight._success_cache.clear()

    management = olt_dependency_preflight.validate_olt_profile_dependencies(
        SimpleNamespace(),
        olt_id="olt-1",
        operation="management-only commissioning",
        scope=OltDependencyAuditScope.MANAGEMENT_ONLY,
    )
    full = olt_dependency_preflight.validate_olt_profile_dependencies(
        SimpleNamespace(),
        olt_id="olt-1",
        operation="authorization",
    )

    assert management.audit == {"is_valid": True, "scope": "management_only"}
    assert full.audit == {"is_valid": True, "scope": "full"}
    assert scopes == [
        OltDependencyAuditScope.MANAGEMENT_ONLY,
        OltDependencyAuditScope.FULL,
    ]


def test_cached_only_dependency_validation_falls_back_to_live_audit_on_cache_miss(
    monkeypatch,
) -> None:
    from app.services.network import olt_dependency_preflight
    from app.services.web_network_ont_actions import config_setters

    calls = {"live": 0}

    monkeypatch.setattr(
        olt_dependency_preflight,
        "get_cached_olt_dependency_validation",
        lambda *_args, **_kwargs: None,
    )

    def _live_audit(*_args, **_kwargs):
        calls["live"] += 1
        return SimpleNamespace(
            success=False,
            message="OLT manual OLT write dependency audit failed: missing WAN profile",
            audit={"is_valid": False},
            errors=["missing WAN profile"],
        )

    monkeypatch.setattr(
        olt_dependency_preflight,
        "validate_olt_profile_dependencies",
        _live_audit,
    )

    result = config_setters._validate_olt_write_dependencies(
        SimpleNamespace(),
        SimpleNamespace(id="olt-1"),
        cached_only=True,
    )

    assert calls["live"] == 1
    assert result is not None
    assert result.success is False
    assert result.message == (
        "OLT manual OLT write dependency audit failed: missing WAN profile"
    )
    assert result.data == {
        "delivery_pending": False,
        "dependency_audit": {"is_valid": False},
    }
