#!/usr/bin/env python3
"""Generate a private reviewed-decision template or read-only Party plan.

This tool has no apply mode. It never creates Parties, binds Subscribers,
assigns roles, quarantines records, merges identities, or calls CRM.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import stat
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from app.db import read_only_snapshot_session
from app.services import party_identity_audit
from app.services.party_identity_adjudication import (
    PartyAdjudicationError,
    PartyBackfillPlan,
    PartyIdentityDecision,
    build_party_backfill_plan,
)

_DECISION_FIELDS = (
    "decision_id",
    "subscriber_id",
    "audit_digest",
    "audit_generated_at",
    "row_fingerprint",
    "lifecycle_cohort",
    "record_classification",
    "recommended_disposition",
    "contradictions",
    "duplicate_group_ids",
    "strongest_duplicate_confidence",
    "existing_party_id",
    "available_display_name_sources",
    "action",
    "planned_party_id",
    "identity_source_subscriber_id",
    "party_type",
    "data_classification",
    "display_name_source",
    "reviewer",
    "reviewed_at",
    "reason",
)


class DecisionFileError(ValueError):
    pass


def _prepare_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _private_text_file(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="")


def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: Iterable[dict]) -> None:
    with _private_text_file(path) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict) -> None:
    with _private_text_file(path) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _decision_id(audit_digest: str, subscriber_id: UUID) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"dotmac-sub:party-adjudication:v1:{audit_digest}:{subscriber_id}",
    )


def write_decision_template(
    audit: party_identity_audit.SubscriberIdentityAudit,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write a UUID-only template; every adjudication field starts blank."""

    _prepare_private_dir(output_dir)
    digest = party_identity_audit.subscriber_identity_audit_digest(audit)
    generated_at = audit.generated_at.isoformat() if audit.generated_at else ""
    summary_path = output_dir / "decision_template_summary.json"
    template_path = output_dir / "party_identity_decisions.csv"
    _write_json(
        summary_path,
        {
            "audit_digest": digest,
            "audit_generated_at": generated_at or None,
            "total_audit_rows": len(audit.rows),
            "artifact_contract": {
                "read_only": True,
                "contains_raw_contact_values": False,
                "contains_display_names": False,
                "automatic_merge_allowed": False,
                "decision_fields_blank": True,
            },
        },
    )
    _write_csv(
        template_path,
        _DECISION_FIELDS,
        (
            {
                "decision_id": str(_decision_id(digest, row.subscriber_id)),
                "subscriber_id": str(row.subscriber_id),
                "audit_digest": digest,
                "audit_generated_at": generated_at,
                "row_fingerprint": (
                    party_identity_audit.subscriber_audit_row_fingerprint(row)
                ),
                "lifecycle_cohort": row.lifecycle_cohort.value,
                "record_classification": row.record_classification.value,
                "recommended_disposition": row.recommended_disposition.value,
                "contradictions": "|".join(row.contradictions),
                "duplicate_group_ids": "|".join(row.duplicate_group_ids),
                "strongest_duplicate_confidence": (
                    row.strongest_duplicate_confidence.value
                    if row.strongest_duplicate_confidence
                    else ""
                ),
                "existing_party_id": (
                    str(row.existing_party_id) if row.existing_party_id else ""
                ),
                "available_display_name_sources": "|".join(
                    row.available_display_name_sources
                ),
                "action": "",
                "planned_party_id": "",
                "identity_source_subscriber_id": "",
                "party_type": "",
                "data_classification": "",
                "display_name_source": "",
                "reviewer": "",
                "reviewed_at": "",
                "reason": "",
            }
            for row in audit.rows
        ),
    )
    return summary_path, template_path


def _optional_uuid(value: str | None) -> UUID | None:
    cleaned = (value or "").strip()
    return UUID(cleaned) if cleaned else None


def _required_uuid(value: str | None, field_name: str) -> UUID:
    parsed = _optional_uuid(value)
    if parsed is None:
        raise ValueError(f"{field_name} is required")
    return parsed


def _required_datetime(value: str | None, field_name: str) -> datetime:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))


