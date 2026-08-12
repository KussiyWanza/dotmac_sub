"""Report or apply exact-ID ONT configuration lifecycle drift repairs.

Dry-run is the default. Execution delegates every mutation to
``network.ont_service_configuration`` and never issues bulk SQL updates.
"""

from __future__ import annotations

import argparse
import json
import uuid

from app.db import SessionLocal
from app.services.network.ont_service_configuration import (
    RepairOntServiceConfigurationDriftCommand,
    inspect_ont_service_configuration_drift,
    repair_ont_service_configuration_drift,
)
from app.services.owner_commands import CommandContext


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ont-id",
        action="append",
        required=True,
        help="Exact ONT UUID; repeat for a reviewed bounded cohort.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply owner-approved repairs. Without this flag the command is read-only.",
    )
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    parser.add_argument("--reviewed-evidence")
    parser.add_argument("--idempotency-key")
    return parser


def _required_execution_value(value: str | None, flag: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise SystemExit(f"--execute requires {flag}")
    return normalized


def main() -> int:
    args = _parser().parse_args()
    try:
        ont_ids = tuple(uuid.UUID(value) for value in args.ont_id)
    except ValueError as exc:
        raise SystemExit(f"invalid --ont-id: {exc}") from exc

    with SessionLocal() as db:
        if not args.execute:
            findings = inspect_ont_service_configuration_drift(db, ont_unit_ids=ont_ids)
            print(
                json.dumps(
                    {
                        "mode": "dry_run",
                        "findings": [
                            {
                                "ont_unit_id": str(item.ont_unit_id),
                                "reasons": list(item.reasons),
                            }
                            for item in findings
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        actor = _required_execution_value(args.actor, "--actor")
        reason = _required_execution_value(args.reason, "--reason")
        evidence = _required_execution_value(
            args.reviewed_evidence, "--reviewed-evidence"
        )
        idempotency_key = _required_execution_value(
            args.idempotency_key, "--idempotency-key"
        )
        command_id = uuid.uuid4()
        outcome = repair_ont_service_configuration_drift(
            db,
            RepairOntServiceConfigurationDriftCommand(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=actor,
                    scope="network:ont:configuration-repair",
                    reason=reason,
                    idempotency_key=idempotency_key,
                ),
                ont_unit_ids=ont_ids,
                reviewed_evidence=evidence,
            ),
        )
        print(
            json.dumps(
                {
                    "mode": "execute",
                    "examined": outcome.examined,
                    "repaired": outcome.repaired,
                    "findings": [
                        {
                            "ont_unit_id": str(item.ont_unit_id),
                            "reasons": list(item.reasons),
                        }
                        for item in outcome.findings
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
