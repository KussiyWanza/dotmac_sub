from __future__ import annotations

from scripts.setup.verify_openbao_boot_secrets import (
    check_required_boot_secrets,
    report_optional_boot_material,
)


def test_preflight_accepts_non_empty_required_fields() -> None:
    refs = {"first": "bao://secret/settings/auth#first", "second": "ref-two"}

    result = check_required_boot_secrets(refs, lambda reference: f"value:{reference}")

    assert result.ok
    assert result.checked_names == ("first", "second")
    assert result.failed_names == ()


def test_preflight_reports_only_names_for_empty_and_unavailable_fields() -> None:
    refs = {"empty": "empty-ref", "unavailable": "unavailable-ref"}

    def resolve(reference: str) -> str:
        if reference == "unavailable-ref":
            raise RuntimeError("sensitive-value-must-not-be-reported")
        return "   "

    result = check_required_boot_secrets(refs, resolve)

    assert not result.ok
    assert result.failed_names == ("empty", "unavailable")


def test_optional_material_is_reported_and_never_gates() -> None:
    """Absence is a legitimate state for material belonging to one feature.

    A deployment not using prepaid reconstruction has no attestation anchor,
    and one that has not started encrypting secret settings has no keyring.
    Failing a deploy over either would make the preflight useless for the
    deployments that need it — so this reports and returns names, and `main`
    keeps its exit code from the REQUIRED check alone.
    """

    refs = {"anchor": "anchor-ref", "keyring": "keyring-ref"}

    def resolve(reference: str) -> str:
        if reference == "anchor-ref":
            raise RuntimeError("sensitive-value-must-not-be-reported")
        return "   "

    assert report_optional_boot_material(refs, resolve) == ("anchor", "keyring")


def test_provisioned_optional_material_is_not_reported() -> None:
    refs = {"anchor": "anchor-ref"}

    assert report_optional_boot_material(refs, lambda _ref: "-----BEGIN…") == ()


def test_the_optional_set_is_the_one_the_application_holds_optionally() -> None:
    """The script must not keep its own list of what is optional.

    Two lists would drift, and the drift is invisible: a name the application
    holds optionally but this script omits is simply never reported, which is
    the outcome the report exists to prevent.
    """

    from app.services.kernel_key_provider import KEYRING_REF
    from app.services.kernel_secret_source import OPTIONAL_SECRET_REFS
    from scripts.setup.verify_openbao_boot_secrets import OPTIONAL_REFS

    assert set(OPTIONAL_REFS) == set(OPTIONAL_SECRET_REFS) | {
        "settings_encryption_keyring"
    }
    assert OPTIONAL_REFS["settings_encryption_keyring"] == KEYRING_REF
