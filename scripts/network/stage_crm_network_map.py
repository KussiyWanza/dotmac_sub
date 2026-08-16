"""Dry-run or stage a restored CRM Network Map archive in bounded batches.

This command never restores into the Selfcare database and never writes
canonical network/GIS assets.  The source URL must point at an isolated
test/restore database and is read from the environment so credentials do not
appear in process arguments or reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.network.crm_network_map_source import (  # noqa: E402
    MAX_BATCH_SIZE,
    CrmMapProfileExtraction,
    CrmNetworkMapExtraction,
    build_kml,
    extract_crm_network_map,
)
from app.services.network.fiber_topology_staging import (  # noqa: E402
    FiberSourcePreview,
    preview_fiber_source,
    stage_fiber_preview_batch,
)

SOURCE_DATABASE_URL_ENV = "CRM_NETWORK_MAP_SOURCE_DATABASE_URL"


def _snapshot_captured_at(value: str) -> datetime:
    try:
        captured_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "snapshot capture time must be a valid ISO-8601 timestamp"
        ) from exc
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            "snapshot capture time must include a UTC offset"
        )
    return captured_at.astimezone(UTC)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a restored CRM map archive and preview or persist only "
            "immutable Selfcare staging evidence."
        )
    )
    parser.add_argument(
        "--archive",
        type=Path,
        required=True,
        help="Read-only CRM PostgreSQL dump whose SHA-256 binds the run.",
    )
    parser.add_argument(
        "--snapshot-captured-at",
        type=_snapshot_captured_at,
        required=True,
        help=(
            "Actual ISO-8601 CRM dump capture time from the immutable snapshot "
            "receipt; this is not the restore or staging time."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help=f"Maximum staged evidence rows per transaction (1-{MAX_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--stage",
        action="store_true",
        help="Persist staging evidence; canonical assets are never written.",
    )
    parser.add_argument(
        "--confirm-archive-sha256",
        help="Required with --stage and must equal the archive's actual SHA-256.",
    )
    parser.add_argument(
        "--actor",
        help="Required audit actor with --stage.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Create a mode-0600 JSON receipt; an existing file is never replaced.",
    )
    return parser.parse_args()


def _archive_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as report:
        json.dump(payload, report, indent=2, sort_keys=True)
        report.write("\n")


def _preview_profiles(
    extraction: CrmNetworkMapExtraction, temporary_directory: Path
) -> tuple[tuple[CrmMapProfileExtraction, Path, FiberSourcePreview], ...]:
    results: list[tuple[CrmMapProfileExtraction, Path, FiberSourcePreview]] = []
    with SessionLocal() as db:
        for profile in extraction.profiles:
            path = temporary_directory / f"{profile.profile.value}.kml"
            path.write_bytes(build_kml(profile.features))
            preview = preview_fiber_source(db, path, profile.profile.value)
            results.append((profile, path, preview))
    return tuple(results)


def _preview_payload(
    profile: CrmMapProfileExtraction, preview: FiberSourcePreview
) -> dict[str, object]:
    return {
        **profile.to_dict(),
        "source_system": preview.source_system,
        "file_sha256": preview.file_sha256,
        "manifest_sha256": preview.manifest_sha256,
        "match_status_counts": dict(sorted(preview.status_counts.items())),
        "ambiguous_count": preview.status_counts.get("ambiguous", 0),
        "staging_blocker_count": preview.blocker_count,
    }


def _stage_profiles(
    previews: tuple[tuple[CrmMapProfileExtraction, Path, FiberSourcePreview], ...],
    *,
    archive_sha256: str,
    actor: str,
    batch_size: int,
    snapshot_timestamp: str,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for profile, _path, preview in previews:
        for start in range(0, preview.feature_count, batch_size):
            stop = min(start + batch_size, preview.feature_count)
            batch_number = (start // batch_size) + 1
            source_name = (
                f"{profile.profile.value}-{batch_number:05d}-{archive_sha256[:12]}.kml"
            )
            with SessionLocal() as db:
                result = stage_fiber_preview_batch(
                    db,
                    preview,
                    start=start,
                    stop=stop,
                    source_name=source_name,
                    created_by=actor,
                    source_metadata={
                        "source_archive_sha256": archive_sha256,
                        "source_database": "dotmac_omni isolated restore",
                        "extraction_format_version": 1,
                        "snapshot_timestamp": snapshot_timestamp,
                        "importer_version": "stage_crm_network_map:v2",
                        "source_count": profile.source_count,
                        "restored_count": profile.source_count,
                        "active_source_count": (
                            profile.source_count - profile.inactive_count
                        ),
                        "valid_active_source_count": profile.feature_count,
                        "staged_count": preview.feature_count,
                        "reconciliation_status": ("source_restore_staged_counts_match"),
                    },
                )
            results.append(
                {
                    "profile": profile.profile.value,
                    "batch_number": batch_number,
                    "start": start + 1,
                    "stop": stop,
                    **result.to_dict(),
                }
            )
    return results


def main() -> int:
    args = parse_args()
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        raise SystemExit(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
    if not args.archive.is_file():
        raise SystemExit("--archive must be a readable file")
    source_database_url = os.environ.get(SOURCE_DATABASE_URL_ENV, "").strip()
    if not source_database_url:
        raise SystemExit(f"{SOURCE_DATABASE_URL_ENV} is required")
    archive_sha256 = _archive_sha256(args.archive)
    if args.stage:
        if (args.confirm_archive_sha256 or "").strip().lower() != archive_sha256:
            raise SystemExit(
                "--confirm-archive-sha256 must equal the actual archive digest"
            )
        if not (args.actor or "").strip():
            raise SystemExit("--actor is required with --stage")

    source_engine = create_engine(source_database_url, pool_pre_ping=True)
    try:
        extraction = extract_crm_network_map(
            source_engine,
            archive_sha256=archive_sha256,
        )
    finally:
        source_engine.dispose()
    snapshot_timestamp = args.snapshot_captured_at.isoformat()

    with tempfile.TemporaryDirectory(prefix="crm-network-map-") as temp_dir:
        previews = _preview_profiles(extraction, Path(temp_dir))
        profile_reports = [
            _preview_payload(profile, preview) for profile, _path, preview in previews
        ]
        hard_conflicts = extraction.blocker_count + sum(
            preview.status_counts.get("ambiguous", 0) + preview.blocker_count
            for _profile, _path, preview in previews
        )
        report: dict[str, object] = {
            "mode": "stage" if args.stage else "dry_run",
            "archive_sha256": archive_sha256,
            "batch_size": args.batch_size,
            "snapshot_timestamp": snapshot_timestamp,
            "hard_conflict_count": hard_conflicts,
            "canonical_asset_writes": 0,
            "extraction": extraction.to_dict(),
            "profiles": profile_reports,
            "olt_status": (
                "comparison_only_not_imported; OLT identity remains owned by "
                "Selfcare network inventory"
            ),
        }
        if args.stage:
            if hard_conflicts:
                report["stage_status"] = "stopped_before_first_write"
            else:
                report["stage_status"] = "completed"
                report["batches"] = _stage_profiles(
                    previews,
                    archive_sha256=archive_sha256,
                    actor=args.actor.strip(),
                    batch_size=args.batch_size,
                    snapshot_timestamp=snapshot_timestamp,
                )
        if args.report_path:
            _write_report(args.report_path, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2 if hard_conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
