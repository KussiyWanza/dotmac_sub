"""Serialize and verify immutable release-candidate evidence.

This module is the JSON adapter around the typed release contracts. GitHub
workflows use it to exchange exact source, tree, image-digest, and workflow-run
identities without treating mutable image tags or free-form JSON as authority.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from scripts.release_artifact_contract import (
    EvidenceConclusion,
    GitCommitSha,
    GitTreeSha,
    OCIImageDigest,
    ReleaseArtifactEvidence,
    ReleaseContractError,
    StagingAcceptanceEvidence,
    StagingDeploymentId,
    WorkflowRunId,
)

SCHEMA_VERSION = 1
_CANDIDATE_KIND = "dotmac.release_candidate"
_STAGING_KIND = "dotmac.staging_acceptance"


class EvidenceDocumentError(ValueError):
    """A release evidence document is missing or violates its exact schema."""


def _document_path(value: str) -> Path:
    path = Path(value)
    if not path.parent.exists():
        raise EvidenceDocumentError(
            f"evidence output directory does not exist: {path.parent}"
        )
    return path


def _read_document(path: Path, *, kind: str, fields: set[str]) -> dict[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceDocumentError(
            f"cannot read evidence document {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise EvidenceDocumentError("evidence document must be a JSON object")

    document = {str(key): value for key, value in raw.items()}
    expected = {"schema_version", "kind", *fields}
    if set(document) != expected:
        raise EvidenceDocumentError(
            f"evidence fields must be exactly {sorted(expected)}"
        )
    if (
        not isinstance(document["schema_version"], int)
        or isinstance(document["schema_version"], bool)
        or document["schema_version"] != SCHEMA_VERSION
    ):
        raise EvidenceDocumentError(
            f"unsupported evidence schema version: {document['schema_version']}"
        )
    if document["kind"] != kind:
        raise EvidenceDocumentError(f"unexpected evidence kind: {document['kind']}")
    return document


def _required_string(document: dict[str, object], field: str) -> str:
    value = document[field]
    if not isinstance(value, str):
        raise EvidenceDocumentError(f"{field} must be a string")
    return value


def _required_positive_int(document: dict[str, object], field: str) -> int:
    value = document[field]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise EvidenceDocumentError(f"{field} must be a positive integer")
    return value


def _write_document(path: Path, document: dict[str, str | int]) -> None:
    path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def write_candidate_evidence(
    path: Path,
    evidence: ReleaseArtifactEvidence,
) -> None:
    """Write one canonical candidate artifact document."""

    _write_document(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "kind": _CANDIDATE_KIND,
            "source_revision": evidence.source_revision.value,
            "source_tree": evidence.source_tree.value,
            "image_digest": evidence.image_digest.value,
            "build_run_id": evidence.build_run_id.value,
            "source_ci_conclusion": evidence.source_ci_conclusion.value,
        },
    )


def read_candidate_evidence(path: Path) -> ReleaseArtifactEvidence:
    """Read a candidate document into the typed release contract."""

    document = _read_document(
        path,
        kind=_CANDIDATE_KIND,
        fields={
            "source_revision",
            "source_tree",
            "image_digest",
            "build_run_id",
            "source_ci_conclusion",
        },
    )
    try:
        source_ci = EvidenceConclusion(
            _required_string(document, "source_ci_conclusion")
        )
    except ValueError as exc:
        raise EvidenceDocumentError(
            "source_ci_conclusion is not a recognized conclusion"
        ) from exc
    try:
        return ReleaseArtifactEvidence(
            source_revision=GitCommitSha(_required_string(document, "source_revision")),
            source_tree=GitTreeSha(_required_string(document, "source_tree")),
            image_digest=OCIImageDigest(_required_string(document, "image_digest")),
            build_run_id=WorkflowRunId(
                _required_positive_int(document, "build_run_id")
            ),
            source_ci_conclusion=source_ci,
        )
    except ReleaseContractError as exc:
        raise EvidenceDocumentError(f"invalid candidate evidence: {exc}") from exc


def write_staging_acceptance(
    path: Path,
    evidence: StagingAcceptanceEvidence,
) -> None:
    """Write one canonical staging-acceptance document."""

    _write_document(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "kind": _STAGING_KIND,
            "deployment_id": evidence.deployment_id.value,
            "source_revision": evidence.source_revision.value,
            "source_tree": evidence.source_tree.value,
            "image_digest": evidence.image_digest.value,
            "conclusion": evidence.conclusion.value,
        },
    )


def read_staging_acceptance(path: Path) -> StagingAcceptanceEvidence:
    """Read a staging document into the typed release contract."""

    document = _read_document(
        path,
        kind=_STAGING_KIND,
        fields={
            "deployment_id",
            "source_revision",
            "source_tree",
            "image_digest",
            "conclusion",
        },
    )
    try:
        conclusion = EvidenceConclusion(_required_string(document, "conclusion"))
    except ValueError as exc:
        raise EvidenceDocumentError("conclusion is not recognized") from exc
    try:
        return StagingAcceptanceEvidence(
            deployment_id=StagingDeploymentId(
                _required_positive_int(document, "deployment_id")
            ),
            source_revision=GitCommitSha(_required_string(document, "source_revision")),
            source_tree=GitTreeSha(_required_string(document, "source_tree")),
            image_digest=OCIImageDigest(_required_string(document, "image_digest")),
            conclusion=conclusion,
        )
    except ReleaseContractError as exc:
        raise EvidenceDocumentError(f"invalid staging evidence: {exc}") from exc


def verify_candidate_evidence(
    evidence: ReleaseArtifactEvidence,
    *,
    expected_source_revision: GitCommitSha,
    expected_source_tree: GitTreeSha,
    expected_build_run_id: WorkflowRunId,
) -> None:
    """Fail unless candidate evidence exactly matches its triggering run."""

    if evidence.source_revision != expected_source_revision:
        raise EvidenceDocumentError("candidate source revision does not match")
    if evidence.source_tree != expected_source_tree:
        raise EvidenceDocumentError("candidate source tree does not match")
    if evidence.build_run_id != expected_build_run_id:
        raise EvidenceDocumentError("candidate build workflow run does not match")
    if evidence.source_ci_conclusion is not EvidenceConclusion.SUCCESS:
        raise EvidenceDocumentError("candidate source CI is not successful")


def _append_github_outputs(path: Path, values: dict[str, str | int]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    candidate = commands.add_parser("write-candidate")
    candidate.add_argument("--source-revision", required=True)
    candidate.add_argument("--source-tree", required=True)
    candidate.add_argument("--image-digest", required=True)
    candidate.add_argument("--build-run-id", required=True, type=int)
    candidate.add_argument("--output", required=True, type=_document_path)

    verify = commands.add_parser("verify-candidate")
    verify.add_argument("--path", required=True, type=Path)
    verify.add_argument("--expected-source-revision", required=True)
    verify.add_argument("--expected-source-tree", required=True)
    verify.add_argument("--expected-build-run-id", required=True, type=int)
    verify.add_argument("--github-output", required=True, type=Path)

    staging = commands.add_parser("write-staging-acceptance")
    staging.add_argument("--deployment-id", required=True, type=int)
    staging.add_argument("--source-revision", required=True)
    staging.add_argument("--source-tree", required=True)
    staging.add_argument("--image-digest", required=True)
    staging.add_argument("--output", required=True, type=_document_path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "write-candidate":
        write_candidate_evidence(
            args.output,
            ReleaseArtifactEvidence(
                source_revision=GitCommitSha(args.source_revision),
                source_tree=GitTreeSha(args.source_tree),
                image_digest=OCIImageDigest(args.image_digest),
                build_run_id=WorkflowRunId(args.build_run_id),
                source_ci_conclusion=EvidenceConclusion.SUCCESS,
            ),
        )
        return 0
    if args.command == "verify-candidate":
        evidence = read_candidate_evidence(args.path)
        verify_candidate_evidence(
            evidence,
            expected_source_revision=GitCommitSha(args.expected_source_revision),
            expected_source_tree=GitTreeSha(args.expected_source_tree),
            expected_build_run_id=WorkflowRunId(args.expected_build_run_id),
        )
        _append_github_outputs(
            args.github_output,
            {
                "source_revision": evidence.source_revision.value,
                "source_tree": evidence.source_tree.value,
                "image_digest": evidence.image_digest.value,
                "build_run_id": evidence.build_run_id.value,
            },
        )
        return 0

    write_staging_acceptance(
        args.output,
        StagingAcceptanceEvidence(
            deployment_id=StagingDeploymentId(args.deployment_id),
            source_revision=GitCommitSha(args.source_revision),
            source_tree=GitTreeSha(args.source_tree),
            image_digest=OCIImageDigest(args.image_digest),
            conclusion=EvidenceConclusion.SUCCESS,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
