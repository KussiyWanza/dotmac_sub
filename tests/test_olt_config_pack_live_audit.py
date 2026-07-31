from __future__ import annotations

from types import SimpleNamespace

from app.models.network import (
    OLTDevice,
    OltLineProfile,
    OltOnuTypeProfileMapping,
    OltServiceProfile,
)
from app.services.network.olt_config_pack_live_audit import (
    OltDependencyAuditScope,
    audit_olt_config_pack_live,
    extract_dba_profile_ids,
    parse_line_profile_detail,
    parse_tr069_profile_detail,
    suggest_compatible_line_profiles,
)

LINE_PROFILE_DETAIL = """
  Profile-ID          :1
  Profile-name        :SPL_1_Unlimited_3
  TR069 management    :Enable
  TR069 IP index      :0
  <T-CONT   0>          DBA Profile-ID:10
   <Gem Index 0>
  <T-CONT   1>          DBA Profile-ID:11
   <Gem Index 1>
  <T-CONT   2>          DBA Profile-ID:12
   <Gem Index 2>
"""


def test_parse_line_profile_detail_extracts_gems_and_tr069() -> None:
    detail = parse_line_profile_detail(LINE_PROFILE_DETAIL, profile_id=1)

    assert detail.profile_id == 1
    assert detail.gem_indexes == {0, 1, 2}
    assert detail.tr069_management_enabled is True
    assert detail.tr069_ip_index == 0


def test_parse_line_profile_detail_handles_missing_tr069() -> None:
    detail = parse_line_profile_detail(
        """
        Profile-ID          :2
        TR069 management    :Disable
        <Gem Index 1>
        """,
        profile_id=2,
    )

    assert detail.gem_indexes == {1}
    assert detail.tr069_management_enabled is False
    assert detail.tr069_ip_index is None


def test_parse_tr069_profile_detail_detects_missing_profile() -> None:
    detail = parse_tr069_profile_detail(
        "Failure: The profile does not exist",
        profile_id=2,
    )

    assert detail.exists is False


def test_parse_tr069_profile_detail_extracts_profile_fields() -> None:
    detail = parse_tr069_profile_detail(
        """
        Profile-ID          :2
        Profile-name        :GenieACS
        URL                 :http://acs.example/cwmp
        """,
        profile_id=2,
    )

    assert detail.exists is True
    assert detail.name == "GenieACS"
    assert detail.acs_url == "http://acs.example/cwmp"


def test_extract_dba_profile_ids_from_imported_line_profile() -> None:
    assert extract_dba_profile_ids(LINE_PROFILE_DETAIL) == {10, 11, 12}


def test_line_profile_suggestions_are_deprecated(db_session) -> None:
    olt = OLTDevice(name="Live Audit OLT", config_pack={"tr069_olt_profile_id": 2})
    db_session.add(olt)
    db_session.flush()

    ok, message, suggestions = suggest_compatible_line_profiles(db_session, str(olt.id))

    assert ok is False
    assert suggestions == []
    assert "Import OLT State" in message


def test_live_audit_validates_profile_dependencies(monkeypatch, db_session) -> None:
    olt = OLTDevice(
        name="Live Audit OLT",
        config_pack={
            "tr069_olt_profile_id": 2,
            "wan_config_profile_id": 0,
            "internet_traffic_table_inbound": 6,
        },
        supports_ont_wan_config=True,
        wan_provisioning_mode="omci_wan_config",
    )
    db_session.add(olt)
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfile(
                olt_id=olt.id,
                profile_id=40,
                name="HG8546M",
                raw_config="tcont 1 dba-profile-id 50",
            ),
            OltServiceProfile(olt_id=olt.id, profile_id=41, name="HG8546M"),
        ]
    )
    db_session.flush()
    db_session.add(
        OltOnuTypeProfileMapping(
            olt_id=olt.id,
            equipment_id="HG8546M",
            line_profile_id=40,
            service_profile_id=41,
            wan_config_profile_id=0,
        )
    )
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_tr069_server_profiles",
        lambda _olt: (
            True,
            "ok",
            [SimpleNamespace(profile_id=2, name="ACS", acs_url="")],
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_dba_profiles",
        lambda _olt: (True, "ok", [SimpleNamespace(profile_id=50)]),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_traffic_tables",
        lambda _olt: (True, "ok", [SimpleNamespace(index=6)]),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_wan_profiles",
        lambda _olt: (True, "ok", [SimpleNamespace(profile_id=0)]),
    )

    audit = audit_olt_config_pack_live(db_session, str(olt.id))

    assert audit.is_valid is True
    assert audit.observed["required_wan_config_profile_ids"] == [0]
    assert audit.observed["missing_wan_config_profile_ids"] == []
    assert audit.observed["required_dba_profile_ids"] == [50]


