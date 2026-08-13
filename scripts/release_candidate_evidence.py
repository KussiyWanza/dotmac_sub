"""Serialize and verify immutable release-candidate evidence.

This module is the JSON adapter around the typed release contracts. GitHub
workflows use it to exchange exact source, tree, image-digest,
product-manifest-digest, and workflow-run identities without treating mutable
image tags or free-form JSON as authority.
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
    MainAuthorizationEvidence,
    OCIImageDigest,
    ProductManifestDigest,
    ReleaseArtifactEvidence,
    ReleaseCandidateRecord,
    ReleaseContractError,
    StagingAcceptanceEvidence,
    StagingDeploymentId,
    WorkflowRunId,
    evaluate_production_eligibility,
)

SCHEMA_VERSION = 2
_CANDIDATE_KIND = "dotmac.release_candidate"
_STAGING_KIND = "dotmac.staging_acceptance"
_PRODUCTION_KIND = "dotmac.production_authorization"


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
            "product_manifest_digest": evidence.product_manifest_digest.value,
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
            "product_manifest_digest",
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
            product_manifest_digest=ProductManifestDigest(
                _required_string(document, "product_manifest_digest")
            ),
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


def write_production_authorization(
    path: Path,
    candidate: ReleaseCandidateRecord,
) -> None:
    """Write one approved, digest-bound production authorization."""

    outcome = evaluate_production_eligibility(candidate)
    if not outcome.approved:
        blockers = ", ".join(blocker.value for blocker in outcome.blockers)
        raise EvidenceDocumentError(f"production authorization refused: {blockers}")
    if candidate.staging is None or candidate.main is None:
        raise EvidenceDocumentError("production authorization evidence is incomplete")
    artifact = candidate.artifact
    staging = candidate.staging
    main = candidate.main
    _write_document(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "kind": _PRODUCTION_KIND,
            "source_revision": artifact.source_revision.value,
            "source_tree": artifact.source_tree.value,
            "image_digest": artifact.image_digest.value,
            "product_manifest_digest": artifact.product_manifest_digest.value,
            "build_run_id": artifact.build_run_id.value,
            "staging_deployment_id": staging.deployment_id.value,
            "release_revision": main.release_revision.value,
            "release_tree": main.release_tree.value,
            "authorization_run_id": main.authorization_run_id.value,
        },
    )


def read_production_authorization(path: Path) -> ReleaseCandidateRecord:
    """Read and re-evaluate a production authorization document."""

    document = _read_document(
        path,
        kind=_PRODUCTION_KIND,
        fields={
            "source_revision",
            "source_tree",
            "image_digest",
            "product_manifest_digest",
            "build_run_id",
            "staging_deployment_id",
            "release_revision",
            "release_tree",
            "authorization_run_id",
        },
    )
    try:
        source_revision = GitCommitSha(_required_string(document, "source_revision"))
        source_tree = GitTreeSha(_required_string(document, "source_tree"))
        image_digest = OCIImageDigest(_required_string(document, "image_digest"))
        record = ReleaseCandidateRecord(
            artifact=ReleaseArtifactEvidence(
                source_revision=source_revision,
                source_tree=source_tree,
                image_digest=image_digest,
                product_manifest_digest=ProductManifestDigest(
                    _required_string(document, "product_manifest_digest")
                ),
                build_run_id=WorkflowRunId(
                    _required_positive_int(document, "build_run_id")
                ),
                source_ci_conclusion=EvidenceConclusion.SUCCESS,
            ),
            staging=StagingAcceptanceEvidence(
                deployment_id=StagingDeploymentId(
                    _required_positive_int(document, "staging_deployment_id")
                ),
                source_revision=source_revision,
                source_tree=source_tree,
                image_digest=image_digest,
                conclusion=EvidenceConclusion.SUCCESS,
            ),
            main=MainAuthorizationEvidence(
                authorization_run_id=WorkflowRunId(
                    _required_positive_int(document, "authorization_run_id")
                ),
                release_revision=GitCommitSha(
                    _required_string(document, "release_revision")
                ),
                release_tree=GitTreeSha(_required_string(document, "release_tree")),
                required_ci_conclusion=EvidenceConclusion.SUCCESS,
                source_revision_is_ancestor=True,
            ),
        )
    except ReleaseContractError as exc:
        raise EvidenceDocumentError(f"invalid production authorization: {exc}") from exc
    outcome = evaluate_production_eligibility(record)
    if not outcome.approved:
        blockers = ", ".join(blocker.value for blocker in outcome.blockers)
        raise EvidenceDocumentError(f"production authorization refused: {blockers}")
    return record


def verify_production_authorization(
    candidate: ReleaseCandidateRecord,
    *,
    expected_authorization_run_id: WorkflowRunId | None = None,
    expected_source_revision: GitCommitSha | None = None,
    expected_release_revision: GitCommitSha | None = None,
    expected_image_digest: OCIImageDigest | None = None,
) -> None:
    """Require an authorization to match the invoking workflow or host."""

    if candidate.main is None:
        raise EvidenceDocumentError("production authorization main evidence missing")
    if (
        expected_authorization_run_id is not None
        and candidate.main.authorization_run_id != expected_authorization_run_id
    ):
        raise EvidenceDocumentError("authorization workflow run does not match")
    if (
        expected_source_revision is not None
        and candidate.artifact.source_revision != expected_source_revision
    ):
        raise EvidenceDocumentError("authorized source revision does not match")
    if (
        expected_release_revision is not None
        and candidate.main.release_revision != expected_release_revision
    ):
        raise EvidenceDocumentError("authorized release revision does not match")
    if (
        expected_image_digest is not None
        and candidate.artifact.image_digest != expected_image_digest
    ):
        raise EvidenceDocumentError("authorized image digest does not match")


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
    candidate.add_argument("--product-manifest-digest", required=True)
    candidate.add_argument("--build-run-id", required=True, type=int)
    candidate.add_argument("--output", required=True, type=_document_path)

    verify = commands.add_parser("verify-candidate")
    verify.add_argument("--path", required=True, type=Path)
    verify.add_argument("--expected-source-revision", required=True)
    verify.add_argument("--expected-source-tree", required=True)
    verify.add_argument("--expected-build-run-id", required=True, type=int)
    verify.add_argument("--github-output", required=True, type=Path)

    inspect_candidate = commands.add_parser("read-candidate")
    inspect_candidate.add_argument("--path", required=True, type=Path)
    inspect_candidate.add_argument("--github-output", required=True, type=Path)

    staging = commands.add_parser("write-staging-acceptance")
    staging.add_argument("--deployment-id", required=True, type=int)
    staging.add_argument("--source-revision", required=True)
    staging.add_argument("--source-tree", required=True)
    staging.add_argument("--image-digest", required=True)
    staging.add_argument("--output", required=True, type=_document_path)

    authorize = commands.add_parser("authorize-production")
    authorize.add_argument("--candidate", required=True, type=Path)
    authorize.add_argument("--staging", required=True, type=Path)
    authorize.add_argument("--expected-build-run-id", required=True, type=int)
    authorize.add_argument(
        "--expected-staging-deployment-id",
        required=True,
        type=int,
    )
    authorize.add_argument("--authorization-run-id", required=True, type=int)
    authorize.add_argument("--release-revision", required=True)
    authorize.add_argument("--release-tree", required=True)
    authorize.add_argument("--source-revision-is-ancestor", action="store_true")
    authorize.add_argument("--output", required=True, type=_document_path)
    authorize.add_argument("--github-output", required=True, type=Path)

    production = commands.add_parser("verify-production")
    production.add_argument("--path", required=True, type=Path)
    production.add_argument("--expected-authorization-run-id", type=int)
    production.add_argument("--expected-source-revision")
    production.add_argument("--expected-release-revision")
    production.add_argument("--expected-image-digest")
    production.add_argument("--github-output", type=Path)
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
                product_manifest_digest=ProductManifestDigest(
                    args.product_manifest_digest
                ),
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
                "product_manifest_digest": evidence.product_manifest_digest.value,
                "build_run_id": evidence.build_run_id.value,
            },
        )
        return 0

    if args.command == "read-candidate":
        evidence = read_candidate_evidence(args.path)
        _append_github_outputs(
            args.github_output,
            {
                "source_revision": evidence.source_revision.value,
                "source_tree": evidence.source_tree.value,
                "image_digest": evidence.image_digest.value,
                "product_manifest_digest": evidence.product_manifest_digest.value,
                "build_run_id": evidence.build_run_id.value,
            },
        )
        return 0

    if args.command == "authorize-production":
        artifact = read_candidate_evidence(args.candidate)
        staging_evidence = read_staging_acceptance(args.staging)
        if artifact.build_run_id != WorkflowRunId(args.expected_build_run_id):
            raise EvidenceDocumentError("candidate build workflow run does not match")
        if staging_evidence.deployment_id != StagingDeploymentId(
            args.expected_staging_deployment_id
        ):
            raise EvidenceDocumentError(
                "staging deployment workflow run does not match"
            )
        record = ReleaseCandidateRecord(
            artifact=artifact,
            staging=staging_evidence,
            main=MainAuthorizationEvidence(
                authorization_run_id=WorkflowRunId(args.authorization_run_id),
                release_revision=GitCommitSha(args.release_revision),
                release_tree=GitTreeSha(args.release_tree),
                required_ci_conclusion=EvidenceConclusion.SUCCESS,
                source_revision_is_ancestor=args.source_revision_is_ancestor,
            ),
        )
        write_production_authorization(args.output, record)
        _append_github_outputs(
            args.github_output,
            {
                "source_revision": artifact.source_revision.value,
                "source_tree": artifact.source_tree.value,
                "image_digest": artifact.image_digest.value,
                "product_manifest_digest": artifact.product_manifest_digest.value,
                "build_run_id": artifact.build_run_id.value,
                "staging_deployment_id": staging_evidence.deployment_id.value,
                "release_revision": args.release_revision,
                "release_tree": args.release_tree,
                "authorization_run_id": args.authorization_run_id,
            },
        )
        return 0

    if args.command == "verify-production":
        record = read_production_authorization(args.path)
        verify_production_authorization(
            record,
            expected_authorization_run_id=(
                WorkflowRunId(args.expected_authorization_run_id)
                if args.expected_authorization_run_id is not None
                else None
            ),
            expected_source_revision=(
                GitCommitSha(args.expected_source_revision)
                if args.expected_source_revision is not None
                else None
            ),
            expected_release_revision=(
                GitCommitSha(args.expected_release_revision)
                if args.expected_release_revision is not None
                else None
            ),
            expected_image_digest=(
                OCIImageDigest(args.expected_image_digest)
                if args.expected_image_digest is not None
                else None
            ),
        )
        if args.github_output is not None:
            if record.main is None:
                raise EvidenceDocumentError("production main evidence missing")
            _append_github_outputs(
                args.github_output,
                {
                    "source_revision": record.artifact.source_revision.value,
                    "source_tree": record.artifact.source_tree.value,
                    "image_digest": record.artifact.image_digest.value,
                    "product_manifest_digest": (
                        record.artifact.product_manifest_digest.value
                    ),
                    "build_run_id": record.artifact.build_run_id.value,
                    "staging_deployment_id": (
                        record.staging.deployment_id.value
                        if record.staging is not None
                        else 0
                    ),
                    "release_revision": record.main.release_revision.value,
                    "release_tree": record.main.release_tree.value,
                    "authorization_run_id": record.main.authorization_run_id.value,
                },
            )
        else:
            if record.main is None:
                raise EvidenceDocumentError("production main evidence missing")
            print(record.main.release_revision.value)
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
