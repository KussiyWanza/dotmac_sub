"""Collect and verify typed deployment backup-policy evidence.

The production adapter extracts migration trees from immutable images and asks
this module to describe them.  The resulting decision is re-evaluated by the
deployment owner before a production backup may be omitted.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from scripts.release_artifact_contract import (
    AlembicHeads,
    BackupPolicyDecision,
    DeploymentTarget,
    HotfixNoMigrationEvidence,
    MigrationGraphDigest,
    MigrationStateEvidence,
    resolve_backup_policy,
)

SCHEMA_VERSION = 1
_IMAGE_KIND = "dotmac.migration_image_state"
_DECISION_KIND = "dotmac.production_backup_decision"


class BackupEvidenceError(ValueError):
    """Migration or decision evidence is malformed or inconsistent."""


def _migration_files(versions_dir: Path) -> tuple[Path, ...]:
    files = tuple(sorted(versions_dir.glob("*.py")))
    if not files:
        raise BackupEvidenceError(f"no migration files found in {versions_dir}")
    return files


def _literal_assignment(module: ast.Module, name: str, path: Path) -> object:
    for node in module.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == name for target in targets
            ):
                value = node.value
                if value is None:
                    break
                try:
                    return ast.literal_eval(value)
                except (ValueError, TypeError) as exc:
                    raise BackupEvidenceError(f"{path} has non-literal {name}") from exc
    raise BackupEvidenceError(f"{path} has no {name} declaration")


def describe_migration_tree(
    versions_dir: Path,
) -> tuple[MigrationGraphDigest, AlembicHeads]:
    """Fingerprint all migration bytes and derive the exact graph heads."""

    digest = hashlib.sha256()
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in _migration_files(versions_dir):
        relative = path.relative_to(versions_dir).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        module = ast.parse(content, filename=str(path))
        revision = _literal_assignment(module, "revision", path)
        if not isinstance(revision, str) or not revision:
            raise BackupEvidenceError(f"{path} has invalid revision")
        if revision in revisions:
            raise BackupEvidenceError(f"duplicate migration revision: {revision}")
        revisions.add(revision)
        down_revision = _literal_assignment(module, "down_revision", path)
        if down_revision is None:
            continue
        values = (down_revision,) if isinstance(down_revision, str) else down_revision
        if not isinstance(values, (tuple, list)) or not all(
            isinstance(value, str) and value for value in values
        ):
            raise BackupEvidenceError(f"{path} has invalid down_revision")
        parents.update(values)
    heads = revisions - parents
    if not heads:
        raise BackupEvidenceError("migration graph has no heads")
    return MigrationGraphDigest(f"sha256:{digest.hexdigest()}"), AlembicHeads(
        tuple(heads)
    )


def _write(path: Path, document: dict[str, object]) -> None:
    path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _read(path: Path, *, kind: str) -> dict[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupEvidenceError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BackupEvidenceError("backup evidence must be a JSON object")
    document = {str(key): value for key, value in raw.items()}
    if document.get("schema_version") != SCHEMA_VERSION or document.get("kind") != kind:
        raise BackupEvidenceError("unsupported backup evidence document")
    return document


def _image_state(path: Path) -> tuple[MigrationGraphDigest, AlembicHeads]:
    document = _read(path, kind=_IMAGE_KIND)
    digest = document.get("graph_digest")
    heads = document.get("heads")
    if (
        not isinstance(digest, str)
        or not isinstance(heads, list)
        or not all(isinstance(head, str) for head in heads)
    ):
        raise BackupEvidenceError("invalid migration image state")
    return MigrationGraphDigest(digest), AlembicHeads(tuple(heads))


def _required_string_list(
    document: dict[str, object],
    field: str,
) -> tuple[str, ...]:
    value = document[field]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BackupEvidenceError(f"{field} must be a list of strings")
    return tuple(value)


def _decision_from_document(path: Path) -> BackupPolicyDecision:
    document = _read(path, kind=_DECISION_KIND)
    required = {
        "schema_version",
        "kind",
        "change_reference",
        "reason",
        "running_graph_digest",
        "candidate_graph_digest",
        "running_image_heads",
        "candidate_image_heads",
        "database_heads",
        "mode",
        "issues",
    }
    if set(document) != required:
        raise BackupEvidenceError("production backup decision has unexpected fields")
    string_fields = (
        "change_reference",
        "reason",
        "running_graph_digest",
        "candidate_graph_digest",
        "mode",
    )
    if any(not isinstance(document[field], str) for field in string_fields):
        raise BackupEvidenceError("production backup decision has invalid strings")
    list_fields = (
        "running_image_heads",
        "candidate_image_heads",
        "database_heads",
        "issues",
    )
    parsed_lists = {
        field: _required_string_list(document, field) for field in list_fields
    }
    running_heads = parsed_lists["running_image_heads"]
    candidate_heads = parsed_lists["candidate_image_heads"]
    database_heads = parsed_lists["database_heads"]
    hotfix = HotfixNoMigrationEvidence(
        change_reference=str(document["change_reference"]),
        reason=str(document["reason"]),
        migration_state=MigrationStateEvidence(
            running_graph_digest=MigrationGraphDigest(
                str(document["running_graph_digest"])
            ),
            candidate_graph_digest=MigrationGraphDigest(
                str(document["candidate_graph_digest"])
            ),
            running_image_heads=AlembicHeads(running_heads),
            candidate_image_heads=AlembicHeads(candidate_heads),
            database_heads=AlembicHeads(database_heads),
        ),
    )
    decision = resolve_backup_policy(target=DeploymentTarget.PRODUCTION, hotfix=hotfix)
    if document["mode"] != decision.mode.value or parsed_lists["issues"] != tuple(
        issue.value for issue in decision.issues
    ):
        raise BackupEvidenceError(
            "production backup decision does not match its evidence"
        )
    return decision


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    describe = commands.add_parser("describe-tree")
    describe.add_argument("--versions-dir", required=True, type=Path)
    describe.add_argument("--output", required=True, type=Path)
    decide = commands.add_parser("write-production-decision")
    decide.add_argument("--running-image", required=True, type=Path)
    decide.add_argument("--candidate-image", required=True, type=Path)
    decide.add_argument("--database-head", action="append", required=True)
    decide.add_argument("--change-reference", required=True)
    decide.add_argument("--reason", required=True)
    decide.add_argument("--output", required=True, type=Path)
    verify = commands.add_parser("verify-production-decision")
    verify.add_argument("--path", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "describe-tree":
        graph_digest, heads = describe_migration_tree(args.versions_dir)
        _write(
            args.output,
            {
                "schema_version": SCHEMA_VERSION,
                "kind": _IMAGE_KIND,
                "graph_digest": graph_digest.value,
                "heads": list(heads.revisions),
            },
        )
        return 0
    if args.command == "write-production-decision":
        running_digest, running_heads = _image_state(args.running_image)
        candidate_digest, candidate_heads = _image_state(args.candidate_image)
        hotfix = HotfixNoMigrationEvidence(
            change_reference=args.change_reference,
            reason=args.reason,
            migration_state=MigrationStateEvidence(
                running_graph_digest=running_digest,
                candidate_graph_digest=candidate_digest,
                running_image_heads=running_heads,
                candidate_image_heads=candidate_heads,
                database_heads=AlembicHeads(tuple(args.database_head)),
            ),
        )
        decision = resolve_backup_policy(
            target=DeploymentTarget.PRODUCTION,
            hotfix=hotfix,
        )
        state = hotfix.migration_state
        _write(
            args.output,
            {
                "schema_version": SCHEMA_VERSION,
                "kind": _DECISION_KIND,
                "change_reference": hotfix.change_reference,
                "reason": hotfix.reason,
                "running_graph_digest": state.running_graph_digest.value,
                "candidate_graph_digest": state.candidate_graph_digest.value,
                "running_image_heads": list(state.running_image_heads.revisions),
                "candidate_image_heads": list(state.candidate_image_heads.revisions),
                "database_heads": list(state.database_heads.revisions),
                "mode": decision.mode.value,
                "issues": [issue.value for issue in decision.issues],
            },
        )
        print(decision.mode.value)
        return 0
    decision = _decision_from_document(args.path)
    print(decision.mode.value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
