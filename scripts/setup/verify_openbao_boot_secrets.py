"""Fail deployment safely when a required OpenBao boot secret is unavailable."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.services.kernel_secret_source import SECRET_REFS
from app.services.secrets import resolve_openbao_ref


@dataclass(frozen=True, slots=True)
class BootSecretPreflightResult:
    """Names checked and names that could not provide a non-empty value."""

    checked_names: tuple[str, ...]
    failed_names: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failed_names


def check_required_boot_secrets(
    refs: Mapping[str, str] = SECRET_REFS,
    resolver: Callable[[str], str] = resolve_openbao_ref,
) -> BootSecretPreflightResult:
    """Resolve required fields without returning or logging their values."""
    checked: list[str] = []
    failed: list[str] = []
    for name, reference in refs.items():
        checked.append(name)
        try:
            value = resolver(reference)
        except Exception:  # The deployment needs only the failed field name.
            failed.append(name)
            continue
        if not value.strip():
            failed.append(name)
    return BootSecretPreflightResult(tuple(checked), tuple(failed))


def main() -> int:
    result = check_required_boot_secrets()
    if not result.ok:
        names = ", ".join(result.failed_names)
        print(f"OpenBao boot-secret preflight failed for: {names}")
        return 1
    print(
        "OpenBao boot-secret preflight passed for "
        f"{len(result.checked_names)} required fields."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
