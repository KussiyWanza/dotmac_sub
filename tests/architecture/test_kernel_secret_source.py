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


def test_the_optional_set_holds_only_the_trust_anchor() -> None:
    """Optional is a narrow exception, not a second general-purpose set.

    A name lands here only when its material belongs to ONE feature, so a
    deployment not using that feature has nothing to provision. Anything the
    whole process needs belongs in the required set above, where a missing
    value stops the boot.
    """

    assert set(kss.OPTIONAL_SECRET_REFS) == {"prepaid_attestation_public_key"}


def test_every_reference_points_at_openbao() -> None:
    for name, reference in {**kss.SECRET_REFS, **kss.OPTIONAL_SECRET_REFS}.items():
        assert reference.startswith("bao://"), f"{name} is not an OpenBao ref"
        assert "#" in reference, f"{name} names no field"


def test_no_name_is_both_required_and_optional() -> None:
    """Two entries for one name would make its absence mean two things."""

    assert not set(kss.SECRET_REFS) & set(kss.OPTIONAL_SECRET_REFS)


@pytest.fixture
def stub_openbao(monkeypatch):
    """Both readers stubbed. The optional set uses `_optional`, which returns
    None for a path that does not exist rather than raising, so a test that
    stubbed only the required reader would reach the real client."""

    def _stub(optional=lambda ref: f"value-for::{ref}"):
        monkeypatch.setattr(kss, "resolve_openbao_ref", lambda ref: f"value-for::{ref}")
        monkeypatch.setattr(kss, "resolve_openbao_ref_optional", optional)

    return _stub


def test_load_returns_every_declared_secret(stub_openbao) -> None:
    stub_openbao()
    loaded = kss.OpenBaoSecretSource().load()
    assert set(loaded) == set(kss.SECRET_REFS) | set(kss.OPTIONAL_SECRET_REFS)
    assert all(v.startswith("value-for::") for v in loaded.values())


def test_an_unprovisioned_optional_secret_is_skipped_not_fatal(stub_openbao) -> None:
    """The narrow exception to all-or-nothing.

    `OPTIONAL_SECRET_REFS` holds material needed by ONE feature. A deployment
    not using that feature has nothing to provision, and failing every boot
    over it would be a worse answer than the feature reporting itself
    unconfigured when asked.
    """

    stub_openbao(optional=lambda _ref: None)
    loaded = kss.OpenBaoSecretSource().load()

    assert set(loaded) == set(kss.SECRET_REFS)
    assert not set(loaded) & set(kss.OPTIONAL_SECRET_REFS)


def test_an_optional_secret_still_fails_the_load_when_the_store_errors(
    monkeypatch,
) -> None:
    """Optional means "the path may be absent", never "errors are tolerated".

    `resolve_openbao_ref_optional` returns None ONLY for a 404; unreachable, a
    bad token, or a missing field on a path that does exist all propagate. If
    that were relaxed here, an outage would read as "not provisioned" for the
    one secret whose absence is silent.
    """

    monkeypatch.setattr(kss, "resolve_openbao_ref", lambda ref: "ok")

    def _unreachable(_ref: str) -> str:
        raise _Boom("openbao unreachable")

    monkeypatch.setattr(kss, "resolve_openbao_ref_optional", _unreachable)
    with pytest.raises(_Boom):
        kss.OpenBaoSecretSource().load()


def test_an_unreachable_store_RAISES_rather_than_returning_empty(monkeypatch) -> None:
    """The load-bearing test. An empty mapping is indistinguishable from
    "nothing is configured", and the kernel would install it as a successful
    load — turning an outage into a silent misconfiguration."""

    def _unreachable(reference: str) -> str:
        raise _Boom("openbao unreachable")

    monkeypatch.setattr(kss, "resolve_openbao_ref", _unreachable)
    monkeypatch.setattr(kss, "resolve_openbao_ref_optional", lambda _ref: None)
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
    monkeypatch.setattr(kss, "resolve_openbao_ref_optional", lambda _ref: None)
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
    monkeypatch.setattr(kss, "resolve_openbao_ref_optional", lambda _ref: secret)
    with caplog.at_level(logging.DEBUG, logger=kss.__name__):
        kss.OpenBaoSecretSource().load()
    assert secret not in caplog.text


def test_it_satisfies_the_kernel_protocol() -> None:
    from dotmac_kernel.secret_sources import SecretSource

    assert isinstance(kss.OpenBaoSecretSource(), SecretSource)
