from __future__ import annotations

from scripts.setup.verify_openbao_boot_secrets import check_required_boot_secrets


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
