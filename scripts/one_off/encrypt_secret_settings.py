"""Convert secret settings from `bao://` references to ciphertext at rest.

Every `is_secret` settings row used to hold a REFERENCE — the value lived in
OpenBao and `value_text` pointed at it. That put a network call on the read path
of every secret setting (starter ADR-0009's forbidden shape) and, at three call
sites that never dereferenced, handed a provider the literal string `bao://…`
as its credential.

Secret settings are now stored as `enc:<key_id>:<token>` and decrypted by the
kernel resolver. This resolves each remaining reference once and rewrites the
row with the ciphertext.

## Why a one-off and not a migration

It needs two things a migration must never need: OpenBao reachable, and an
active settings-encryption key. A migration runs unattended inside `deploy.sh`,
before the application is up, and failing there blocks a deployment on
infrastructure that has nothing to do with the schema.

Nothing depends on this having run. Both readers and the write path tolerate a
reference — `resolve_secret` passes plaintext through and dereferences a
reference — so the estate keeps working while it is half converted, and the
tolerance is removed in a later slice once no references remain.

## Safety

* **Dry run by default.** `--apply` performs writes.
* **Idempotent.** A row already carrying `enc:` is skipped, so a re-run after a
  partial failure converts only what is left.
* **One row at a time, committed as it goes.** A failure converts fewer rows
  rather than rolling back the ones that worked; each is independently valid
  because a reference and a ciphertext are both readable.
* **No value is ever printed or logged** — not the reference, not the
  plaintext, not the ciphertext. Only `domain.key`, which is what an operator
  needs to fix one.

Usage:

    python -m scripts.one_off.encrypt_secret_settings            # dry run
    python -m scripts.one_off.encrypt_secret_settings --apply
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting
from app.services.secrets import is_secret_ref, resolve_secret


@dataclass
class ConversionReport:
    """What the run found and did. Names only — never values."""

    converted: list[str] = field(default_factory=list)
    already_ciphertext: list[str] = field(default_factory=list)
    unresolvable: list[str] = field(default_factory=list)
    plaintext_encrypted: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolvable


def _label(row: DomainSetting) -> str:
    return f"{row.domain}.{row.key}"


def convert_secret_settings(db: Session, *, apply: bool) -> ConversionReport:
    """Rewrite every secret row that still holds a reference."""

    from dotmac_kernel.settings_crypto import encrypt_value, is_encrypted

    report = ConversionReport()
    rows = (
        db.query(DomainSetting)
        .filter(DomainSetting.is_secret.is_(True))
        .order_by(DomainSetting.domain, DomainSetting.key)
        .all()
    )

    for row in rows:
        stored = (row.value_text or "").strip()
        if not stored:
            continue
        if is_encrypted(stored):
            report.already_ciphertext.append(_label(row))
            continue
        if not is_secret_ref(stored):
            # A value stored before OpenBao existed, in the clear. Encrypting it
            # is exactly what the write path now does, so it is converted too —
            # this is the row the whole change exists for.
            try:
                ciphertext = encrypt_value(stored)
            except Exception:
                report.unresolvable.append(_label(row))
                continue
            report.plaintext_encrypted.append(_label(row))
            if apply:
                row.value_text = ciphertext
                db.commit()
            continue

        try:
            plaintext = resolve_secret(stored)
        except Exception:
            # The reference does not resolve — a retired path, a revoked token,
            # a typo. Reported by NAME so an operator can look, and left alone:
            # the row still reads exactly as it did before this ran.
            report.unresolvable.append(_label(row))
            continue
        if not plaintext or not str(plaintext).strip():
            report.unresolvable.append(_label(row))
            continue

        try:
            ciphertext = encrypt_value(str(plaintext).strip())
        except Exception:
            report.unresolvable.append(_label(row))
            continue

        report.converted.append(_label(row))
        if apply:
            row.value_text = ciphertext
            db.commit()

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the rewrite. Without it the run only reports.",
    )
    args = parser.parse_args(argv)

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        report = convert_secret_settings(db, apply=args.apply)
    finally:
        db.close()

    mode = "converted" if args.apply else "would convert"
    print(f"{mode}: {len(report.converted)}")
    for label in report.converted:
        print(f"  {label}")
    if report.plaintext_encrypted:
        print(f"{mode} (was stored in the clear): {len(report.plaintext_encrypted)}")
        for label in report.plaintext_encrypted:
            print(f"  {label}")
    if report.already_ciphertext:
        print(f"already ciphertext: {len(report.already_ciphertext)}")
    if report.unresolvable:
        print(f"UNRESOLVABLE — left unchanged: {len(report.unresolvable)}")
        for label in report.unresolvable:
            print(f"  {label}")
        print(
            "Each of these still reads as it did before. Check the reference "
            "resolves and that a settings-encryption key is active, then re-run."
        )
    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
