#!/usr/bin/env python3
"""Generate a private, read-only subscriber identity cleanup worklist."""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections.abc import Iterable
from pathlib import Path

from app.db import read_only_snapshot_session
from app.services import party_identity_audit


def _private_text_file(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="")


def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: Iterable[dict]) -> None:
    with _private_text_file(path) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_audit_artifacts(
    audit: party_identity_audit.SubscriberIdentityAudit,
    output_dir: Path,
) -> tuple[Path, ...]:
    """Write UUID-only worklists; raw email, phone, NIN, and names stay out."""

    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    summary_path = output_dir / "summary.json"
    subscriber_path = output_dir / "subscribers.csv"
    groups_path = output_dir / "duplicate_groups.csv"
    members_path = output_dir / "duplicate_members.csv"

    with _private_text_file(summary_path) as handle:
        json.dump(audit.summary(), handle, indent=2, sort_keys=True)
        handle.write("\n")

    _write_csv(
        subscriber_path,
        (
            "subscriber_id",
            "lifecycle_cohort",
            "record_classification",
            "recommended_disposition",
            "lifecycle_evidence",
            "classification_evidence",
            "contradictions",
            "duplicate_group_count",
            "strongest_duplicate_confidence",
            "existing_party_id",
            "available_display_name_sources",
            "row_fingerprint",
        ),
        (
            {
                "subscriber_id": str(row.subscriber_id),
                "lifecycle_cohort": row.lifecycle_cohort.value,
                "record_classification": row.record_classification.value,
                "recommended_disposition": row.recommended_disposition.value,
                "lifecycle_evidence": "|".join(row.lifecycle_evidence),
                "classification_evidence": "|".join(row.classification_evidence),
                "contradictions": "|".join(row.contradictions),
                "duplicate_group_count": len(row.duplicate_group_ids),
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
                "row_fingerprint": (
                    party_identity_audit.subscriber_audit_row_fingerprint(row)
                ),
            }
            for row in audit.rows
        ),
    )
    _write_csv(
        groups_path,
        (
            "group_id",
            "evidence_type",
            "confidence",
            "member_count",
            "automatic_merge_allowed",
        ),
        (
            {
                "group_id": group.group_id,
                "evidence_type": group.evidence_type,
                "confidence": group.confidence.value,
                "member_count": group.member_count,
                "automatic_merge_allowed": str(group.automatic_merge_allowed).lower(),
            }
            for group in audit.duplicate_groups
        ),
    )
    _write_csv(
        members_path,
        ("group_id", "subscriber_id"),
        (
            {"group_id": group.group_id, "subscriber_id": str(subscriber_id)}
            for group in audit.duplicate_groups
            for subscriber_id in group.subscriber_ids
        ),
    )
    return summary_path, subscriber_path, groups_path, members_path


def run(output_dir: Path) -> party_identity_audit.SubscriberIdentityAudit:
    with read_only_snapshot_session() as db:
        audit = party_identity_audit.build_subscriber_identity_audit(db)
        db.rollback()
    write_audit_artifacts(audit, output_dir)
    return audit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Private output directory for summary and UUID-only CSV worklists",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    audit = run(args.out)
    print(json.dumps(audit.summary(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
