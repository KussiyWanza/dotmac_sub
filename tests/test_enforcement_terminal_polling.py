"""Terminal RADIUS enforcement waits for accounting convergence without re-kick."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.catalog import NasDevice, Subscription
from app.services.enforcement import (
    CoADisconnectDisposition,
    CoADisconnectOutcome,
    SessionEnforcementError,
    disconnect_subscription_sessions,
)


def _context():
    db = MagicMock()
    subscription = MagicMock(spec=Subscription)
    subscription.id = uuid4()
    subscription.login = "projection-user"
    db.get.return_value = subscription
    db.query.return_value.filter.return_value.filter.return_value.filter.return_value.all.return_value = []
    nas = MagicMock(spec=NasDevice)
    nas.id = uuid4()
    nas.name = "BNG-1"
    open_session = {
        "session_id": "acct-1",
        "nas_ip": "192.0.2.10",
        "framed_ip": "172.16.1.10",
    }
    return db, subscription, nas, open_session


def test_terminal_poll_observes_settle_without_second_disconnect() -> None:
    db, subscription, nas, open_session = _context()

    with (
        patch(
            "app.services.enforcement._open_radacct_sessions_for_username",
            side_effect=[[open_session], [open_session], []],
        ) as observe,
        patch(
            "app.services.enforcement._nas_device_by_ip",
            return_value=nas,
        ),
        patch(
            "app.services.enforcement._send_coa_disconnect",
            return_value=CoADisconnectOutcome(CoADisconnectDisposition.disconnected),
        ) as disconnect,
        patch("app.services.enforcement.time.monotonic", side_effect=[0.0, 0.1]),
        patch("app.services.enforcement.time.sleep") as sleep,
    ):
        count = disconnect_subscription_sessions(
            db,
            str(subscription.id),
            reason="ip_assignment_served_projection_repaired",
            framed_ip_address="172.16.1.10",
            require_terminal=True,
        )

    assert count == 1
    assert observe.call_count == 3
    disconnect.assert_called_once()
    sleep.assert_called_once_with(0.5)


def test_terminal_poll_times_out_durably_without_second_disconnect() -> None:
    db, subscription, nas, open_session = _context()

    with (
        patch(
            "app.services.enforcement._open_radacct_sessions_for_username",
            side_effect=[[open_session], [open_session]],
        ),
        patch(
            "app.services.enforcement._nas_device_by_ip",
            return_value=nas,
        ),
        patch(
            "app.services.enforcement._send_coa_disconnect",
            return_value=CoADisconnectOutcome(CoADisconnectDisposition.disconnected),
        ) as disconnect,
        patch("app.services.enforcement.time.monotonic", side_effect=[0.0, 16.0]),
        pytest.raises(SessionEnforcementError) as exc_info,
    ):
        disconnect_subscription_sessions(
            db,
            str(subscription.id),
            reason="ip_assignment_served_projection_repaired",
            framed_ip_address="172.16.1.10",
            require_terminal=True,
        )

    assert exc_info.value.code.endswith(".terminal_session_timeout")
    disconnect.assert_called_once()


def test_authoritative_retry_ignores_lagging_imported_session() -> None:
    db, subscription, _nas, _open_session = _context()
    stale_import = MagicMock()
    stale_import.session_id = "stale-import"
    db.query.return_value.filter.return_value.filter.return_value.filter.return_value.all.return_value = [
        stale_import
    ]

    with (
        patch(
            "app.services.enforcement._open_radacct_sessions_for_username",
            side_effect=[[], []],
        ),
        patch("app.services.enforcement._resolve_nas_device") as resolve_nas,
        patch("app.services.enforcement._send_coa_disconnect") as disconnect,
    ):
        count = disconnect_subscription_sessions(
            db,
            str(subscription.id),
            reason="ip_assignment_served_projection_repaired",
            framed_ip_address="172.16.1.10",
            require_terminal=True,
            authoritative_only=True,
        )

    assert count == 0
    resolve_nas.assert_not_called()
    disconnect.assert_not_called()
