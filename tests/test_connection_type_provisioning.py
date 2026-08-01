"""Tests for connection-type-specific provisioning logic."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.models.catalog import (
    ConnectionType,
    NasConnectionRule,
    NasDevice,
    NasVendor,
    RadiusProfile,
    Subscription,
)
from app.models.subscriber import SubscriberStatus
from app.services.connection_type_provisioning import (
    _append_additional_routes,
    _mikrotik_commands,
    _rule_matches,
    build_radius_reply_attributes,
    resolve_connection_type,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_subscription():
    sub = MagicMock(spec=Subscription)
    sub.id = uuid4()
    sub.subscriber_id = uuid4()
    sub.offer_id = uuid4()
    sub.radius_profile_id = None
    sub.provisioning_nas_device_id = None
    sub.login = "test_user"
    sub.ipv4_address = "10.0.0.100"
    sub.ipv6_address = None
    sub.mac_address = "AA:BB:CC:DD:EE:FF"
    return sub


@pytest.fixture
def mock_profile():
    profile = MagicMock(spec=RadiusProfile)
    profile.id = uuid4()
    profile.name = "50Mbps-Down"
    profile.code = "50M"
    profile.connection_type = None
    profile.download_speed = 50000
    profile.upload_speed = 25000
    profile.burst_download = None
    profile.burst_upload = None
    profile.burst_threshold = None
    profile.burst_time = None
    profile.ip_pool_name = "pool-residential"
    profile.ipv6_pool_name = None
    profile.session_timeout = 86400
    profile.idle_timeout = 600
    profile.simultaneous_use = 1
    profile.vlan_id = None
    profile.inner_vlan_id = None
    profile.mikrotik_rate_limit = None
    profile.mikrotik_address_list = None
    return profile


@pytest.fixture
def mock_nas_device():
    device = MagicMock(spec=NasDevice)
    device.id = uuid4()
    device.vendor = NasVendor.mikrotik
    device.default_connection_type = ConnectionType.pppoe
    device.supported_connection_types = ["pppoe", "dhcp"]
    return device


# ---------------------------------------------------------------------------
# resolve_connection_type
# ---------------------------------------------------------------------------


class TestResolveConnectionType:
    def test_defaults_to_pppoe(self, mock_subscription):
        db = MagicMock()
        db.get.return_value = None
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        result = resolve_connection_type(db, mock_subscription)
        assert result == ConnectionType.pppoe

    def test_uses_profile_connection_type(self, mock_subscription, mock_profile):
        db = MagicMock()
        mock_profile.connection_type = ConnectionType.ipoe
        mock_subscription.radius_profile_id = mock_profile.id
        db.get.return_value = mock_profile
        result = resolve_connection_type(db, mock_subscription)
        assert result == ConnectionType.ipoe

    def test_uses_nas_default(self, mock_subscription, mock_nas_device):
        db = MagicMock()
        db.get.return_value = mock_nas_device
        mock_nas_device.default_connection_type = ConnectionType.dhcp
        mock_subscription.provisioning_nas_device_id = mock_nas_device.id
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        result = resolve_connection_type(db, mock_subscription)
        assert result == ConnectionType.dhcp


# ---------------------------------------------------------------------------
# _rule_matches
# ---------------------------------------------------------------------------


class TestRuleMatches:
    def test_no_expression_matches_all(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = None
        assert _rule_matches(rule, mock_subscription) is True

    def test_wildcard_matches_all(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = "*"
        assert _rule_matches(rule, mock_subscription) is True

    def test_login_prefix_match(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = "login:test_*"
        assert _rule_matches(rule, mock_subscription) is True

    def test_login_prefix_no_match(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = "login:other_*"
        assert _rule_matches(rule, mock_subscription) is False

    def test_mac_prefix_match(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = "mac:AA:BB:*"
        assert _rule_matches(rule, mock_subscription) is True

    def test_mac_exact_match(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = "mac:AA:BB:CC:DD:EE:FF"
        assert _rule_matches(rule, mock_subscription) is True

    def test_mac_no_match(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = "mac:11:22:*"
        assert _rule_matches(rule, mock_subscription) is False


# ---------------------------------------------------------------------------
# build_radius_reply_attributes
# ---------------------------------------------------------------------------


class TestBuildRadiusReplyAttributes:
    def test_pppoe_attributes(self, mock_subscription, mock_profile):
        db = MagicMock()
        db.get.return_value = mock_profile
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter.return_value.all.return_value = []
        mock_subscription.radius_profile_id = mock_profile.id
        attrs = build_radius_reply_attributes(
            db, mock_subscription, profile=mock_profile
        )
        attr_names = [a["attribute"] for a in attrs]
        assert "Service-Type" in attr_names
        assert "Framed-Protocol" in attr_names
        assert "Framed-Pool" in attr_names
        assert "Session-Timeout" in attr_names
        assert "Framed-IP-Address" in attr_names
        rate_limit = next(
            attr["value"]
            for attr in attrs
            if attr["attribute"] == "Mikrotik-Rate-Limit"
        )
        assert rate_limit == "50000k/25000k"

    def test_dhcp_attributes(self, mock_subscription, mock_profile):
        db = MagicMock()
        mock_profile.connection_type = ConnectionType.dhcp
        mock_subscription.radius_profile_id = mock_profile.id
        db.get.return_value = mock_profile
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter.return_value.all.return_value = []
        attrs = build_radius_reply_attributes(
            db, mock_subscription, profile=mock_profile
        )
        attr_names = [a["attribute"] for a in attrs]
        assert "Calling-Station-Id" in attr_names
        assert "Framed-Protocol" not in attr_names

    def test_ipoe_attributes(self, mock_subscription, mock_profile):
        db = MagicMock()
        mock_profile.connection_type = ConnectionType.ipoe
        mock_profile.vlan_id = 100
        mock_profile.inner_vlan_id = None
        mock_subscription.radius_profile_id = mock_profile.id
        db.get.return_value = mock_profile
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.filter.return_value.filter.return_value.first.return_value = None
        attrs = build_radius_reply_attributes(
            db, mock_subscription, profile=mock_profile
        )
        attr_names = [a["attribute"] for a in attrs]
        assert "NAS-Port-Type" in attr_names
        assert "Tunnel-Type" in attr_names
        assert "Tunnel-Private-Group-Id" in attr_names

    def test_hotspot_attributes(self, mock_subscription, mock_profile):
        db = MagicMock()
        mock_profile.connection_type = ConnectionType.hotspot
        mock_subscription.radius_profile_id = mock_profile.id
        db.get.return_value = mock_profile
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter.return_value.all.return_value = []
        attrs = build_radius_reply_attributes(
            db, mock_subscription, profile=mock_profile
        )
        attr_names = [a["attribute"] for a in attrs]
        svc_type = next(a for a in attrs if a["attribute"] == "Service-Type")
        assert svc_type["value"] == "Login-User"
        assert "Mikrotik-Group" in attr_names

    def test_static_attributes(self, mock_subscription, mock_profile):
        db = MagicMock()
        mock_profile.connection_type = ConnectionType.static
        mock_subscription.radius_profile_id = mock_profile.id
        mock_subscription.ipv6_address = "2001:db8::1"
        db.get.return_value = mock_profile
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter.return_value.all.return_value = []
        attrs = build_radius_reply_attributes(
            db, mock_subscription, profile=mock_profile
        )
        attr_names = [a["attribute"] for a in attrs]
        assert "Framed-IP-Address" in attr_names
        assert "Framed-IPv6-Prefix" in attr_names


class TestFramedIpParity:
    """Writer-#2 parity: never emit a stale/empty/0.0.0.0 Framed-IP — fall back to
    the active IPAM assignment like the authoritative sweep does."""

    @staticmethod
    def _attrs(value):
        return [
            {"attribute": "Service-Type", "op": ":=", "value": "Framed-User"},
            {"attribute": "Framed-IP-Address", "op": ":=", "value": value},
        ]

    def test_valid_column_kept_without_db_lookup(self, mock_subscription):
        from app.services.connection_type_provisioning import _ensure_framed_ip

        db = MagicMock()
        mock_subscription.ipv4_address = "10.0.0.100"
        attrs = self._attrs("10.0.0.100")
        _ensure_framed_ip(db, attrs, mock_subscription)
        db.execute.assert_not_called()  # no fallback query when the column is usable
        framed = [a for a in attrs if a["attribute"] == "Framed-IP-Address"]
        assert [a["value"] for a in framed] == ["10.0.0.100"]

    def test_zero_address_replaced_by_ipam_fallback(self, mock_subscription):
        from app.services.connection_type_provisioning import _ensure_framed_ip

        db = MagicMock()
        db.execute.return_value.scalar.return_value = "10.9.9.9"
        mock_subscription.ipv4_address = "0.0.0.0"
        attrs = self._attrs("0.0.0.0")
        _ensure_framed_ip(db, attrs, mock_subscription)
        framed = [a for a in attrs if a["attribute"] == "Framed-IP-Address"]
        assert [a["value"] for a in framed] == ["10.9.9.9"]

    def test_empty_column_injects_fallback(self, mock_subscription):
        from app.services.connection_type_provisioning import _ensure_framed_ip

        db = MagicMock()
        db.execute.return_value.scalar.return_value = "10.9.9.9"
        mock_subscription.ipv4_address = None
        attrs = [{"attribute": "Service-Type", "op": ":=", "value": "Framed-User"}]
        _ensure_framed_ip(db, attrs, mock_subscription)
        assert {
            "attribute": "Framed-IP-Address",
            "op": ":=",
            "value": "10.9.9.9",
        } in attrs

    def test_no_ip_anywhere_drops_zero_address(self, mock_subscription):
        from app.services.connection_type_provisioning import _ensure_framed_ip

        db = MagicMock()
        db.execute.return_value.scalar.return_value = None
        mock_subscription.ipv4_address = "0.0.0.0"
        attrs = self._attrs("0.0.0.0")
        _ensure_framed_ip(db, attrs, mock_subscription)
        assert not [a for a in attrs if a["attribute"] == "Framed-IP-Address"]

    def test_offer_rate_limit_fallback(self, mock_subscription):
        from types import SimpleNamespace

        from app.services.connection_type_provisioning import (
            _offer_rate_limit_fallback,
        )

        db = MagicMock()
        db.get.return_value = SimpleNamespace(
            speed_download_mbps=50, speed_upload_mbps=25
        )
        assert _offer_rate_limit_fallback(db, mock_subscription) == "50M/25M"

        db.get.return_value = None
        assert _offer_rate_limit_fallback(db, mock_subscription) is None

    def test_build_emits_acct_interim_interval(self, mock_subscription, mock_profile):
        db = MagicMock()
        db.get.return_value = mock_profile
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter.return_value.all.return_value = []
        mock_subscription.radius_profile_id = mock_profile.id
        attrs = build_radius_reply_attributes(
            db, mock_subscription, profile=mock_profile
        )
        assert any(a["attribute"] == "Acct-Interim-Interval" for a in attrs)


# ---------------------------------------------------------------------------
# _append_additional_routes
# ---------------------------------------------------------------------------


def _route(cidr, metric=1):
    """A stand-in for a SubscriberAdditionalRoute row (is_active filtering
    happens in the query, so the helper only carries what the builder reads)."""
    r = MagicMock()
    r.cidr = cidr
    r.metric = metric
    return r


def _active_subscription(ipv4="10.0.0.100", status=SubscriberStatus.active):
    sub = MagicMock(spec=Subscription)
    sub.subscriber_id = uuid4()
    sub.ipv4_address = ipv4
    subscriber = MagicMock()
    subscriber.status = status
    sub.subscriber = subscriber
    return sub


def _db_returning_routes(routes):
    """db.query(...).filter(...).all() -> routes (single .filter, two conditions)."""
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = routes
    return db


class TestAppendAdditionalRoutes:
    def test_active_subscriber_emits_one_framed_route_per_block(self):
        sub = _active_subscription()
        db = _db_returning_routes(
            [_route("203.0.113.8/29"), _route("198.51.100.0/30", metric=2)]
        )
        attrs: list[dict[str, str]] = []
        _append_additional_routes(db, attrs, sub)

        routes = [a for a in attrs if a["attribute"] == "Framed-Route"]
        # gateway 0.0.0.0 (via session), op += so multiple blocks coexist
        assert {r["value"] for r in routes} == {
            "203.0.113.8/29 0.0.0.0 1",
            "198.51.100.0/30 0.0.0.0 2",
        }
        assert all(r["op"] == "+=" for r in routes)

    def test_blocked_subscriber_emits_nothing(self):
        sub = _active_subscription(status=SubscriberStatus.blocked)
        db = _db_returning_routes([_route("203.0.113.8/29")])
        attrs: list[dict[str, str]] = []
        _append_additional_routes(db, attrs, sub)
        assert attrs == []

    def test_disabled_subscriber_emits_nothing(self):
        sub = _active_subscription(status=SubscriberStatus.disabled)
        db = _db_returning_routes([_route("203.0.113.8/29")])
        attrs: list[dict[str, str]] = []
        _append_additional_routes(db, attrs, sub)
        assert attrs == []

    def test_primary_host_route_is_skipped(self):
        # A row equal to the primary /32 must never become a route (defence
        # in depth against a mis-tagged backfill row).
        sub = _active_subscription(ipv4="203.0.113.5")
        db = _db_returning_routes([_route("203.0.113.5/32"), _route("203.0.113.8/29")])
        attrs: list[dict[str, str]] = []
        _append_additional_routes(db, attrs, sub)
        routes = [a for a in attrs if a["attribute"] == "Framed-Route"]
        assert len(routes) == 1
        assert routes[0]["value"] == "203.0.113.8/29 0.0.0.0 1"

    def test_duplicate_cidrs_are_deduped(self):
        sub = _active_subscription()
        db = _db_returning_routes([_route("203.0.113.8/29"), _route("203.0.113.8/29")])
        attrs: list[dict[str, str]] = []
        _append_additional_routes(db, attrs, sub)
        routes = [a for a in attrs if a["attribute"] == "Framed-Route"]
        assert len(routes) == 1

    def test_metric_defaults_to_one_when_missing(self):
        sub = _active_subscription()
        db = _db_returning_routes([_route("203.0.113.8/29", metric=None)])
        attrs: list[dict[str, str]] = []
        _append_additional_routes(db, attrs, sub)
        assert attrs[0]["value"] == "203.0.113.8/29 0.0.0.0 1"

    def test_no_subscriber_id_is_a_noop(self):
        sub = MagicMock(spec=Subscription)
        sub.subscriber_id = None
        attrs: list[dict[str, str]] = []
        _append_additional_routes(MagicMock(), attrs, sub)
        assert attrs == []

    def test_integration_route_coexists_with_primary_ip(
        self, mock_subscription, mock_profile
    ):
        from app.models.network import SubscriberAdditionalRoute

        def query_dispatch(model):
            # Distinct query mock per model so the routes query and the
            # custom-attribute query don't share a mock chain.
            qm = MagicMock()
            qm.filter.return_value.all.return_value = []
            qm.filter.return_value.filter.return_value.all.return_value = []
            qm.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
            qm.filter.return_value.filter.return_value.first.return_value = None
            if model is SubscriberAdditionalRoute:
                qm.filter.return_value.all.return_value = [_route("203.0.113.8/29")]
            return qm

        db = MagicMock()
        db.get.return_value = mock_profile
        db.query.side_effect = query_dispatch
        mock_subscription.radius_profile_id = mock_profile.id
        subscriber = MagicMock()
        subscriber.status = SubscriberStatus.active
        mock_subscription.subscriber = subscriber

        attrs = build_radius_reply_attributes(
            db, mock_subscription, profile=mock_profile
        )
        names = [a["attribute"] for a in attrs]
        assert "Framed-IP-Address" in names
        assert "Framed-Route" in names


# ---------------------------------------------------------------------------
# _mikrotik_commands
# ---------------------------------------------------------------------------


class TestMikrotikCommands:
    @pytest.mark.parametrize(
        "action", ["create", "delete", "suspend", "unsuspend", "change_ip"]
    )
    def test_pppoe_emits_no_local_secret_command(
        self, mock_subscription, mock_profile, action
    ):
        """The NAS carries no per-customer PPPoE record, for any action.

        A local ``/ppp secret`` bypasses the RADIUS projection rather than
        supplementing it, so access lives entirely in
        ``access.radius_projection``; suspension is CoA through
        ``access.session_enforcement``; removal of an existing secret is the
        reviewed cleanup operation, not a builder concern.
        """
        cmds = _mikrotik_commands(
            mock_subscription, mock_profile, ConnectionType.pppoe, action
        )

        assert cmds == []

    def test_pppoe_never_mentions_a_local_secret_at_all(
        self, mock_subscription, mock_profile
    ):
        """Belt and braces: no action may leak the prohibited verbs."""
        emitted = [
            command
            for action in ("create", "delete", "suspend", "unsuspend", "change_ip")
            for command in _mikrotik_commands(
                mock_subscription, mock_profile, ConnectionType.pppoe, action
            )
        ]

        assert not [c for c in emitted if "/ppp secret" in c]

    def test_dhcp_create(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(
            mock_subscription, mock_profile, ConnectionType.dhcp, "create"
        )
        assert len(cmds) == 1
        assert "/ip dhcp-server lease add" in cmds[0]
        assert "mac-address=" in cmds[0]

    def test_dhcp_delete(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(
            mock_subscription, mock_profile, ConnectionType.dhcp, "delete"
        )
        assert len(cmds) == 1
        assert "/ip dhcp-server lease remove" in cmds[0]

    def test_hotspot_create(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(
            mock_subscription, mock_profile, ConnectionType.hotspot, "create"
        )
        assert len(cmds) == 1
        assert "/ip hotspot user add" in cmds[0]
        assert mock_profile.name in cmds[0]

    def test_hotspot_suspend(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(
            mock_subscription, mock_profile, ConnectionType.hotspot, "suspend"
        )
        assert len(cmds) == 2
        assert "disabled=yes" in cmds[0]
        assert "/ip hotspot active remove" in cmds[1]

    def test_static_suspend(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(
            mock_subscription, mock_profile, ConnectionType.static, "suspend"
        )
        assert len(cmds) == 1
        assert "blocked-subscribers" in cmds[0]

    def test_ipoe_create(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(
            mock_subscription, mock_profile, ConnectionType.ipoe, "create"
        )
        assert len(cmds) == 1
        assert "/ip dhcp-server lease add" in cmds[0]
        assert "use-src-mac=yes" in cmds[0]
