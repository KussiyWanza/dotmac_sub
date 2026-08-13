"""Sub's settings-encryption keyring: what it loads, and how it fails.

The failure path is the contract. `dotmac_kernel.settings_crypto` treats what a
`KeyProvider` returns as the complete keyring, so a provider that returns
nothing when OpenBao is unreachable would have the kernel install an empty one —
and every secret-setting write would then fail with "no active key", which is
indistinguishable from a deployment that has simply not created a keyring yet.

Those two states must be told apart, and only one of them may be quiet:

* the path does not exist — nothing is configured, return nothing;
* anything else — raise, and let the boot fail.
"""

from __future__ import annotations

import json

import pytest
from dotmac_kernel.settings_crypto import KeyStatus

from app.services import kernel_key_provider as provider

KEYRING = [
    {"key_id": "k1", "key": "material-one", "status": "retired"},
    {"key_id": "k2", "key": "material-two"},
]


def test_the_reference_names_one_openbao_field() -> None:
    assert provider.KEYRING_REF.startswith("bao://")
    assert "#" in provider.KEYRING_REF, "the reference names no field"


def test_it_satisfies_the_kernel_protocol() -> None:
    from dotmac_kernel.settings_crypto import KeyProvider

    assert isinstance(provider.OpenBaoKeyProvider(), KeyProvider)


def test_a_stored_keyring_loads_with_its_statuses(monkeypatch) -> None:
    monkeypatch.setattr(
        provider, "resolve_openbao_ref_optional", lambda _ref: json.dumps(KEYRING)
    )

    keys = list(provider.OpenBaoKeyProvider().load_keys())

    assert [key.key_id for key in keys] == ["k1", "k2"]
    assert keys[0].status is KeyStatus.RETIRED
    # Defaulted, because a keyring whose entries must all say "active" would
    # make the common case the noisy one.
    assert keys[1].status is KeyStatus.ACTIVE
    assert keys[1].material == "material-two"


def test_a_missing_path_means_no_keyring_yet(monkeypatch) -> None:
    """The one case where returning nothing is honest.

    Every secret setting Sub holds today is a `bao://` reference, so encryption
    becomes POSSIBLE before it becomes used — a deployment must boot before its
    keyring exists.
    """

    monkeypatch.setattr(provider, "resolve_openbao_ref_optional", lambda _ref: None)

    assert list(provider.OpenBaoKeyProvider().load_keys()) == []


def test_an_unreachable_store_raises(monkeypatch) -> None:
    """The load-bearing test.

    Returning nothing here would be read as "no keyring configured", and every
    secret write would then fail at a settings screen hours later rather than
    at the boot that could not reach the store.
    """

    class _Unreachable(RuntimeError):
        pass

    def _unreachable(_ref: str) -> str:
        raise _Unreachable("openbao request failed")

    monkeypatch.setattr(provider, "resolve_openbao_ref_optional", _unreachable)

    with pytest.raises(_Unreachable):
        provider.OpenBaoKeyProvider().load_keys()


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"key_id": "k1", "key": "m"}',
        '[{"key_id": "k1"}]',
        '[{"key_id": "k1", "key": "m", "status": "sideways"}]',
    ],
    ids=["not-json", "not-a-list", "missing-key", "unknown-status"],
)
def test_a_malformed_keyring_is_named_not_guessed(monkeypatch, raw) -> None:
    from dotmac_kernel.settings_crypto import KeyringError

    monkeypatch.setattr(provider, "resolve_openbao_ref_optional", lambda _ref: raw)

    with pytest.raises(KeyringError):
        provider.OpenBaoKeyProvider().load_keys()


def test_no_material_reaches_an_error_message(monkeypatch) -> None:
    """A keyring error names ids and fields; the key itself is the whole secret."""

    from dotmac_kernel.settings_crypto import KeyringError

    material = "AAAA-actual-key-material-AAAA"
    monkeypatch.setattr(
        provider,
        "resolve_openbao_ref_optional",
        lambda _ref: json.dumps([{"key_id": "k1", "key": material, "status": "?"}]),
    )

    with pytest.raises(KeyringError) as excinfo:
        provider.OpenBaoKeyProvider().load_keys()
    assert material not in str(excinfo.value)


def test_no_material_is_logged(monkeypatch, caplog) -> None:
    import logging

    material = "BBBB-actual-key-material-BBBB"
    monkeypatch.setattr(
        provider,
        "resolve_openbao_ref_optional",
        lambda _ref: json.dumps([{"key_id": "k1", "key": material}]),
    )

    with caplog.at_level(logging.DEBUG, logger=provider.__name__):
        provider.OpenBaoKeyProvider().load_keys()

    assert material not in caplog.text
    assert "k1" in caplog.text


def test_install_is_skipped_when_no_openbao_is_configured(monkeypatch) -> None:
    """Without OpenBao the kernel's own environment keyring applies, so a
    developer machine and a CI shard are unchanged."""

    from app.services import secrets as secrets_module

    monkeypatch.setattr(secrets_module, "is_openbao_configured", lambda: False)
    monkeypatch.setattr(
        provider,
        "resolve_openbao_ref_optional",
        lambda _ref: pytest.fail("must not reach OpenBao"),
    )

    assert provider.install_if_configured() == ()


def test_install_returns_ids_only(monkeypatch) -> None:
    from dotmac_kernel.settings_crypto import clear_key_provider

    from app.services import secrets as secrets_module

    monkeypatch.setattr(secrets_module, "is_openbao_configured", lambda: True)
    monkeypatch.setattr(
        provider, "resolve_openbao_ref_optional", lambda _ref: json.dumps(KEYRING)
    )

    try:
        assert provider.install_if_configured() == ("k1", "k2")
    finally:
        clear_key_provider()
