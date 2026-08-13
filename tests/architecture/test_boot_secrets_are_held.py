"""The five boot-held secrets are read from memory, never from a store or a row.

`tests/architecture/test_kernel_secret_source.py` pins what the SOURCE does.
This pins what the READERS do, which is the half that was missing: the source
had no caller at all, so nothing installed it and every reader still went to
the database for a `bao://` reference and dereferenced it while handling a
request.

Starter ADR-0009 states the rule — a secret is held, never dereferenced — and
the failure it prevents is specific. A settings read is on a per-request path;
putting a network hop there turns a store outage into an outage of everything
that signs a token, verifies a TOTP code, decrypts a device credential or
authenticates against RADIUS.

Each test drives one reader with the held set installed and asserts two
things: the held value is what comes back, and no OpenBao call was attempted.
"""

from __future__ import annotations

import pytest
from dotmac_kernel.secret_sources import clear_secret_source, install_secret_source

from app.services import kernel_secret_source as kss

HELD = {
    "credential_encryption_key": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0=",
    "totp_encryption_key": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb0=",
    "wireguard_key_encryption_key": "cccccccccccccccccccccccccccccccccccccccccc0=",
    "jwt_secret": "held-jwt-secret",
    "radius_auth_shared_secret": "held-radius-secret",
}


class _Fixed:
    """A `SecretSource` over a literal mapping — no store, no I/O."""

    def load(self) -> dict[str, str]:
        return dict(HELD)


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch):
    """Install the five, and make any OpenBao call a test failure."""

    def _no_network(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            "a reader reached OpenBao while resolving a secret — the point of "
            "holding them is that nothing on a request path does this"
        )

    # `_fetch_secret_data` is `lru_cache`-wrapped and `secrets.clear_cache()`
    # calls `.cache_clear()` on it. A bare function has no such attribute, so
    # the stub carries one — otherwise any code path that clears the cache
    # (rotation does, and so does a session-scoped fixture) fails with an
    # AttributeError that says nothing about what this test is checking.
    _no_network.cache_clear = lambda: None  # type: ignore[attr-defined]

    from app.services import secrets as secrets_module

    monkeypatch.setattr(secrets_module, "resolve_openbao_ref", _no_network)
    monkeypatch.setattr(secrets_module, "_fetch_secret_data", _no_network)

    install_secret_source(_Fixed())
    try:
        yield
    finally:
        clear_secret_source()


@pytest.fixture
def nothing_held():
    """No source installed — a developer machine, or a CI shard."""

    clear_secret_source()
    yield
    clear_secret_source()


def test_the_readers_and_the_source_agree_on_every_name() -> None:
    """The names this file exercises ARE the names the source declares.

    A reader asking for `radius_shared_secret` when the source declares
    `radius_auth_shared_secret` would get `None` and fall through to "not
    configured" — a silent misconfiguration that no other test would catch,
    because both halves would look correct on their own.
    """

    assert set(HELD) == set(kss.SECRET_REFS)


def test_jwt_secret_comes_from_the_held_set(held, monkeypatch) -> None:
    from app.services import auth_flow

    monkeypatch.delenv("JWT_SECRET", raising=False)
    assert auth_flow._jwt_secret(None) == HELD["jwt_secret"]


def test_the_environment_still_wins_over_the_held_jwt_secret(held, monkeypatch) -> None:
    """Precedence is preserved, not reordered. `_jwt_secret` read the
    environment first before this change, and an operator who pinned a value
    there must not have it silently replaced by the store's."""

    from app.services import auth_flow

    monkeypatch.setenv("JWT_SECRET", "from-the-environment")
    assert auth_flow._jwt_secret(None) == "from-the-environment"


def test_totp_key_comes_from_the_held_set(held, monkeypatch) -> None:
    from app.services import auth_flow

    monkeypatch.delenv("TOTP_ENCRYPTION_KEY", raising=False)
    assert auth_flow._mfa_key(None) == HELD["totp_encryption_key"].encode()


def test_wireguard_key_comes_from_the_held_set(held, monkeypatch) -> None:
    from app.services import wireguard_crypto

    monkeypatch.delenv("WIREGUARD_KEY_ENCRYPTION_KEY", raising=False)
    assert (
        wireguard_crypto.get_encryption_key()
        == HELD["wireguard_key_encryption_key"].encode()
    )