def _assert_private_decision_file(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise DecisionFileError(
            f"Decision file '{path}' must not be readable or writable by group/other; "
            f"current mode is {oct(mode)}, expected 0o600"
        )


def load_decisions(path: Path) -> tuple[PartyIdentityDecision, ...]:
    """Load only rows with an explicit action; blank template rows are unreviewed."""

    _assert_private_decision_file(path)
    decisions: list[PartyIdentityDecision] = []
    errors: list[str] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing_fields = sorted(set(_DECISION_FIELDS) - set(reader.fieldnames or ()))
        if missing_fields:
            raise DecisionFileError(
                f"Decision file is missing columns: {', '.join(missing_fields)}"
            )
        for line_number, row in enumerate(reader, start=2):
            if not (row.get("action") or "").strip():
                continue
            try:
                decisions.append(
                    PartyIdentityDecision(
                        decision_id=_required_uuid(
                            row.get("decision_id"), "decision_id"
                        ),
                        subscriber_id=_required_uuid(
                            row.get("subscriber_id"), "subscriber_id"
                        ),
                        audit_digest=(row.get("audit_digest") or "").strip(),
                        row_fingerprint=(row.get("row_fingerprint") or "").strip(),
                        action=(row.get("action") or "").strip(),
                        planned_party_id=_optional_uuid(row.get("planned_party_id")),
                        identity_source_subscriber_id=_optional_uuid(
                            row.get("identity_source_subscriber_id")
                        ),
                        party_type=(row.get("party_type") or "").strip() or None,
                        data_classification=(
                            (row.get("data_classification") or "").strip() or None
                        ),
                        display_name_source=(
                            (row.get("display_name_source") or "").strip() or None
                        ),
                        reviewer=(row.get("reviewer") or "").strip(),
                        reviewed_at=_required_datetime(
                            row.get("reviewed_at"), "reviewed_at"
                        ),
                        reason=(row.get("reason") or "").strip(),
                    )
                )
            except (ValueError, TypeError) as exc:
                errors.append(f"line {line_number}: {exc}")
    if errors:
        raise DecisionFileError("; ".join(errors))
    return tuple(decisions)


def write_plan_artifacts(
    plan: PartyBackfillPlan,
    output_dir: Path,
    *,
    decision_file_sha256: str | None = None,
) -> tuple[Path, ...]:
    """Write a PII-free dry-run manifest; decision reason text stays out."""

    _prepare_private_dir(output_dir)
    summary_path = output_dir / "party_backfill_plan.json"
    groups_path = output_dir / "planned_parties.csv"
    bindings_path = output_dir / "planned_bindings.csv"
    deferred_path = output_dir / "deferred_decisions.csv"
    summary = plan.summary()
    if decision_file_sha256:
        summary["decision_file_sha256"] = decision_file_sha256
    _write_json(summary_path, summary)
    _write_csv(
        groups_path,
        (
            "planned_party_id",
            "party_type",
            "data_classification",
            "target_status",
            "identity_source_subscriber_id",
            "display_name_source",
            "subscriber_count",
            "decision_count",
        ),
        (
            {
                "planned_party_id": str(group.planned_party_id),
                "party_type": group.party_type.value,
                "data_classification": group.data_classification.value,
                "target_status": group.target_status.value,
                "identity_source_subscriber_id": str(
                    group.identity_source_subscriber_id
                ),
                "display_name_source": group.display_name_source.value,
                "subscriber_count": len(group.subscriber_ids),
                "decision_count": len(group.decision_ids),
            }
            for group in plan.groups
        ),
    )
    _write_csv(
        bindings_path,
        (
            "decision_id",
            "subscriber_id",
            "planned_party_id",
            "action",
            "row_fingerprint",
            "reviewed_at",
            "reviewer_sha256",
            "reason_sha256",
        ),
        (binding.digest_value() for binding in plan.bindings),
    )
    _write_csv(
        deferred_path,
        (
            "decision_id",
            "subscriber_id",
            "row_fingerprint",
            "reviewed_at",
            "reviewer_sha256",
            "reason_sha256",
        ),
        (item.digest_value() for item in plan.deferred),
    )
    return summary_path, groups_path, bindings_path, deferred_path


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_current_audit() -> party_identity_audit.SubscriberIdentityAudit:
    with read_only_snapshot_session() as db:
        audit = party_identity_audit.build_subscriber_identity_audit(db)
        db.rollback()
    return audit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--template",
        action="store_true",
        help="Write a private blank decision template from the current audit",
    )
    mode.add_argument(
        "--decisions",
        type=Path,
        help="Private reviewed decision CSV to validate into a dry-run plan",
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    audit = _build_current_audit()
    if args.template:
        template_paths = write_decision_template(audit, args.out)
        print(
            json.dumps(
                {
                    "status": "template_written",
                    "audit_digest": (
                        party_identity_audit.subscriber_identity_audit_digest(audit)
                    ),
                    "total_audit_rows": len(audit.rows),
                    "files": [str(path) for path in template_paths],
                    "database_writes": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    try:
        decisions = load_decisions(args.decisions)
        plan = build_party_backfill_plan(audit, decisions)
    except (DecisionFileError, PartyAdjudicationError) as exc:
        print(f"REFUSED: {exc}")
        return 2
    decision_file_sha256 = _file_sha256(args.decisions)
    plan_paths = write_plan_artifacts(
        plan,
        args.out,
        decision_file_sha256=decision_file_sha256,
    )
    print(
        json.dumps(
            {
                **plan.summary(),
                "decision_file_sha256": decision_file_sha256,
                "files": [str(path) for path in plan_paths],
                "database_writes": 0,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
