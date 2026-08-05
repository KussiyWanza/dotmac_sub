"""Preview, apply, and inspect reviewed Team Inbox lifecycle audit evidence."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from uuid import UUID

from app.db import SessionLocal
from app.services import team_inbox_audit, team_inbox_audit_reconstruction

FINAL_CONFIRMATION = "APPLY_REVIEWED_TEAM_INBOX_AUDIT"


def _manifest_report(
    manifest: team_inbox_audit_reconstruction.ReconstructionManifest,
) -> dict[str, object]:
    counts = Counter(item.evidence_grade.value for item in manifest.items)
    return {
        "generated_at": manifest.generated_at.isoformat(),
        "source_watermark": manifest.source_watermark,
        "sha256": manifest.sha256,
        "counts_by_evidence_grade": dict(sorted(counts.items())),
        "items": [
            {
                "source_id": item.source_id,
                "kind": item.kind.value,
                "subject_id": str(item.subject_id),
                "previous_value": item.previous_value,
                "value": item.value,
                "actor_person_id": (
                    str(item.actor_person_id) if item.actor_person_id else None
                ),
                "occurred_at": (
                    item.occurred_at.isoformat() if item.occurred_at else None
                ),
                "evidence_grade": item.evidence_grade.value,
            }
            for item in manifest.items
        ],
    }


def _timeline_report(
    timeline: team_inbox_audit.InboxConversationAuditTimeline,
) -> dict[str, object]:
    return {
        "conversation_id": str(timeline.conversation_id),
        "native_coverage_started_at": (
            timeline.native_coverage_started_at.isoformat()
            if timeline.native_coverage_started_at
            else None
        ),
        "has_pre_cutover_unknowns": timeline.has_pre_cutover_unknowns,
        "entries": [
            {
                "event_id": str(item.event_id),
                "kind": item.kind.value,
                "action": item.action,
                "previous_value": item.previous_value,
                "value": item.value,
                "actor_person_id": (
                    str(item.actor_person_id) if item.actor_person_id else None
                ),
                "reason_code": item.reason_code,
                "occurred_at": item.occurred_at.isoformat(),
                "recorded_at": item.recorded_at.isoformat(),
                "evidence_grade": item.evidence_grade.value,
                "source_id": item.source_id,
            }
            for item in timeline.entries
        ],
        "findings": [
            {
                "kind": item.kind.value,
                "subject_id": str(item.subject_id),
                "expected_value": item.expected_value,
                "actual_value": item.actual_value,
            }
            for item in timeline.findings
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preview", help="Print the complete read-only manifest.")

    timeline = commands.add_parser(
        "timeline", help="Print one identifier-only timeline and drift report."
    )
    timeline.add_argument("--conversation-id", type=UUID, required=True)

    apply = commands.add_parser("apply", help="Apply one reviewed manifest.")
    apply.add_argument("--reviewed-sha256", required=True)
    apply.add_argument("--source-watermark", required=True)
    apply.add_argument("--actor-person-id", type=UUID, required=True)
    apply.add_argument("--approval-reference", required=True)
    apply.add_argument("--idempotency-key", required=True)
    apply.add_argument("--confirm", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "preview":
        with SessionLocal() as db:
            manifest = team_inbox_audit_reconstruction.preview_reconstruction(db)
        print(json.dumps(_manifest_report(manifest), indent=2, sort_keys=True))
        return 0
    if args.command == "timeline":
        with SessionLocal() as db:
            timeline = team_inbox_audit.conversation_audit_timeline(
                db, conversation_id=args.conversation_id
            )
        print(json.dumps(_timeline_report(timeline), indent=2, sort_keys=True))
        return 0
    if args.confirm != FINAL_CONFIRMATION:
        parser.error(f"--confirm must equal {FINAL_CONFIRMATION}")
    with SessionLocal() as db:
        outcome = team_inbox_audit_reconstruction.apply_reconstruction(
            db,
            team_inbox_audit_reconstruction.ApplyReconstructionCommand(
                expected_manifest_sha256=args.reviewed_sha256,
                expected_source_watermark=args.source_watermark,
                actor_person_id=args.actor_person_id,
                approval_reference=args.approval_reference,
                idempotency_key=args.idempotency_key,
            ),
        )
    print(
        json.dumps(
            {
                "manifest_sha256": outcome.manifest_sha256,
                "applied": outcome.applied,
                "exceptions": outcome.exceptions,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