def test_credential_key_comes_from_the_held_set(held, monkeypatch) -> None:
    from app.services import credential_crypto

    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    assert (
        credential_crypto.get_encryption_key()
        == HELD["credential_encryption_key"].encode()
    )


def test_radius_uses_the_held_secret_not_the_stored_reference(
    held, monkeypatch
) -> None:
    """The bug this closes, pinned.

    `radius/auth_shared_secret` WAS a setting declared `is_secret=True`, so
    every write through `DomainSettings` stored a `bao://…` REFERENCE in
    `value_text`. The old code read that column and passed it to
    `secret.encode("utf-8")`, handing the RADIUS client the literal string
    `bao://…` as the shared secret.

    The spec is retired now — it had no reader left, which
    `tests/architecture/test_no_orphan_settings.py` refuses — so this asserts
    the positive half: the secret comes from the held set, and no environment
    read stands in for it (`test_decision_input_ownership` rejects one here,
    since a business caller may not read the environment at runtime).
    """

    from app.services import radius_auth

    monkeypatch.setenv("RADIUS_AUTH_SHARED_SECRET", "must-not-be-used")

    class _Stop(Exception):
        """Ends `authenticate` once the client has been built with the secret."""

    class _Server:
        host = "radius.example.test"
        auth_port = 1812

    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.retries = 0
            self.timeout = 0.0

        def CreateAuthPacket(self, **_kwargs: object) -> object:  # noqa: N802
            raise _Stop

    monkeypatch.setattr(radius_auth, "_pick_radius_server", lambda *_a: _Server())
    monkeypatch.setattr(radius_auth, "Dictionary", lambda *_a: object())
    monkeypatch.setattr(radius_auth, "_setting_value", lambda *_a: None)
    monkeypatch.setattr(radius_auth, "Client", _Client)

    with pytest.raises(_Stop):
        radius_auth.authenticate(None, "user", "password")

    assert captured["secret"] == HELD["radius_auth_shared_secret"].encode("utf-8")


def test_no_source_installed_leaves_every_reader_on_its_environment(
    nothing_held, monkeypatch
) -> None:
    """The unconfigured deployment keeps working.

    `install_if_configured` holds nothing when no OpenBao is named, so a
    developer machine and a CI shard must behave exactly as they did before —
    otherwise the fail-closed boot would be traded for a fail-closed test run.
    """

    from app.services import auth_flow, credential_crypto, wireguard_crypto

    monkeypatch.setenv("JWT_SECRET", "env-jwt")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "env-credential-key")
    monkeypatch.setenv("WIREGUARD_KEY_ENCRYPTION_KEY", "env-wireguard-key")

    assert auth_flow._jwt_secret(None) == "env-jwt"
    assert credential_crypto.get_encryption_key() == b"env-credential-key"
    assert wireguard_crypto.get_encryption_key() == b"env-wireguard-key"


def test_install_is_skipped_when_no_openbao_is_configured(monkeypatch) -> None:
    monkeypatch.setattr(kss, "install", lambda: pytest.fail("should not install"))
    from app.services import secrets as secrets_module

    monkeypatch.setattr(secrets_module, "is_openbao_configured", lambda: False)
    assert kss.install_if_configured() == ()


def test_a_configured_but_unreachable_store_fails_the_boot(monkeypatch) -> None:
    """The load-bearing half of the gate.

    Skipping the install on a store that is configured and unreachable would
    hand every reader a `None` that reads as "not configured" — credential
    encryption silently off, reported as a warning line. So the gate is
    configuration, and an unreachable store raises.
    """

    from app.services import secrets as secrets_module

    monkeypatch.setattr(secrets_module, "is_openbao_configured", lambda: True)

    class _Unreachable(RuntimeError):
        pass

    def _boom(_reference: str) -> str:
        raise _Unreachable("openbao unreachable")

    monkeypatch.setattr(kss, "resolve_openbao_ref", _boom)
    with pytest.raises(Exception) as excinfo:
        kss.install_if_configured()
    # The kernel wraps a non-`SecretError` in `SecretSourceError`, naming the
    # TYPE only — a store client's message can quote the payload it choked on.
    assert "openbao unreachable" not in str(excinfo.value)
