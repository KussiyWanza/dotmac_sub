"""Tests for enforcement gap implementations.

Covers:
- CoA-Update for mid-session speed changes
- Hotspot session disconnect
- Subscription cancellation cleanup
- EnforcementHandler event routing
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.catalog import (
    AccessCredential,
    NasDevice,
    RadiusProfile,
    Subscription,
)
from app.services.enforcement import (
    CoADisconnectDisposition,
    CoADisconnectOutcome,
)
from app.services.events.types import Event, EventType
from app.services.radius import (
    ConnectivityProjectionDisposition,
    RadiusConnectivityProjectionIncomplete,
    SubscriptionConnectivityOutcome,
)


def _coa(disposition: CoADisconnectDisposition) -> CoADisconnectOutcome:
    return CoADisconnectOutcome(disposition)


# ---------------------------------------------------------------------------
# _build_mikrotik_rate_limit
# ---------------------------------------------------------------------------


class TestBuildMikrotikRateLimit:
    def test_returns_explicit_rate_limit(self):
        from app.services.enforcement import _build_mikrotik_rate_limit

        profile = MagicMock(spec=RadiusProfile)
        profile.mikrotik_rate_limit = "50M/25M"
        profile.download_speed = 50000
        profile.upload_speed = 25000
        assert _build_mikrotik_rate_limit(profile) == "50M/25M"

    def test_builds_from_speeds(self):
        from app.services.enforcement import _build_mikrotik_rate_limit

        profile = MagicMock(spec=RadiusProfile)
        profile.mikrotik_rate_limit = None
        profile.download_speed = 50000
        profile.upload_speed = 25000
        profile.burst_download = None
        profile.burst_upload = None
        profile.burst_threshold = None
        profile.burst_time = None
        result = _build_mikrotik_rate_limit(profile)
        # rx/tx is NAS-perspective: rx is the subscriber's upload, tx their
        # download (app.services.bandwidth.to_subscriber_directions). So a
        # 50/25 profile emits upload first — reversed, the customer would be
        # given 25 Mbps down and 50 Mbps up.
        assert result == "25000k/50000k"

    def test_builds_with_burst(self):
        from app.services.enforcement import _build_mikrotik_rate_limit

        profile = MagicMock(spec=RadiusProfile)
        profile.mikrotik_rate_limit = None
        profile.download_speed = 50000
        profile.upload_speed = 25000
        profile.burst_download = 75000
        profile.burst_upload = 37500
        profile.burst_threshold = 40000
        profile.burst_time = 15
        result = _build_mikrotik_rate_limit(profile)
        # Upload-first applies to the burst and threshold pairs too, not just
        # the base rate — a half-swapped string is worse than a fully swapped
        # one because the burst would fight the base limit.
        assert "25000k/50000k" in result
        assert "37500k/75000k" in result
        assert "15/15" in result

    def test_returns_none_no_speeds(self):
        from app.services.enforcement import _build_mikrotik_rate_limit

        profile = MagicMock(spec=RadiusProfile)
        profile.mikrotik_rate_limit = None
        profile.download_speed = None
        profile.upload_speed = None
        assert _build_mikrotik_rate_limit(profile) is None


# ---------------------------------------------------------------------------
# update_subscription_sessions (CoA-Update)
# ---------------------------------------------------------------------------


class TestUpdateSubscriptionSessions:
    @patch("app.services.enforcement._send_coa_update")
    @patch("app.services.enforcement._resolve_effective_profile")
    def test_sends_coa_update(self, mock_profile, mock_coa_update):
        from app.services.enforcement import update_subscription_sessions

        db = MagicMock()
        sub_id = uuid4()
        subscription = MagicMock(spec=Subscription)
        subscription.id = sub_id
        subscription.ipv4_address = "10.0.0.1"
        db.get.return_value = subscription

        profile = MagicMock(spec=RadiusProfile)
        mock_profile.return_value = profile

        session = MagicMock()
        session.access_credential_id = uuid4()
        session.session_id = "sess-123"
        credential = MagicMock(spec=AccessCredential)
        credential.username = "user1"

        db.query.return_value.filter.return_value.filter.return_value.filter.return_value.all.return_value = [
            session
        ]
        # First db.get returns subscription, second returns credential, third returns NAS
        nas_device = MagicMock(spec=NasDevice)
        db.get.side_effect = [subscription, credential, nas_device]

        mock_coa_update.return_value = True

        # Need to also mock _resolve_nas_device
        with patch(
            "app.services.enforcement._resolve_nas_device", return_value=nas_device
        ):
            count = update_subscription_sessions(db, str(sub_id))

        assert count == 1
        mock_coa_update.assert_called_once()

    @patch("app.services.enforcement._resolve_effective_profile")
    def test_skips_if_no_profile(self, mock_profile):
        from app.services.enforcement import update_subscription_sessions

        db = MagicMock()
        subscription = MagicMock(spec=Subscription)
        db.get.return_value = subscription
        mock_profile.return_value = None

        count = update_subscription_sessions(db, str(uuid4()))
        assert count == 0


# ---------------------------------------------------------------------------
# EnforcementHandler event routing
# ---------------------------------------------------------------------------


class TestEnforcementHandlerRouting:
    def test_routes_upgraded_to_speed_change(self):
        from app.services.events.handlers.enforcement import EnforcementHandler

        handler = EnforcementHandler()
        event = Event(
            event_type=EventType.subscription_upgraded,
            payload={"subscription_id": str(uuid4())},
            subscription_id=uuid4(),
        )
        db = MagicMock()
        with patch.object(handler, "_handle_subscription_speed_change") as mock:
            handler.handle(db, event)
            mock.assert_called_once_with(db, event)

    def test_routes_downgraded_to_speed_change(self):
        from app.services.events.handlers.enforcement import EnforcementHandler

        handler = EnforcementHandler()
        event = Event(
            event_type=EventType.subscription_downgraded,
            payload={"subscription_id": str(uuid4())},
            subscription_id=uuid4(),
        )
        db = MagicMock()
        with patch.object(handler, "_handle_subscription_speed_change") as mock:
            handler.handle(db, event)
            mock.assert_called_once_with(db, event)

    def test_routes_canceled_to_cleanup(self):
        from app.services.events.handlers.enforcement import EnforcementHandler

        handler = EnforcementHandler()
        event = Event(
            event_type=EventType.subscription_canceled,
            payload={"subscription_id": str(uuid4())},
            subscription_id=uuid4(),
        )
        db = MagicMock()
        with patch.object(handler, "_handle_subscription_cancel") as mock:
            handler.handle(db, event)
            mock.assert_called_once_with(db, event)

    def test_routes_suspended_to_block(self):
        from app.services.events.handlers.enforcement import EnforcementHandler

        handler = EnforcementHandler()
        event = Event(
            event_type=EventType.subscription_suspended,
            payload={"subscription_id": str(uuid4())},
            subscription_id=uuid4(),
        )
        db = MagicMock()
        with patch.object(handler, "_handle_subscription_block") as mock:
            handler.handle(db, event)
            mock.assert_called_once()

    def test_routes_activated_to_restore(self):
        from app.services.events.handlers.enforcement import EnforcementHandler

        handler = EnforcementHandler()
        event = Event(
            event_type=EventType.subscription_activated,
            payload={"subscription_id": str(uuid4())},
            subscription_id=uuid4(),
        )
        db = MagicMock()
        with patch.object(handler, "_handle_subscription_restore") as mock:
            handler.handle(db, event)
            mock.assert_called_once_with(db, event)


# ---------------------------------------------------------------------------
# ProvisioningHandler auto-provisioning
# ---------------------------------------------------------------------------


class TestProvisioningHandlerAutoProvisioning:
    def test_activation_syncs_radius(self):
        from app.services.events.handlers.provisioning import ProvisioningHandler

        handler = ProvisioningHandler()
        sub_id = uuid4()
        event = Event(
            event_type=EventType.subscription_activated,
            payload={"subscription_id": str(sub_id)},
            subscription_id=sub_id,
        )
        db = MagicMock()
        with (
            patch.object(handler, "_sync_radius_on_activation") as mock_sync,
            patch.object(handler, "_push_nas_provisioning") as mock_push,
            patch(
                "app.services.events.handlers.provisioning.provisioning_service"
            ) as mock_prov,
        ):
            handler._handle_subscription_activated(db, event)
            mock_sync.assert_called_once_with(db, str(sub_id))
            mock_push.assert_called_once_with(db, str(sub_id))

    @patch("app.services.events.handlers.provisioning.coerce_uuid")
    def test_sync_radius_calls_service(self, mock_coerce):
        from app.services.events.handlers.provisioning import ProvisioningHandler

        handler = ProvisioningHandler()
        db = MagicMock()
        subscription = MagicMock(spec=Subscription)
        subscription.subscriber_id = uuid4()
        db.get.return_value = subscription
        mock_coerce.return_value = subscription.id

        with (
            patch(
                "app.services.radius.sync_account_credentials_to_radius",
                return_value=2,
            ) as mock_sync,
            patch(
                "app.services.radius.reconcile_subscription_connectivity",
                return_value=SubscriptionConnectivityOutcome(
                    subscription_id=str(uuid4()),
                    disposition=ConnectivityProjectionDisposition.projected,
                    requested_logins=1,
                    projected_logins=1,
                    projection_targets=1,
                ),
            ),
        ):
            handler._sync_radius_on_activation(db, str(uuid4()))
            mock_sync.assert_called_once()


def test_restore_connectivity_fails_when_radius_projection_is_incomplete():
    from app.services.enforcement import restore_subscription_connectivity

    db = MagicMock()
    subscription = MagicMock(spec=Subscription)
    subscription.id = uuid4()
    subscription.subscriber_id = uuid4()
    db.get.return_value = subscription
    db.query.return_value.filter.return_value.filter.return_value.all.return_value = []

    with (
        patch("app.services.connectivity_backup.capture_connectivity_state"),
        patch(
            "app.services.radius.reconcile_subscription_connectivity",
            return_value=SubscriptionConnectivityOutcome(
                subscription_id=str(subscription.id),
                disposition=ConnectivityProjectionDisposition.target_unavailable,
            ),
        ),
        patch(
            "app.services.enforcement.remove_subscription_address_list_block"
        ) as remove_block,
        pytest.raises(RadiusConnectivityProjectionIncomplete),
    ):
        restore_subscription_connectivity(db, str(subscription.id))

    remove_block.assert_not_called()


# ---------------------------------------------------------------------------
# CoA negative-cache (auto-detect NASes that don't support CoA)
# ---------------------------------------------------------------------------


class TestCoaNegativeCache:
    """A CoA Timeout poisons the NAS for 15 minutes; a success clears it."""

    def setup_method(self) -> None:
        from app.services import enforcement

        enforcement.reset_coa_cache()

    def teardown_method(self) -> None:
        from app.services import enforcement

        enforcement.reset_coa_cache()

    def _device(self, nas_id):
        device = MagicMock(spec=NasDevice)
        device.id = nas_id
        device.shared_secret = "enc:secret"
        device.nas_ip = "10.1.1.1"
        device.management_ip = "10.1.1.1"
        device.ip_address = "10.1.1.1"
        device.coa_port = 3799
        return device

    @patch("app.services.enforcement.decrypt_credential", return_value="secret")
    @patch("app.services.enforcement._radius_dictionary_path", return_value="/dict")
    @patch("app.services.enforcement.Dictionary")
    @patch("app.services.enforcement.Client")
    def test_timeout_populates_cache_and_skips_next_call(
        self, mock_client_cls, mock_dict, mock_path, mock_decrypt
    ):
        from pyrad.client import Timeout

        from app.services.enforcement import _coa_disabled_for_nas, _send_coa_disconnect

        db = MagicMock()
        nas_id = uuid4()
        device = self._device(nas_id)
        mock_client = MagicMock()
        mock_client.SendPacket.side_effect = Timeout()
        mock_client_cls.return_value = mock_client

        with (
            patch("app.services.enforcement._coa_enabled", return_value=True),
            patch("app.services.enforcement._coa_retries", return_value=0),
            patch("app.services.enforcement._radius_timeout_sec", return_value=0.01),
        ):
            assert (
                _send_coa_disconnect(db, device, "u", "1.2.3.4", "sid").disposition
                is CoADisconnectDisposition.timeout
            )

        assert _coa_disabled_for_nas(nas_id) is True

        # Second call must short-circuit: Client must NOT be instantiated again.
        mock_client_cls.reset_mock()
        with patch("app.services.enforcement._coa_enabled", return_value=True):
            assert (
                _send_coa_disconnect(db, device, "u", "1.2.3.4", "sid").disposition
                is CoADisconnectDisposition.temporarily_suppressed
            )
        mock_client_cls.assert_not_called()

    @patch("app.services.enforcement.decrypt_credential", return_value="secret")
    @patch("app.services.enforcement._radius_dictionary_path", return_value="/dict")
    @patch("app.services.enforcement.Dictionary")
    @patch("app.services.enforcement.Client")
    def test_success_clears_negative_cache(
        self, mock_client_cls, mock_dict, mock_path, mock_decrypt
    ):
        from app.services.enforcement import (
            _coa_disabled_for_nas,
            _mark_coa_unsupported,
            _send_coa_disconnect,
        )

        db = MagicMock()
        nas_id = uuid4()
        device = self._device(nas_id)
        _mark_coa_unsupported(nas_id)
        assert _coa_disabled_for_nas(nas_id) is True

        # Manually clear so the next call actually runs (success path then re-clears).
        from app.services import enforcement

        enforcement.reset_coa_cache(nas_id)

        from pyrad.packet import DisconnectACK

        mock_client = MagicMock()
        mock_client.SendPacket.return_value = SimpleNamespace(code=DisconnectACK)
        mock_client_cls.return_value = mock_client

        with (
            patch("app.services.enforcement._coa_enabled", return_value=True),
            patch("app.services.enforcement._coa_retries", return_value=0),
            patch("app.services.enforcement._radius_timeout_sec", return_value=0.01),
        ):
            assert _send_coa_disconnect(db, device, "u", "1.2.3.4", "sid").disconnected

        # Successful send must keep the cache clear.
        assert _coa_disabled_for_nas(nas_id) is False

    @patch("app.services.enforcement.decrypt_credential", return_value="secret")
    @patch("app.services.enforcement._radius_dictionary_path", return_value="/dict")
    @patch("app.services.enforcement.Dictionary")
    @patch("app.services.enforcement.Client")
    def test_disconnect_nak_is_not_a_kill(
        self, mock_client_cls, mock_dict, mock_path, mock_decrypt
    ):
        """A Disconnect-NAK means the NAS speaks CoA but did NOT kill the
        session (e.g. stale Framed-IP-Address AND-match) — must return False
        so the SSH fallback runs, and must NOT negative-cache the NAS."""
        from pyrad.packet import DisconnectNAK

        from app.services.enforcement import _coa_disabled_for_nas, _send_coa_disconnect

        db = MagicMock()
        nas_id = uuid4()
        device = self._device(nas_id)
        mock_client = MagicMock()
        mock_client.SendPacket.return_value = SimpleNamespace(code=DisconnectNAK)
        mock_client_cls.return_value = mock_client

        with (
            patch("app.services.enforcement._coa_enabled", return_value=True),
            patch("app.services.enforcement._coa_retries", return_value=0),
            patch("app.services.enforcement._radius_timeout_sec", return_value=0.01),
        ):
            assert (
                _send_coa_disconnect(db, device, "u", "1.2.3.4", "sid").disposition
                is CoADisconnectDisposition.rejected
            )
        assert _coa_disabled_for_nas(nas_id) is False

    @patch("app.services.enforcement.decrypt_credential", return_value="secret")
    @patch("app.services.enforcement._radius_dictionary_path", return_value="/dict")
    @patch("app.services.enforcement.Dictionary")
    @patch("app.services.enforcement.Client")
    def test_disconnect_nak_session_not_found_is_typed_evidence(
        self, mock_client_cls, mock_dict, mock_path, mock_decrypt
    ):
        from pyrad.packet import DisconnectNAK

        from app.services.enforcement import _send_coa_disconnect

        db = MagicMock()
        device = self._device(uuid4())
        response = MagicMock()
        response.code = DisconnectNAK
        response.__getitem__.return_value = [b"503"]
        mock_client_cls.return_value.SendPacket.return_value = response

        with (
            patch("app.services.enforcement._coa_enabled", return_value=True),
            patch("app.services.enforcement._coa_retries", return_value=0),
            patch("app.services.enforcement._radius_timeout_sec", return_value=0.01),
        ):
            outcome = _send_coa_disconnect(db, device, "u", "1.2.3.4", "sid")

        assert outcome.session_absent

    def test_reset_coa_cache_all_and_per_nas(self):
        from app.services.enforcement import (
            _coa_disabled_for_nas,
            _mark_coa_unsupported,
            reset_coa_cache,
        )

        a, b = uuid4(), uuid4()
        _mark_coa_unsupported(a)
        _mark_coa_unsupported(b)
        assert _coa_disabled_for_nas(a) is True
        assert _coa_disabled_for_nas(b) is True

        reset_coa_cache(a)
        assert _coa_disabled_for_nas(a) is False
        assert _coa_disabled_for_nas(b) is True

        reset_coa_cache()
        assert _coa_disabled_for_nas(b) is False


# ---------------------------------------------------------------------------
# _resolve_coa_secret — external nas-table fallback
# ---------------------------------------------------------------------------


class TestResolveCoaSecret:
    def setup_method(self) -> None:
        from app.services import enforcement

        enforcement.reset_coa_cache()

    teardown_method = setup_method

    def _device(self, nas_id, shared_secret=None):
        device = MagicMock(spec=NasDevice)
        device.id = nas_id
        device.name = "Test NAS"
        device.shared_secret = shared_secret
        device.nas_ip = "10.1.1.1"
        device.management_ip = "10.1.1.1"
        device.ip_address = "10.1.1.1"
        device.coa_port = 3799
        return device

    @patch("app.services.enforcement.decrypt_credential", return_value="local-secret")
    def test_prefers_local_shared_secret(self, mock_decrypt):
        from app.services.enforcement import _resolve_coa_secret

        device = self._device(uuid4(), shared_secret="enc:x")
        assert _resolve_coa_secret(MagicMock(), device) == "local-secret"
        mock_decrypt.assert_called_once_with("enc:x")

    def test_falls_back_to_external_nas_table(self):
        from app.services.enforcement import _resolve_coa_secret

        device = self._device(uuid4(), shared_secret=None)
        conn = MagicMock()
        conn.execute.return_value.scalar.return_value = "radius-secret"
        engine = MagicMock()
        engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch(
                "app.services.radius._active_external_sync_configs",
                return_value=[{"db_url": "postgresql://x", "nas_table": "nas"}],
            ),
            patch("app.services.radius._get_external_engine", return_value=engine),
            patch(
                "app.services.radius._radius_client_ip_for_nas",
                return_value="10.1.1.1",
            ),
        ):
            assert _resolve_coa_secret(MagicMock(), device) == "radius-secret"

        # Second call must hit the per-process cache, not the engine again.
        engine.connect.reset_mock()
        with patch(
            "app.services.radius._active_external_sync_configs",
            return_value=[],
        ):
            assert _resolve_coa_secret(MagicMock(), device) == "radius-secret"
        engine.connect.assert_not_called()

    def test_returns_none_when_no_secret_anywhere(self):
        from app.services.enforcement import _resolve_coa_secret

        device = self._device(uuid4(), shared_secret=None)
        with (
            patch(
                "app.services.radius._active_external_sync_configs",
                return_value=[],
            ),
            patch(
                "app.services.radius._radius_client_ip_for_nas",
                return_value="10.1.1.1",
            ),
        ):
            assert _resolve_coa_secret(MagicMock(), device) is None

    @patch("app.services.enforcement._radius_dictionary_path", return_value="/dict")
    @patch("app.services.enforcement.Dictionary")
    @patch("app.services.enforcement.Client")
    def test_coa_disconnect_uses_fallback_secret(
        self, mock_client_cls, mock_dict, mock_path
    ):
        from pyrad.packet import DisconnectACK

        from app.services.enforcement import _send_coa_disconnect

        device = self._device(uuid4(), shared_secret=None)
        mock_client = MagicMock()
        mock_client.SendPacket.return_value = SimpleNamespace(code=DisconnectACK)
        mock_client_cls.return_value = mock_client

        with (
            patch("app.services.enforcement._coa_enabled", return_value=True),
            patch("app.services.enforcement._coa_retries", return_value=0),
            patch("app.services.enforcement._radius_timeout_sec", return_value=0.01),
            patch(
                "app.services.enforcement._resolve_coa_secret",
                return_value="radius-secret",
            ),
        ):
            assert _send_coa_disconnect(
                MagicMock(), device, "u", None, "sid"
            ).disconnected
        assert mock_client_cls.call_args.kwargs["secret"] == b"radius-secret"

    def test_unusable_local_secret_falls_through_to_external(self):
        """A corrupt/undecryptable local shared_secret must not raise out of
        the enforcement loop — it falls through to the external nas table."""
        from app.services.enforcement import _resolve_coa_secret

        device = self._device(uuid4(), shared_secret="enc:corrupt")
        conn = MagicMock()
        conn.execute.return_value.scalar.return_value = "radius-secret"
        engine = MagicMock()
        engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch(
                "app.services.enforcement.decrypt_credential",
                side_effect=ValueError("bad token"),
            ),
            patch(
                "app.services.radius._active_external_sync_configs",
                return_value=[{"db_url": "postgresql://x", "nas_table": "nas"}],
            ),
            patch("app.services.radius._get_external_engine", return_value=engine),
            patch(
                "app.services.radius._radius_client_ip_for_nas",
                return_value="10.1.1.1",
            ),
        ):
            assert _resolve_coa_secret(MagicMock(), device) == "radius-secret"


# ---------------------------------------------------------------------------
# disconnect_subscription_sessions — zero-kill must be loud
# ---------------------------------------------------------------------------


class TestDisconnectZeroKillLogging:
    def test_warns_when_sessions_found_but_none_disconnected(self, caplog):
        import logging as _logging

        from app.services.enforcement import disconnect_subscription_sessions

        db = MagicMock()
        subscription = MagicMock(spec=Subscription)
        subscription.id = uuid4()
        subscription.ipv4_address = None
        db.get.return_value = subscription

        session = MagicMock()
        session.session_id = "sid-1"
        session.nas_device_id = None
        session.radius_client_id = None
        session.access_credential_id = None
        db.query.return_value.filter.return_value.filter.return_value.filter.return_value.all.return_value = [
            session
        ]

        with caplog.at_level(_logging.WARNING, logger="app.services.enforcement"):
            count = disconnect_subscription_sessions(db, str(subscription.id))

        assert count == 0
        messages = " ".join(r.message for r in caplog.records)
        assert "no NAS device" in messages
        assert "Failed to disconnect any" in messages


class TestDisconnectRouterOsApiGate:
    def _session_context(self):
        db = MagicMock()
        subscription = MagicMock(spec=Subscription)
        subscription.id = uuid4()
        subscription.login = "pppoe-user"
        subscription.ipv4_address = "10.0.0.10"
        credential = MagicMock(spec=AccessCredential)
        credential.username = "pppoe-user"
        db.get.side_effect = [subscription, credential]

        session = MagicMock()
        session.session_id = "sid-1"
        session.access_credential_id = uuid4()
        db.query.return_value.filter.return_value.filter.return_value.filter.return_value.all.return_value = [
            session
        ]

        nas_device = MagicMock(spec=NasDevice)
        nas_device.id = uuid4()
        nas_device.name = "BNG-1"
        return db, subscription, nas_device

    def test_routeros_api_kick_default_disabled_uses_ssh_fallback(self):
        from app.services.enforcement import disconnect_subscription_sessions

        db, subscription, nas_device = self._session_context()
        ssh = MagicMock()
        ssh_cm = MagicMock()
        ssh_cm.__enter__.return_value = ssh
        ssh_cm.__exit__.return_value = False

        with (
            patch(
                "app.services.enforcement._open_radacct_sessions_for_username",
                return_value=[],
            ),
            patch(
                "app.services.enforcement._resolve_nas_device",
                return_value=nas_device,
            ),
            patch(
                "app.services.enforcement._send_coa_disconnect",
                return_value=_coa(CoADisconnectDisposition.rejected),
            ),
            patch(
                "app.services.enforcement._mikrotik_api_session_kick_enabled",
                return_value=False,
            ),
            patch(
                "app.services.nas._mikrotik.disconnect_mikrotik_pppoe_bulk"
            ) as api_kick,
            patch(
                "app.services.enforcement.DeviceProvisioner.ssh_session",
                return_value=ssh_cm,
            ),
            patch(
                "app.services.enforcement._disconnect_mikrotik_session",
                return_value=True,
            ),
        ):
            count = disconnect_subscription_sessions(db, str(subscription.id))

        assert count == 1
        api_kick.assert_not_called()

    def test_routeros_api_kick_enabled_calls_api_fallback(self):
        from app.services.enforcement import disconnect_subscription_sessions

        db, subscription, nas_device = self._session_context()

        with (
            patch(
                "app.services.enforcement._open_radacct_sessions_for_username",
                return_value=[],
            ),
            patch(
                "app.services.enforcement._resolve_nas_device",
                return_value=nas_device,
            ),
            patch(
                "app.services.enforcement._send_coa_disconnect",
                return_value=_coa(CoADisconnectDisposition.rejected),
            ),
            patch(
                "app.services.enforcement._mikrotik_api_session_kick_enabled",
                return_value=True,
            ),
            patch(
                "app.services.enforcement._nas_with_api_creds",
                return_value=nas_device,
            ),
            patch(
                "app.services.nas._mikrotik.disconnect_mikrotik_pppoe_bulk",
                return_value={"pppoe-user"},
            ) as api_kick,
            patch("app.services.enforcement.DeviceProvisioner.ssh_session") as ssh,
        ):
            count = disconnect_subscription_sessions(db, str(subscription.id))

        assert count == 1
        api_kick.assert_called_once_with(nas_device, {"pppoe-user"})
        ssh.assert_not_called()


class TestAddressListApiFallback:
    def test_address_list_api_fallback_is_not_gated_by_session_kick_setting(self):
        from app.services.enforcement import _enforce_address_list_on_nas

        db = MagicMock()
        nas_device = MagicMock(spec=NasDevice)
        nas_device.name = "BNG-API"

        ssh_cm = MagicMock()
        ssh_cm.__enter__.side_effect = RuntimeError("ssh unavailable")

        with (
            patch(
                "app.services.enforcement.DeviceProvisioner.ssh_session",
                return_value=ssh_cm,
            ),
            patch(
                "app.services.enforcement._mikrotik_api_session_kick_enabled",
                return_value=False,
            ) as session_kick_enabled,
            patch(
                "app.services.enforcement._nas_with_api_creds",
                return_value=nas_device,
            ),
            patch(
                "app.services.nas._mikrotik.apply_mikrotik_address_list_via_api",
                return_value=True,
            ) as api_apply,
        ):
            result = _enforce_address_list_on_nas(
                db, nas_device, "blocked", "10.0.0.10", add=True
            )

        assert result is True
        session_kick_enabled.assert_not_called()
        api_apply.assert_called_once_with(nas_device, "blocked", "10.0.0.10")
