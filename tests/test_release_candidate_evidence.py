from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.release_artifact_contract import (
    EvidenceConclusion,
    GitCommitSha,
    GitTreeSha,
    OCIImageDigest,
    ReleaseArtifactEvidence,
    StagingAcceptanceEvidence,
    StagingDeploymentId,
    WorkflowRunId,
)
from scripts.release_candidate_evidence import (
    EvidenceDocumentError,
    read_candidate_evidence,
    read_staging_acceptance,
    verify_candidate_evidence,
    write_candidate_evidence,
    write_staging_acceptance,
)

SOURCE_REVISION = GitCommitSha("1" * 40)
SOURCE_TREE = GitTreeSha("2" * 40)
IMAGE_DIGEST = OCIImageDigest("sha256:" + "3" * 64)
BUILD_RUN_ID = WorkflowRunId(400)


def _candidate(
    *,
    conclusion: EvidenceConclusion = EvidenceConclusion.SUCCESS,
) -> ReleaseArtifactEvidence:
    return ReleaseArtifactEvidence(
        source_revision=SOURCE_REVISION,
        source_tree=SOURCE_TREE,
        image_digest=IMAGE_DIGEST,
        build_run_id=BUILD_RUN_ID,
        source_ci_conclusion=conclusion,
    )


def test_candidate_evidence_round_trips_exact_typed_identity(tmp_path: Path) -> None:
    path = tmp_path / "candidate.json"

    write_candidate_evidence(path, _candidate())

    assert read_candidate_evidence(path) == _candidate()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "kind": "dotmac.release_candidate",
        "source_revision": SOURCE_REVISION.value,
        "source_tree": SOURCE_TREE.value,
        "image_digest": IMAGE_DIGEST.value,
        "build_run_id": BUILD_RUN_ID.value,
        "source_ci_conclusion": "success",
    }


@pytest.mark.parametrize(
    ("revision", "tree", "run_id", "message"),
    [
        (GitCommitSha("4" * 40), SOURCE_TREE, BUILD_RUN_ID, "source revision"),
        (SOURCE_REVISION, GitTreeSha("5" * 40), BUILD_RUN_ID, "source tree"),
        (SOURCE_REVISION, SOURCE_TREE, WorkflowRunId(401), "workflow run"),
    ],
)
def test_candidate_verification_rejects_trigger_identity_mismatch(
    revision: GitCommitSha,
    tree: GitTreeSha,
    run_id: WorkflowRunId,
    message: str,
) -> None:
    with pytest.raises(EvidenceDocumentError, match=message):
        verify_candidate_evidence(
            _candidate(),
            expected_source_revision=revision,
            expected_source_tree=tree,
            expected_build_run_id=run_id,
        )


def test_candidate_verification_rejects_non_green_source_ci() -> None:
    with pytest.raises(EvidenceDocumentError, match="source CI"):
        verify_candidate_evidence(
            _candidate(conclusion=EvidenceConclusion.FAILURE),
            expected_source_revision=SOURCE_REVISION,
            expected_source_tree=SOURCE_TREE,
            expected_build_run_id=BUILD_RUN_ID,
        )


def test_candidate_document_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "candidate.json"
    write_candidate_evidence(path, _candidate())
    document = json.loads(path.read_text(encoding="utf-8"))
    document["mutable_tag"] = "latest"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(EvidenceDocumentError, match="fields must be exactly"):
        read_candidate_evidence(path)


def test_candidate_document_normalizes_invalid_typed_identity(tmp_path: Path) -> None:
    path = tmp_path / "candidate.json"
    write_candidate_evidence(path, _candidate())
    document = json.loads(path.read_text(encoding="utf-8"))
    document["image_digest"] = "latest"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(EvidenceDocumentError, match="invalid candidate evidence"):
        read_candidate_evidence(path)


def test_staging_acceptance_round_trips_the_same_digest(tmp_path: Path) -> None:
    path = tmp_path / "acceptance.json"
    evidence = StagingAcceptanceEvidence(
        deployment_id=StagingDeploymentId(500),
        source_revision=SOURCE_REVISION,
        source_tree=SOURCE_TREE,
        image_digest=IMAGE_DIGEST,
        conclusion=EvidenceConclusion.SUCCESS,
    )

    write_staging_acceptance(path, evidence)

    assert read_staging_acceptance(path) == evidence
