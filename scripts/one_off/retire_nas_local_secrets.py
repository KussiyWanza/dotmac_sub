#!/usr/bin/env python
"""Retire shadowing NAS-local PPPoE secrets for an explicit cohort of logins.

A RouterOS ``/ppp secret`` bypasses the RADIUS projection instead of overriding
it, so every one is a second authority for a customer's address and access.
This CLI drives ``network.nas_local_secret_boundary`` to remove them. It makes
no decisions of its own: it collects provenance, previews through
``plan_cleanup``, and applies through ``apply_cleanup``.

Deliberately a CLI and not an HTTP route. Retiring a secret changes live
customer authentication on a production BNG; it should require a named host, a
named device and a named operator, not a session cookie.

Safety properties, none of them optional:

* **Preview by default.** ``--apply`` is required to touch the device.
* **Exact named NAS.** ``--nas`` must resolve to exactly one device by name or
  code. An ambiguous match is an error, never a guess.
* **Bounded explicit cohort.** ``--login`` (repeatable) or ``--logins-file``.
  There is no ``--all``: a fleet-wide blast is not an operator gesture.
* **Fingerprint gate.** ``--apply`` requires ``--fingerprint``, echoing the
  digest from the preview. If the device or the subscription cohort moved in
  between, the digest changes and the apply is refused.
* **Actor and reason** are required for ``--apply`` and recorded as operator
  provenance on the ``NetworkOperation``.
* **Count-only readback.** The device is probed for existence, never with
  ``print detail``, which would echo the stored PPP password.
* **Per-login verified outcomes.** One login's refusal or device failure never
  silently aborts or half-applies the rest.

Both targets must be named explicitly and separately: this runs on the
production application/DB host (which is NOT the NAS), and it reaches the NAS
named by ``--nas``. Neither is inferred.

Usage:

    # preview a cohort
    docker compose exec app python scripts/one_off/retire_nas_local_secrets.py \\
        --nas "Eagle Access" --login 100010053 --login 100024250

    # apply, echoing the fingerprint the preview printed
    docker compose exec app python scripts/one_off/retire_nas_local_secrets.py \\
        --nas "Eagle Access" --logins-file cohort1.txt \\
        --apply --fingerprint 9f2a1c0b4d5e6f70 \\
        --actor michael --reason "Eagle shadowing-secret sweep cohort 1"

Exit status is non-zero when any cohort member was refused or failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select

from app.db import SessionLocal
from app.models.catalog import NasDevice
from app.services.nas.local_secret_policy import (
    CleanupIntent,
    CleanupProvenance,
    LocalSecretCleanupError,
    LocalSecretCleanupRequest,
    ProvenanceKind,
    apply_cleanup,
    plan_cleanup,
)

MAX_COHORT = 50


class CohortError(RuntimeError):
    """The requested cohort or target could not be resolved unambiguously."""


def _resolve_nas(db: Any, needle: str) -> NasDevice:
    """Exactly one device, by exact name or code. Never a prefix guess."""
    devices = (
        db.execute(
            select(NasDevice).where(
                or_(NasDevice.name == needle, NasDevice.code == needle)
            )
        )
        .scalars()
        .all()
    )
    if not devices:
        raise CohortError(
            f"No NAS device matches {needle!r} exactly by name or code. "
            "Pass the exact device name; this tool does not guess."
        )
    if len(devices) > 1:
        names = ", ".join(sorted(str(d.id) for d in devices))
        raise CohortError(f"{needle!r} matches {len(devices)} devices ({names}).")
    return devices[0]


def _resolve_cohort(logins: list[str], logins_file: str | None) -> tuple[str, ...]:
    collected = [item.strip() for item in logins if item.strip()]
    if logins_file:
        text = Path(logins_file).read_text(encoding="utf-8")
        collected.extend(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    cohort = tuple(sorted(set(collected)))
    if not cohort:
        raise CohortError(
            "An explicit cohort is required: pass --login (repeatable) or "
            "--logins-file. There is no --all."
        )
    if len(cohort) > MAX_COHORT:
        raise CohortError(
            f"Cohort of {len(cohort)} exceeds the {MAX_COHORT}-login bound. "
            "Sweep in staged cohorts and verify each before expanding."
        )
    return cohort


def _cohort_fingerprint(plans: list[dict[str, Any]]) -> str:
    """One digest over every per-login fingerprint, in cohort order."""
    material = "|".join(f"{p['login']}:{p['fingerprint']}" for p in plans)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--nas",
        required=True,
        help="Exact NAS device name or code. Must match exactly one device.",
    )
    parser.add_argument(
        "--login",
        action="append",
        default=[],
        help="A login to retire. Repeatable. No --all exists.",
    )
    parser.add_argument(
        "--logins-file",
        help="File of logins, one per line; '#' comments ignored.",
    )
    parser.add_argument(
        "--intent",
        choices=[intent.value for intent in CleanupIntent],
        default=CleanupIntent.migrate_to_radius.value,
        help=(
            "migrate_to_radius: service continues and RADIUS must already serve "
            "it. terminal_retirement: service ended and RADIUS absence is "
            "expected."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually remove. Without this the run is a read-only preview.",
    )
    parser.add_argument("--fingerprint", help="Cohort digest from the preview run.")
    parser.add_argument("--actor", help="Operator identity recorded as provenance.")
    parser.add_argument("--reason", help="Why this cohort is being retired.")
    args = parser.parse_args()

    intent = CleanupIntent(args.intent)

    if args.apply:
        missing = [
            flag
            for flag, value in (
                ("--fingerprint", args.fingerprint),
                ("--actor", args.actor),
                ("--reason", args.reason),
            )
            if not (value or "").strip()
        ]
        if missing:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"--apply requires {', '.join(missing)}",
                    },
                    indent=2,
                )
            )
            return 2

    session = SessionLocal()
    try:
        try:
            device = _resolve_nas(session, args.nas)
            cohort = _resolve_cohort(args.login, args.logins_file)
        except CohortError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            return 2

        result: dict[str, Any] = {
            "nas": device.name,
            "nas_device_id": str(device.id),
            "intent": intent.value,
            "cohort_size": len(cohort),
            "applied": bool(args.apply),
            "plans": [],
            "outcomes": [],
            "refusals": [],
        }

        # Preview every member first, so the printed fingerprint covers the
        # whole cohort and a later apply cannot act on a shifted cohort.
        plans: list[dict[str, Any]] = []
        for login in cohort:
            request = LocalSecretCleanupRequest(
                nas_device_id=device.id,
                login=login,
                intent=intent,
                provenance=CleanupProvenance(
                    kind=ProvenanceKind.operator,
                    actor=(args.actor or "preview"),
                    reference=(args.reason or "preview"),
                ),
            )
            try:
                plan = plan_cleanup(session, request)
            except LocalSecretCleanupError as exc:
                result["refusals"].append(
                    {"login": login, "code": exc.code, "reason": exc.message}
                )
                continue
            plans.append(plan.as_payload())
        result["plans"] = plans
        result["cohort_fingerprint"] = _cohort_fingerprint(plans)

        if not args.apply:
            result["ok"] = not result["refusals"]
            result["next"] = (
                "Re-run with --apply --fingerprint "
                f"{result['cohort_fingerprint']} --actor <you> --reason <why>"
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["ok"] else 1

        if args.fingerprint != result["cohort_fingerprint"]:
            result["ok"] = False
            result["error"] = (
                f"Cohort fingerprint changed (given {args.fingerprint}, now "
                f"{result['cohort_fingerprint']}). Re-preview before applying."
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 2

        provenance = CleanupProvenance(
            kind=ProvenanceKind.operator,
            actor=args.actor,
            reference=args.reason,
        )
        for plan_payload in plans:
            login = str(plan_payload["login"])
            request = LocalSecretCleanupRequest(
                nas_device_id=device.id,
                login=login,
                intent=intent,
                provenance=provenance,
            )
            try:
                outcome = apply_cleanup(
                    session,
                    request,
                    expected_fingerprint=str(plan_payload["fingerprint"]),
                )
                session.commit()
                result["outcomes"].append(
                    {
                        "login": login,
                        "removed": outcome.removed,
                        "verified_absent": outcome.verified_absent,
                        "remaining_count": outcome.remaining_count,
                        "operation_id": outcome.operation_id,
                    }
                )
            except LocalSecretCleanupError as exc:
                # The operation ledger already holds the durable failure row.
                session.commit()
                result["refusals"].append(
                    {"login": login, "code": exc.code, "reason": exc.message}
                )
            except Exception as exc:  # noqa: BLE001 - one login must not abort the rest
                session.rollback()
                result["refusals"].append(
                    {"login": login, "code": "unexpected_error", "reason": str(exc)}
                )

        result["ok"] = not result["refusals"]
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
