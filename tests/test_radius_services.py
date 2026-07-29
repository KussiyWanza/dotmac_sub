"""Tests for RADIUS service."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from passlib.context import CryptContext
from passlib.hash import sha512_crypt

from app.models.catalog import (
    AccessCredential,
    NasDevice,
    NasVendor,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.radius import RadiusClient, RadiusServer, RadiusUser
from app.models.subscription_engine import SettingValueType
from app.schemas.radius import (
    RadiusClientCreate,
    RadiusClientUpdate,
    RadiusServerCreate,
    RadiusServerUpdate,
    RadiusSyncJobCreate,
)
from app.services import radius as radius_service
from app.services import radius_auth

SERVICE_PASSWORD_CONTEXT = CryptContext(
    schemes=["pbkdf2_sha256", "bcrypt", "sha512_crypt"],
    default="pbkdf2_sha256",
)


def test_create_radius_server(db_session):
    """Test creating a RADIUS server."""
    server = radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(
            name="Main RADIUS",
            host="radius.example.com",
            auth_port=1812,
            acct_port=1813,
        ),
    )
    assert server.name == "Main RADIUS"
    assert server.host == "radius.example.com"
    assert server.auth_port == 1812
    assert server.acct_port == 1813


def test_radius_server_default_ports(db_session):
    """Test RADIUS server with default ports."""
    server = radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(
            name="Default Ports",
            host="radius2.example.com",
        ),
    )
    assert server.host == "radius2.example.com"


def test_list_radius_servers(db_session):
    """Test listing RADIUS servers."""
    radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(name="Server 1", host="radius1.local"),
    )
    radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(name="Server 2", host="radius2.local"),
    )

    servers = radius_service.radius_servers.list(
        db_session,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(servers) >= 2


def test_update_radius_server(db_session):
    """Test updating a RADIUS server."""
    server = radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(name="Original", host="old.host.com"),
    )
    updated = radius_service.radius_servers.update(
        db_session,
        str(server.id),
        RadiusServerUpdate(name="Updated", host="new.host.com"),
    )
    assert updated.name == "Updated"
    assert updated.host == "new.host.com"


def test_delete_radius_server(db_session):
    """Test deleting a RADIUS server."""
    server = radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(name="To Delete", host="delete.local"),
    )
    radius_service.radius_servers.delete(db_session, str(server.id))
    db_session.refresh(server)
    assert server.is_active is False


def test_create_radius_client(db_session, radius_server):
    """Test creating a RADIUS client."""
    client = radius_service.radius_clients.create(
        db_session,
        RadiusClientCreate(
            server_id=radius_server.id,
            client_ip="10.0.0.1",
            shared_secret_hash="shared-secret-hash",
        ),
    )
    assert client.server_id == radius_server.id
    assert client.client_ip == "10.0.0.1"


def test_external_password_row_uses_cleartext_password_for_plain_prefixed_secret():
    credential = AccessCredential(
        subscriber_id="00000000-0000-0000-0000-000000000001",
        username="10005030",
        secret_hash="plain:secret123",
        is_active=True,
    )

    row = radius_service._external_password_row(
        credential,
        default_attribute="Cleartext-Password",
        default_op=":=",
    )

    assert row is not None
    assert row[0] == "Cleartext-Password"
    assert row[1] == ":="
    assert row[2] == "secret123"


def test_external_password_row_strips_cleartext_marker():
    credential = AccessCredential(
        subscriber_id="00000000-0000-0000-0000-000000000001",
        username="10005031",
        secret_hash="cleartext:secret123",
        is_active=True,
    )

    row = radius_service._external_password_row(
        credential,
        default_attribute="Cleartext-Password",
        default_op=":=",
    )

    assert row == ("Cleartext-Password", ":=", "secret123")


def test_external_password_row_keeps_crypt_password_for_legacy_sha512_crypt():
    credential = AccessCredential(
        subscriber_id="00000000-0000-0000-0000-000000000001",
        username="10005030",
        secret_hash=sha512_crypt.hash("secret123"),
        is_active=True,
    )

    row = radius_service._external_password_row(
        credential,
        default_attribute="Cleartext-Password",
        default_op=":=",
    )

    assert row is not None
    assert row[0] == "Crypt-Password"
    assert row[1] == ":="
    assert row[2].startswith("$6$")


def test_external_password_row_skips_legacy_pbkdf2_hash():
    credential = AccessCredential(
        subscriber_id="00000000-0000-0000-0000-000000000001",
        username="10005030",
        secret_hash=SERVICE_PASSWORD_CONTEXT.hash("secret123"),
        is_active=True,
    )

    row = radius_service._external_password_row(
        credential,
        default_attribute="Cleartext-Password",
        default_op=":=",
    )

    assert row is None


def test_list_radius_clients_by_server(db_session, radius_server):
    """Test listing RADIUS clients by server."""
    radius_service.radius_clients.create(
        db_session,
        RadiusClientCreate(
            server_id=radius_server.id,
            client_ip="10.0.0.1",
            shared_secret_hash="secret1",
        ),
    )
    radius_service.radius_clients.create(
        db_session,
        RadiusClientCreate(
            server_id=radius_server.id,
            client_ip="10.0.0.2",
            shared_secret_hash="secret2",
        ),
    )

    clients = radius_service.radius_clients.list(
        db_session,
        server_id=str(radius_server.id),
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(clients) >= 2
    assert all(c.server_id == radius_server.id for c in clients)


def _write_external_radius_db(db_path, rows):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE radcheck (username TEXT, attribute TEXT, op TEXT, value TEXT)"
        )
        conn.executemany(
            "INSERT INTO radcheck (username, attribute, op, value) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_import_access_credentials_from_external_radius_matches_subscription_login(
    db_session, tmp_path, subscriber, catalog_offer
):
    db_session.add(
        Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
            login="pppoe-001",
        )
    )
    db_session.commit()

    radius_db = tmp_path / "radius-login.db"
    _write_external_radius_db(
        radius_db,
        [("pppoe-001", "Cleartext-Password", ":=", "secret123")],
    )

    result = radius_service.import_access_credentials_from_external_radius(
        db_session,
        config={
            "db_url": f"sqlite:///{radius_db}",
            "radcheck_table": '"radcheck"',
        },
    )

    credential = (
        db_session.query(AccessCredential)
        .filter(AccessCredential.username == "pppoe-001")
        .one()
    )
    assert credential.subscriber_id == subscriber.id
    assert credential.secret_hash is not None
    assert credential.secret_hash.startswith(("enc:", "plain:"))
    assert result["created"] == 1
    assert result["matched_subscription_login"] == 1
    assert result["secrets_imported"] == 1


def test_import_access_credentials_from_external_radius_skips_opaque_password_but_creates_credential(
    db_session, tmp_path, subscriber
):
    subscriber.subscriber_number = "100000127"
    db_session.commit()

    radius_db = tmp_path / "radius-opaque.db"
    _write_external_radius_db(
        radius_db,
        [
            (
                "100000127",
                "Cleartext-Password",
                ":=",
                "fHbF0nDj7iT/NYQTWcpUYvpxAEZkGfMofjjQukY=",
            )
        ],
    )

    result = radius_service.import_access_credentials_from_external_radius(
        db_session,
        config={
            "db_url": f"sqlite:///{radius_db}",
            "radcheck_table": '"radcheck"',
        },
    )

    credential = (
        db_session.query(AccessCredential)
        .filter(AccessCredential.username == "100000127")
        .one()
    )
    assert credential.subscriber_id == subscriber.id
    assert credential.secret_hash is None
    assert result["created"] == 1
    assert result["matched_subscriber_number"] == 1
    assert result["secrets_skipped"] == 1


def test_import_access_credentials_from_external_radius_reports_unmatched(
    db_session, tmp_path
):
    radius_db = tmp_path / "radius-unmatched.db"
    _write_external_radius_db(
        radius_db,
        [("unmatched-user", "Cleartext-Password", ":=", "secret123")],
    )

    result = radius_service.import_access_credentials_from_external_radius(
        db_session,
        config={
            "db_url": f"sqlite:///{radius_db}",
            "radcheck_table": '"radcheck"',
        },
    )

    assert result["created"] == 0
    assert result["unmatched"] == 1
    assert result["unmatched_examples"] == ["unmatched-user"]


def test_create_radius_sync_job(db_session, radius_server):
    """Test creating a RADIUS sync job."""
    job = radius_service.radius_sync_jobs.create(
        db_session,
        RadiusSyncJobCreate(
            name="Daily Sync",
            server_id=radius_server.id,
        ),
    )
    assert job.name == "Daily Sync"
    assert job.server_id == radius_server.id


def test_list_radius_sync_jobs(db_session, radius_server):
    """Test listing RADIUS sync jobs."""
    radius_service.radius_sync_jobs.create(
        db_session,
        RadiusSyncJobCreate(name="Job 1", server_id=radius_server.id),
    )
    radius_service.radius_sync_jobs.create(
        db_session,
        RadiusSyncJobCreate(name="Job 2", server_id=radius_server.id),
    )

    jobs = radius_service.radius_sync_jobs.list(
        db_session,
        server_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(jobs) >= 2


def test_get_radius_server(db_session):
    """Test getting a RADIUS server by ID."""
    server = radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(
            name="Get Test",
            host="get.test.local",
            description="Test server",
        ),
    )
    fetched = radius_service.radius_servers.get(db_session, str(server.id))
    assert fetched is not None
    assert fetched.id == server.id
    assert fetched.name == "Get Test"


def test_update_radius_client(db_session, radius_server):
    """Test updating a RADIUS client."""
    client = radius_service.radius_clients.create(
        db_session,
        RadiusClientCreate(
            server_id=radius_server.id,
            client_ip="10.0.0.100",
            shared_secret_hash="old-secret",
        ),
    )
    updated = radius_service.radius_clients.update(
        db_session,
        str(client.id),
        RadiusClientUpdate(description="Updated description"),
    )
    assert updated.description == "Updated description"


def test_delete_radius_client(db_session, radius_server):
    """Test deleting a RADIUS client."""
    client = radius_service.radius_clients.create(
        db_session,
        RadiusClientCreate(
            server_id=radius_server.id,
            client_ip="10.0.0.200",
            shared_secret_hash="delete-me",
        ),
    )
    radius_service.radius_clients.delete(db_session, str(client.id))
    db_session.refresh(client)
    assert client.is_active is False


@patch("app.services.radius.active_external_radius_targets", return_value=[])
def test_reconcile_subscription_connectivity_creates_internal_radius_state(
    _active_external_radius_targets,
    db_session,
    subscriber,
    catalog_offer,
    radius_server,
):
    """Keep the user observation unchanged when no user target can converge."""
    nas_device = NasDevice(
        name="Edge NAS",
        vendor=NasVendor.mikrotik,
        nas_ip="10.10.10.1",
        management_ip="10.10.10.1",
        shared_secret="plain:radius-secret",
        is_active=True,
    )
    db_session.add(nas_device)
    db_session.flush()

    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        provisioning_nas_device_id=nas_device.id,
        status=SubscriptionStatus.active,
        login="10005030",
    )
    db_session.add(subscription)
    db_session.flush()

    credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="10005030",
        secret_hash="hashed-secret",
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    result = radius_service.reconcile_subscription_connectivity(
        db_session, str(subscription.id)
    )

    assert result.ok is False
    assert result.disposition is (
        radius_service.ConnectivityProjectionDisposition.target_unavailable
    )
    assert result.radius_clients_changed == 1
    assert result.radius_users_changed == 0
    assert result.external_nas_synced == 0
    assert result.external_credentials_synced == 0
    assert (
        db_session.query(RadiusUser)
        .filter(RadiusUser.access_credential_id == credential.id)
        .one_or_none()
        is None
    )

    client = (
        db_session.query(RadiusClient)
        .filter(RadiusClient.server_id == radius_server.id)
        .filter(RadiusClient.nas_device_id == nas_device.id)
        .one()
    )
    assert client.client_ip == "10.10.10.1"
    assert client.shared_secret_hash == radius_service._hash_secret("radius-secret")
    assert client.description == "Edge NAS"


# =============================================================================
# Radius Auth Tests
# =============================================================================


class TestRadiusAuthSettingValue:
    """Tests for _setting_value function."""

    def test_returns_value_text(self, db_session):
        """Test returns value_text when set."""
        setting = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_server_id",
            value_type=SettingValueType.string,
            value_text="server-123",
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = radius_auth._setting_value(db_session, "auth_server_id")
        assert result == "server-123"

    def test_returns_value_json_as_string(self, db_session):
        """Test returns value_json as string when value_text is None."""
        setting = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_config",
            value_type=SettingValueType.json,
            value_json={"timeout": 5},
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = radius_auth._setting_value(db_session, "auth_config")
        assert "timeout" in result

    def test_returns_none_when_not_found(self, db_session):
        """Test returns None when setting not found."""
        result = radius_auth._setting_value(db_session, "nonexistent_key")
        assert result is None

    def test_ignores_inactive_settings(self, db_session):
        """Test ignores inactive settings."""
        setting = DomainSetting(
            domain=SettingDomain.radius,
            key="inactive_key",
            value_type=SettingValueType.string,
            value_text="value",
            is_active=False,
        )
        db_session.add(setting)
        db_session.commit()

        result = radius_auth._setting_value(db_session, "inactive_key")
        assert result is None


class TestRadiusAuthPickServer:
    """Tests for _pick_radius_server function."""

    def test_picks_server_by_id(self, db_session, radius_server):
        """Test picks specific server by ID."""
        server = radius_auth._pick_radius_server(db_session, str(radius_server.id))
        assert server.id == radius_server.id

    def test_picks_server_from_setting(self, db_session, radius_server):
        """Test picks server from settings when no ID provided."""
        setting = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_server_id",
            value_type=SettingValueType.string,
            value_text=str(radius_server.id),
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        server = radius_auth._pick_radius_server(db_session, None)
        assert server.id == radius_server.id

    def test_picks_most_recent_when_no_preference(self, db_session):
        """Test picks most recent server when no preference."""
        server1 = RadiusServer(name="Old", host="old.local", is_active=True)
        db_session.add(server1)
        db_session.commit()

        server2 = RadiusServer(name="New", host="new.local", is_active=True)
        db_session.add(server2)
        db_session.commit()

        server = radius_auth._pick_radius_server(db_session, None)
        assert server.id == server2.id

    def test_raises_when_no_server_configured(self, db_session):
        """Test raises HTTPException when no server available."""
        # Deactivate all servers
        db_session.query(RadiusServer).update({RadiusServer.is_active: False})
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            radius_auth._pick_radius_server(db_session, None)

        assert exc_info.value.status_code == 400
        assert "not configured" in exc_info.value.detail


class TestRadiusAuthenticate:
    """Tests for authenticate function."""

    def test_raises_without_shared_secret(self, db_session, radius_server):
        """Test raises when shared secret not configured."""
        with pytest.raises(HTTPException) as exc_info:
            radius_auth.authenticate(db_session, "user", "pass", str(radius_server.id))

        assert exc_info.value.status_code == 400
        assert "secret not configured" in exc_info.value.detail

    def test_raises_on_dictionary_error(self, db_session, radius_server):
        """Test raises when dictionary file not available."""
        secret = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_shared_secret",
            value_type=SettingValueType.string,
            value_text="secret123",
            is_active=True,
        )
        db_session.add(secret)
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            radius_auth.authenticate(db_session, "user", "pass", str(radius_server.id))

        assert exc_info.value.status_code == 500
        assert "dictionary" in exc_info.value.detail.lower()

    def test_raises_on_timeout(self, db_session, radius_server):
        """Test raises on RADIUS timeout."""
        secret = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_shared_secret",
            value_type=SettingValueType.string,
            value_text="secret123",
            is_active=True,
        )
        db_session.add(secret)
        db_session.commit()

        mock_dict = MagicMock()
        mock_client = MagicMock()
        mock_client.SendPacket.side_effect = TimeoutError("Timeout")

        with patch("app.services.radius_auth.Dictionary", return_value=mock_dict):
            with patch("app.services.radius_auth.Client", return_value=mock_client):
                with pytest.raises(HTTPException) as exc_info:
                    radius_auth.authenticate(
                        db_session, "user", "pass", str(radius_server.id)
                    )

                assert exc_info.value.status_code == 502
                assert "timeout" in exc_info.value.detail.lower()

    def test_raises_on_send_error(self, db_session, radius_server):
        """Test raises on RADIUS send error."""
        secret = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_shared_secret",
            value_type=SettingValueType.string,
            value_text="secret123",
            is_active=True,
        )
        db_session.add(secret)
        db_session.commit()

        mock_dict = MagicMock()
        mock_client = MagicMock()
        mock_client.SendPacket.side_effect = Exception("Connection refused")

        with patch("app.services.radius_auth.Dictionary", return_value=mock_dict):
            with patch("app.services.radius_auth.Client", return_value=mock_client):
                with pytest.raises(HTTPException) as exc_info:
                    radius_auth.authenticate(
                        db_session, "user", "pass", str(radius_server.id)
                    )

                assert exc_info.value.status_code == 502
                assert "failed" in exc_info.value.detail.lower()

    def test_raises_on_access_reject(self, db_session, radius_server):
        """Test raises on access reject."""
        secret = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_shared_secret",
            value_type=SettingValueType.string,
            value_text="secret123",
            is_active=True,
        )
        db_session.add(secret)
        db_session.commit()

        mock_dict = MagicMock()
        mock_client = MagicMock()
        mock_reply = MagicMock()
        mock_reply.code = 3  # AccessReject
        mock_reply.AccessAccept = 2  # Different from reply.code
        mock_client.SendPacket.return_value = mock_reply

        with patch("app.services.radius_auth.Dictionary", return_value=mock_dict):
            with patch("app.services.radius_auth.Client", return_value=mock_client):
                with pytest.raises(HTTPException) as exc_info:
                    radius_auth.authenticate(
                        db_session, "user", "pass", str(radius_server.id)
                    )

                assert exc_info.value.status_code == 401
                assert "credentials" in exc_info.value.detail.lower()

    def test_success_on_access_accept(self, db_session, radius_server):
        """Test success on access accept."""
        secret = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_shared_secret",
            value_type=SettingValueType.string,
            value_text="secret123",
            is_active=True,
        )
        db_session.add(secret)
        db_session.commit()

        mock_dict = MagicMock()
        mock_client = MagicMock()
        mock_reply = MagicMock()
        mock_reply.code = 2  # AccessAccept
        mock_reply.AccessAccept = 2  # Same as reply.code
        mock_client.SendPacket.return_value = mock_reply
        mock_packet = MagicMock()
        mock_client.CreateAuthPacket.return_value = mock_packet

        with patch("app.services.radius_auth.Dictionary", return_value=mock_dict):
            with patch("app.services.radius_auth.Client", return_value=mock_client):
                # Should not raise
                radius_auth.authenticate(
                    db_session, "testuser", "testpass", str(radius_server.id)
                )


# =============================================================================
# Block / Unblock external RADIUS credentials
# (Group/Auth-Type-based blocking — see app/services/radius.py)
# =============================================================================


def _write_full_radius_sqlite(
    db_path, *, radcheck=None, radreply=None, radusergroup=None
):
    """Set up a sqlite stand-in for the external FreeRADIUS DB with all three
    tables `_external_sync_users` operates on."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE radcheck (username TEXT, attribute TEXT, op TEXT, value TEXT)"
        )
        conn.execute(
            "CREATE TABLE radreply (username TEXT, attribute TEXT, op TEXT, value TEXT)"
        )
        conn.execute(
            "CREATE TABLE radusergroup (username TEXT, groupname TEXT, priority INTEGER)"
        )
        for row in radcheck or []:
            conn.execute(
                "INSERT INTO radcheck (username, attribute, op, value) VALUES (?, ?, ?, ?)",
                row,
            )
        for row in radreply or []:
            conn.execute(
                "INSERT INTO radreply (username, attribute, op, value) VALUES (?, ?, ?, ?)",
                row,
            )
        for row in radusergroup or []:
            conn.execute(
                "INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, ?)",
                row,
            )
        conn.commit()
    finally:
        conn.close()


