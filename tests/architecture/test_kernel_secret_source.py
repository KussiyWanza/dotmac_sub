"""Sub's `SecretSource` — what it loads, and how it fails.

The behaviour that matters is the failure path. `dotmac_kernel.secret_sources`
treats a successful return as the COMPLETE set of secrets this deployment holds,
so a source that returns partially, or empty, when OpenBao is unreachable would
have the kernel install that as success — and every consumer would then see
"not configured" rather than "could not reach the store".

Sub has two OpenBao readers and only one is safe here: `resolve_openbao_ref`
raises, `get_secret` swallows every exception and returns a default. Using the
convenient one would be the bug.
"""

from __future__ import annotations

import pytest

from app.services import kernel_secret_source as kss


class _Boom(RuntimeError):
    pass


def test_the_five_ruled_secrets_are_the_ones_declared() -> None:
    """Per the 2026-08-09 classification: three keys that protect data in this
    same database (so they must not live in it) plus two Dotmac-issued,
    boot-stable credentials."""
    assert set(kss.SECRET_REFS) == {
        "credential_encryption_key",
        "totp_encryption_key",
        "wireguard_key_encryption_key",
        "jwt_secret",
        "radius_auth_shared_secret",
    }


def test_every_reference_points_at_openbao() -> None:
    for name, reference in kss.SECRET_REFS.items():
        assert reference.startswith("bao://"), f"{name} is not an OpenBao ref"
        assert "#" in reference, f"{name} names no field"


def test_load_returns_every_declared_secret(monkeypatch) -> None:
    monkeypatch.setattr(kss, "resolve_openbao_ref", lambda ref: f"value-for::{ref}")
    loaded = kss.OpenBaoSecretSource().load()
    assert set(loaded) == set(kss.SECRET_REFS)
    assert all(v.startswith("value-for::") for v in loaded.values())


def test_an_unreachable_store_RAISES_rather_than_returning_empty(monkeypatch) -> None:
    """The load-bearing test. An empty mapping is indistinguishable from
    "nothing is configured", and the kernel would install it as a successful
    load — turning an outage into a silent misconfiguration."""

    def _unreachable(reference: str) -> str:
        raise _Boom("openbao unreachable")

    monkeypatch.setattr(kss, "resolve_openbao_ref", _unreachable)
    with pytest.raises(_Boom):
        kss.OpenBaoSecretSource().load()


def test_one_missing_secret_fails_the_whole_load(monkeypatch) -> None:
    """No partial sets. The kernel treats what it gets as complete, so a source
    that returned four of five would leave the fifth looking unconfigured."""

    def _one_missing(reference: str) -> str:
        if "jwt_secret" in reference:
            raise _Boom("field not found")
        return "ok"

    monkeypatch.setattr(kss, "resolve_openbao_ref", _one_missing)
    with pytest.raises(_Boom):
        kss.OpenBaoSecretSource().load()


def test_it_does_not_use_the_swallowing_reader() -> None:
    """`get_secret` returns a default on ANY exception — convenient for a caller
    with a fallback, catastrophic for a source whose silence the kernel trusts.
    Pinned because the two readers sit side by side in `app/services/secrets.py`
    and the wrong one is the more obvious choice."""
    from pathlib import Path

    source = Path(kss.__file__).read_text(encoding="utf-8")
    body = source.split("class OpenBaoSecretSource")[1]
    assert "resolve_openbao_ref" in body
    assert "get_secret(" not in body, (
        "the source used `get_secret`, which swallows failures and returns a "
        "default — the kernel would install that as a successful load"
    )


def test_load_never_logs_a_secret_value(monkeypatch, caplog) -> None:
    import logging

    secret = "AAAA-actual-secret-material-AAAA"
    monkeypatch.setattr(kss, "resolve_openbao_ref", lambda ref: secret)
    with caplog.at_level(logging.DEBUG, logger=kss.__name__):
        kss.OpenBaoSecretSource().load()
    assert secret not in caplog.text


def test_it_satisfies_the_kernel_protocol() -> None:
    from dotmac_kernel.secret_sources import SecretSource

    assert isinstance(kss.OpenBaoSecretSource(), SecretSource)