def test_management_only_audit_skips_customer_service_inventories(
    monkeypatch,
    db_session,
) -> None:
    olt = OLTDevice(
        name="Management-only Audit OLT",
        config_pack={
            "tr069_olt_profile_id": 2,
            "wan_config_profile_id": 9,
            "internet_traffic_table_inbound": 6,
        },
    )
    db_session.add(olt)
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfile(
                olt_id=olt.id,
                profile_id=40,
                name="HG8546M",
                raw_config="tcont 1 dba-profile-id 50",
            ),
            OltServiceProfile(olt_id=olt.id, profile_id=41, name="HG8546M"),
        ]
    )
    db_session.flush()
    db_session.add(
        OltOnuTypeProfileMapping(
            olt_id=olt.id,
            equipment_id="HG8546M",
            line_profile_id=40,
            service_profile_id=41,
            wan_config_profile_id=9,
        )
    )
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_tr069_server_profiles",
        lambda _olt: (
            True,
            "ok",
            [SimpleNamespace(profile_id=2, name="ACS", acs_url="")],
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_dba_profiles",
        lambda _olt: (True, "ok", [SimpleNamespace(profile_id=50)]),
    )

    def unexpected_customer_service_inventory(_olt):
        raise AssertionError("management-only audit read a customer service inventory")

    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_traffic_tables",
        unexpected_customer_service_inventory,
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_wan_profiles",
        unexpected_customer_service_inventory,
    )

    audit = audit_olt_config_pack_live(
        db_session,
        str(olt.id),
        scope=OltDependencyAuditScope.MANAGEMENT_ONLY,
    )

    assert audit.is_valid is True
    assert audit.observed["dependency_scope"] == "management_only"
    assert audit.observed["required_traffic_table_ids"] == {}
    assert audit.observed["required_wan_config_profile_ids"] == []


def test_live_audit_detects_missing_wan_profile_zero(monkeypatch, db_session) -> None:
    olt = OLTDevice(
        name="Live Audit OLT",
        config_pack={"tr069_olt_profile_id": 2, "wan_config_profile_id": 0},
        supports_ont_wan_config=True,
        wan_provisioning_mode="omci_wan_config",
    )
    db_session.add(olt)
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfile(olt_id=olt.id, profile_id=40, name="HG8546M"),
            OltServiceProfile(olt_id=olt.id, profile_id=41, name="HG8546M"),
        ]
    )
    db_session.flush()
    db_session.add(
        OltOnuTypeProfileMapping(
            olt_id=olt.id,
            equipment_id="HG8546M",
            line_profile_id=40,
            service_profile_id=41,
            wan_config_profile_id=0,
        )
    )
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_tr069_server_profiles",
        lambda _olt: (
            True,
            "ok",
            [SimpleNamespace(profile_id=2, name="ACS", acs_url="")],
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_dba_profiles",
        lambda _olt: (True, "ok", []),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_traffic_tables",
        lambda _olt: (True, "ok", []),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_wan_profiles",
        lambda _olt: (True, "ok", [SimpleNamespace(profile_id=5)]),
    )

    audit = audit_olt_config_pack_live(db_session, str(olt.id))

    assert audit.is_valid is False
    assert audit.observed["required_wan_config_profile_ids"] == [0]
    assert audit.observed["missing_wan_config_profile_ids"] == [0]
    assert "missing WAN config profile(s): 0" in audit.errors[-1]


def test_live_audit_allows_missing_wan_profile_zero_when_enabled(
    monkeypatch, db_session
) -> None:
    olt = OLTDevice(
        name="Live Audit OLT",
        config_pack={
            "tr069_olt_profile_id": 2,
            "wan_config_profile_id": 0,
            "allow_zero_wan_config_profile_id": True,
        },
        supports_ont_wan_config=True,
        wan_provisioning_mode="omci_wan_config",
    )
    db_session.add(olt)
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfile(olt_id=olt.id, profile_id=40, name="HG8546M"),
            OltServiceProfile(olt_id=olt.id, profile_id=41, name="HG8546M"),
        ]
    )
    db_session.flush()
    db_session.add(
        OltOnuTypeProfileMapping(
            olt_id=olt.id,
            equipment_id="HG8546M",
            line_profile_id=40,
            service_profile_id=41,
            wan_config_profile_id=0,
        )
    )
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_tr069_server_profiles",
        lambda _olt: (
            True,
            "ok",
            [SimpleNamespace(profile_id=2, name="ACS", acs_url="")],
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_dba_profiles",
        lambda _olt: (True, "ok", []),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_traffic_tables",
        lambda _olt: (True, "ok", []),
    )
    monkeypatch.setattr(
        "app.services.network.olt_config_pack_live_audit.get_wan_profiles",
        lambda _olt: (True, "ok", [SimpleNamespace(profile_id=5)]),
    )

    audit = audit_olt_config_pack_live(db_session, str(olt.id))

    assert audit.is_valid is True
    assert audit.observed["required_wan_config_profile_ids"] == [0]
    assert audit.observed["missing_wan_config_profile_ids"] == []