def _fake_external_config(db_path):
    return {
        "db_url": f"sqlite:///{db_path}",
        "radcheck_table": "radcheck",
        "radreply_table": "radreply",
        "radusergroup_table": "radusergroup",
        "nas_table": "nas",
        "password_attribute": "Cleartext-Password",
        "password_op": ":=",
        "use_group": False,
        "group_priority": 0,
        "default_reply_op": ":=",
    }


def _read_radcheck(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return list(
            conn.execute(
                "SELECT username, attribute, op, value FROM radcheck ORDER BY rowid"
            )
        )
    finally:
        conn.close()


def _read_radreply(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return list(
            conn.execute(
                "SELECT username, attribute, op, value FROM radreply ORDER BY rowid"
            )
        )
    finally:
        conn.close()


def test_external_nas_lifecycle_helpers_do_not_read_or_return_secrets(
    db_session, tmp_path, monkeypatch
):
    db_path = tmp_path / "radius-nas-lifecycle.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE nas (nasname TEXT, shortname TEXT, type TEXT, "
            "secret TEXT, description TEXT)"
        )
        conn.executemany(
            "INSERT INTO nas (nasname, secret) VALUES (?, ?)",
            [("10.0.0.1", "secret-one"), ("10.0.0.2", "secret-two")],
        )
        conn.commit()
    finally:
        conn.close()
    config = _fake_external_config(db_path)
    monkeypatch.setattr(
        radius_service, "_active_external_sync_configs", lambda _db: [config]
    )

    found = radius_service.external_radius_nas_client_ips(
        db_session, {"10.0.0.1", "10.0.0.3"}
    )
    inventory = radius_service.external_radius_nas_secret_inventory(
        db_session, {"10.0.0.1", "10.0.0.3"}
    )
    removed = radius_service.remove_external_radius_nas_clients(
        db_session, {"10.0.0.1"}
    )

    assert found == {"10.0.0.1"}
    assert inventory.present_client_ips == {"10.0.0.1"}
    assert inventory.recoverable_secrets == {"10.0.0.1": "secret-one"}
    assert inventory.conflicting_client_ips == set()
    assert removed == 1
    conn = sqlite3.connect(db_path)
    try:
        remaining = list(conn.execute("SELECT nasname FROM nas ORDER BY nasname"))
    finally:
        conn.close()
    assert remaining == [("10.0.0.2",)]


def test_authoritative_external_radius_db_url_requires_one_target(
    db_session, monkeypatch
):
    from app.services import external_radius_targets

    monkeypatch.setattr(
        external_radius_targets,
        "active_external_radius_targets",
        lambda _db: [
            {
                "db_url": "postgresql+psycopg://radius-one/radius",
                "authoritative_accounting": False,
            },
            {
                "db_url": "postgresql+psycopg://radius-one/radius",
                "authoritative_accounting": False,
            },
        ],
    )
    with pytest.raises(RuntimeError, match="authoritative_accounting"):
        external_radius_targets.authoritative_external_radius_db_url(db_session)

    monkeypatch.setattr(
        external_radius_targets,
        "active_external_radius_targets",
        lambda _db: [
            {
                "db_url": "postgresql+psycopg://radius-one/radius",
                "authoritative_accounting": True,
            },
            {
                "db_url": "postgresql+psycopg://radius-two/radius",
                "authoritative_accounting": False,
            },
        ],
    )
    assert external_radius_targets.authoritative_external_radius_db_url(db_session) == (
        "postgresql+psycopg://radius-one/radius"
    )


def test_radius_nas_lifecycle_projects_active_and_inactive_state(
    db_session, radius_server, tmp_path, monkeypatch
):
    db_path = tmp_path / "radius-nas-reconcile.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE nas (nasname TEXT, shortname TEXT, type TEXT, "
            "secret TEXT, description TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
    config = _fake_external_config(db_path)
    monkeypatch.setattr(
        radius_service, "_active_external_sync_configs", lambda _db: [config]
    )
    device = NasDevice(
        name="lifecycle-router",
        vendor=NasVendor.mikrotik,
        nas_ip="10.40.0.1",
        shared_secret="plain:radius-secret",
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()

    active = radius_service.apply_radius_nas_lifecycle(db_session, device, active=True)
    db_session.flush()
    # The linked client remains authoritative lifecycle evidence while a NAS IP
    # correction is pending projection to the external store.
    device.nas_ip = "10.40.0.2"
    db_session.flush()
    active_state = radius_service.radius_nas_lifecycle_state(db_session, device)

    assert active.internal_clients_changed == 1
    assert active.external_clients_changed == 1
    assert active_state.internal_active_clients == 1
    assert active_state.external_present is True

    inactive = radius_service.apply_radius_nas_lifecycle(
        db_session, device, active=False
    )
    db_session.flush()
    inactive_state = radius_service.radius_nas_lifecycle_state(db_session, device)

    assert inactive.internal_clients_changed == 1
    assert inactive.external_clients_changed == 1
    assert inactive_state.internal_active_clients == 0
    assert inactive_state.external_present is False


class TestRadiusSyncSubscriptionSelection:
    """_radius_sync_subscription_for_subscriber must prefer an active
    subscription when a subscriber has several eligible ones (migration
    duplicate-service customers carry active + canceled/suspended rows)."""

    def test_prefers_active_over_later_dated_canceled(
        self, db_session, subscriber, catalog_offer
    ):
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        # Canceled row with the LATER start_at — would win by date alone.
        canceled = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.canceled,
            login="canceled-login",
            start_at=now,
        )
        # Active row with an EARLIER start_at.
        active = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
            login="active-login",
            start_at=now - timedelta(days=30),
        )
        db_session.add_all([canceled, active])
        db_session.commit()

        picked = radius_service._radius_sync_subscription_for_subscriber(
            db_session, subscriber.id
        )
        assert picked is not None
        assert picked.id == active.id  # active wins despite the earlier date

    def test_falls_back_to_eligible_when_no_active(
        self, db_session, subscriber, catalog_offer
    ):
        canceled = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.canceled,
            login="only-canceled",
        )
        db_session.add(canceled)
        db_session.commit()

        picked = radius_service._radius_sync_subscription_for_subscriber(
            db_session, subscriber.id
        )
        assert picked is not None
        assert picked.id == canceled.id  # unchanged behaviour when no active
