from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.release_artifact_contract import (
    EvidenceConclusion,
    GitCommitSha,
    GitTreeSha,
    MainAuthorizationEvidence,
    OCIImageDigest,
    ProductionBootstrapAuthorization,
    ProductionRollbackAuthorization,
    ProductionServerName,
    ProductManifestDigest,
    ReleaseArtifactEvidence,
    ReleaseCandidateRecord,
    StagingAcceptanceEvidence,
    StagingDeploymentId,
    WorkflowRunId,
)
from scripts.release_candidate_evidence import (
    EvidenceDocumentError,
    read_bootstrap_authorization,
    read_candidate_evidence,
    read_production_authorization,
    read_rollback_authorization,
    read_staging_acceptance,
    verify_bootstrap_authorization,
    verify_candidate_evidence,
    verify_production_authorization,
    verify_rollback_authorization,
    write_bootstrap_authorization,
    write_candidate_evidence,
    write_production_authorization,
    write_rollback_authorization,
    write_staging_acceptance,
)

SOURCE_REVISION = GitCommitSha("1" * 40)
# A later main commit that performed the authorization. Distinct from the
# staged revision on purpose: on one trunk main moves after every merge.
AUTHORIZING_MAIN_REVISION = GitCommitSha("6" * 40)
SOURCE_TREE = GitTreeSha("2" * 40)
IMAGE_DIGEST = OCIImageDigest("sha256:" + "3" * 64)
PRODUCT_MANIFEST_DIGEST = ProductManifestDigest("sha256:" + "4" * 64)
BUILD_RUN_ID = WorkflowRunId(400)
EVIDENCE_RUN_ID = WorkflowRunId(400)


def _candidate(
    *,
    conclusion: EvidenceConclusion = EvidenceConclusion.SUCCESS,
    build_run_id: WorkflowRunId = BUILD_RUN_ID,
    evidence_run_id: WorkflowRunId = EVIDENCE_RUN_ID,
) -> ReleaseArtifactEvidence:
    return ReleaseArtifactEvidence(
        source_revision=SOURCE_REVISION,
        source_tree=SOURCE_TREE,
        image_digest=IMAGE_DIGEST,
        product_manifest_digest=PRODUCT_MANIFEST_DIGEST,
        build_run_id=build_run_id,
        evidence_run_id=evidence_run_id,
        source_ci_conclusion=conclusion,
    )


def test_candidate_evidence_round_trips_exact_typed_identity(tmp_path: Path) -> None:
    path = tmp_path / "candidate.json"

    write_candidate_evidence(path, _candidate())

    assert read_candidate_evidence(path) == _candidate()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 3,
        "kind": "dotmac.release_candidate",
        "source_revision": SOURCE_REVISION.value,
        "source_tree": SOURCE_TREE.value,
        "image_digest": IMAGE_DIGEST.value,
        "product_manifest_digest": PRODUCT_MANIFEST_DIGEST.value,
        "build_run_id": BUILD_RUN_ID.value,
        "evidence_run_id": EVIDENCE_RUN_ID.value,
        "source_ci_conclusion": "success",
    }


def test_candidate_evidence_distinguishes_build_run_from_evidence_run(
    tmp_path: Path,
) -> None:
    """A resumed candidate build writes evidence under a different run.

    The image itself -- and therefore `build_run_id` -- still names the
    original build; only `evidence_run_id` names the run that (re-)wrote this
    document. Conflating them is exactly what made a resume unable to satisfy
    both downstream checks at once.
    """

    path = tmp_path / "candidate.json"
    resumed = _candidate(build_run_id=BUILD_RUN_ID, evidence_run_id=WorkflowRunId(999))

    write_candidate_evidence(path, resumed)
    restored = read_candidate_evidence(path)

    assert restored.build_run_id == BUILD_RUN_ID
    assert restored.evidence_run_id == WorkflowRunId(999)
    assert restored.build_run_id != restored.evidence_run_id


@pytest.mark.parametrize(
    ("revision", "tree", "run_id", "message"),
    [
        (GitCommitSha("4" * 40), SOURCE_TREE, EVIDENCE_RUN_ID, "source revision"),
        (SOURCE_REVISION, GitTreeSha("5" * 40), EVIDENCE_RUN_ID, "source tree"),
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
            expected_evidence_run_id=run_id,
        )


def test_candidate_verification_checks_evidence_run_not_build_run() -> None:
    """A resume's `build_run_id` legitimately differs from the triggering run.

    Verification must accept it anyway, because it only binds
    `evidence_run_id` to the run that is actually downloading this artifact.
    """

    resumed = _candidate(build_run_id=WorkflowRunId(111), evidence_run_id=BUILD_RUN_ID)

    verify_candidate_evidence(
        resumed,
        expected_source_revision=SOURCE_REVISION,
        expected_source_tree=SOURCE_TREE,
        expected_evidence_run_id=BUILD_RUN_ID,
    )


def test_candidate_verification_rejects_non_green_source_ci() -> None:
    with pytest.raises(EvidenceDocumentError, match="source CI"):
        verify_candidate_evidence(
            _candidate(conclusion=EvidenceConclusion.FAILURE),
            expected_source_revision=SOURCE_REVISION,
            expected_source_tree=SOURCE_TREE,
            expected_evidence_run_id=EVIDENCE_RUN_ID,
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


def _authorized_record() -> ReleaseCandidateRecord:
    return ReleaseCandidateRecord(
        artifact=_candidate(),
        staging=StagingAcceptanceEvidence(
            deployment_id=StagingDeploymentId(500),
            source_revision=SOURCE_REVISION,
            source_tree=SOURCE_TREE,
            image_digest=IMAGE_DIGEST,
            conclusion=EvidenceConclusion.SUCCESS,
        ),
        main=MainAuthorizationEvidence(
            authorization_run_id=WorkflowRunId(600),
            authorization_main_revision=AUTHORIZING_MAIN_REVISION,
            release_revision=SOURCE_REVISION,
            release_tree=SOURCE_TREE,
            required_ci_conclusion=EvidenceConclusion.SUCCESS,
            source_revision_is_ancestor=True,
        ),
    )


def test_production_authorization_round_trips_distinct_main_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authorization.json"
    record = ReleaseCandidateRecord(
        artifact=_candidate(),
        staging=StagingAcceptanceEvidence(
            deployment_id=StagingDeploymentId(500),
            source_revision=SOURCE_REVISION,
            source_tree=SOURCE_TREE,
            image_digest=IMAGE_DIGEST,
            conclusion=EvidenceConclusion.SUCCESS,
        ),
        main=MainAuthorizationEvidence(
            authorization_run_id=WorkflowRunId(600),
            authorization_main_revision=AUTHORIZING_MAIN_REVISION,
            release_revision=SOURCE_REVISION,
            release_tree=SOURCE_TREE,
            required_ci_conclusion=EvidenceConclusion.SUCCESS,
            source_revision_is_ancestor=True,
        ),
    )

    write_production_authorization(path, record)
    restored = read_production_authorization(path)
    verify_production_authorization(
        restored,
        expected_authorization_run_id=WorkflowRunId(600),
        expected_source_revision=SOURCE_REVISION,
        expected_authorization_main_revision=AUTHORIZING_MAIN_REVISION,
        expected_release_revision=SOURCE_REVISION,
        expected_image_digest=IMAGE_DIGEST,
    )

    assert restored == record


def test_production_authorization_rejects_a_different_staging_digest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authorization.json"
    record = ReleaseCandidateRecord(
        artifact=_candidate(),
        staging=StagingAcceptanceEvidence(
            deployment_id=StagingDeploymentId(500),
            source_revision=SOURCE_REVISION,
            source_tree=SOURCE_TREE,
            image_digest=OCIImageDigest("sha256:" + "9" * 64),
            conclusion=EvidenceConclusion.SUCCESS,
        ),
        main=MainAuthorizationEvidence(
            authorization_run_id=WorkflowRunId(600),
            authorization_main_revision=AUTHORIZING_MAIN_REVISION,
            release_revision=SOURCE_REVISION,
            release_tree=SOURCE_TREE,
            required_ci_conclusion=EvidenceConclusion.SUCCESS,
            source_revision_is_ancestor=True,
        ),
    )

    with pytest.raises(EvidenceDocumentError, match="staging_image_digest_mismatch"):
        write_production_authorization(path, record)


def test_verifier_binds_the_authorizing_main_revision(tmp_path: Path) -> None:
    """The deploy must prove WHICH main authorized it, not just that one did.

    production-deploy.yml passes the authorization run's own head_sha here. If
    the document could name any main revision, an authorization produced by
    older workflow code would be indistinguishable from one produced by the
    protected code the deploy believes it is running under.
    """

    path = tmp_path / "authorization.json"
    write_production_authorization(path, _authorized_record())
    restored = read_production_authorization(path)

    verify_production_authorization(
        restored, expected_authorization_main_revision=AUTHORIZING_MAIN_REVISION
    )
    with pytest.raises(EvidenceDocumentError, match="authorizing main revision"):
        verify_production_authorization(
            restored,
            expected_authorization_main_revision=GitCommitSha("7" * 40),
        )


def test_rollback_authorization_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "rollback.json"
    authorization = ProductionRollbackAuthorization(
        from_revision=AUTHORIZING_MAIN_REVISION,
        to_revision=SOURCE_REVISION,
        change_reference="INC-4212",
        reason="revert the billing regression",
    )

    write_rollback_authorization(path, authorization)
    restored = read_rollback_authorization(path)

    assert restored == authorization
    verify_rollback_authorization(
        restored,
        running_revision=AUTHORIZING_MAIN_REVISION,
        target_revision=SOURCE_REVISION,
    )


def test_rollback_authorization_is_bound_to_one_exact_transition(
    tmp_path: Path,
) -> None:
    """It authorizes a transition, not a standing permission to go backwards.

    Both halves must bite: an authorization written for a different running
    revision, or for a different target, is refused. Otherwise a document kept
    from an earlier incident would silently authorize an unrelated rollback.
    """

    path = tmp_path / "rollback.json"
    write_rollback_authorization(
        path,
        ProductionRollbackAuthorization(
            from_revision=AUTHORIZING_MAIN_REVISION,
            to_revision=SOURCE_REVISION,
            change_reference="INC-4212",
            reason="revert the billing regression",
        ),
    )
    restored = read_rollback_authorization(path)

    with pytest.raises(EvidenceDocumentError, match="running revision"):
        verify_rollback_authorization(
            restored,
            running_revision=GitCommitSha("8" * 40),
            target_revision=SOURCE_REVISION,
        )
    with pytest.raises(EvidenceDocumentError, match="deploying revision"):
        verify_rollback_authorization(
            restored,
            running_revision=AUTHORIZING_MAIN_REVISION,
            target_revision=GitCommitSha("8" * 40),
        )


def test_bootstrap_authorization_round_trips_exact_host_and_revision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bootstrap.json"
    authorization = ProductionBootstrapAuthorization(
        target_revision=SOURCE_REVISION,
        target_server=ProductionServerName("dotmac-sub-prod"),
        change_reference="CHG-2026-0829",
        reason="initialize the empty production application slot",
    )

    write_bootstrap_authorization(path, authorization)
    restored = read_bootstrap_authorization(path)

    assert restored == authorization
    verify_bootstrap_authorization(
        restored,
        target_revision=SOURCE_REVISION,
        target_server=ProductionServerName("dotmac-sub-prod"),
    )


def test_bootstrap_authorization_is_not_reusable_for_another_deploy(
    tmp_path: Path,
) -> None:
    """The revision binding bites, so bootstrap is not a standing bypass."""

    path = tmp_path / "bootstrap.json"
    write_bootstrap_authorization(
        path,
        ProductionBootstrapAuthorization(
            target_revision=SOURCE_REVISION,
            target_server=ProductionServerName("dotmac-sub-prod"),
            change_reference="CHG-2026-0829",
            reason="initialize the empty production application slot",
        ),
    )
    restored = read_bootstrap_authorization(path)

    with pytest.raises(EvidenceDocumentError, match="deploying revision"):
        verify_bootstrap_authorization(
            restored,
            target_revision=GitCommitSha("8" * 40),
            target_server=ProductionServerName("dotmac-sub-prod"),
        )


def test_bootstrap_authorization_refuses_a_non_production_server(
    tmp_path: Path,
) -> None:
    """The host binding is enforced when the document is read, not later.

    `ProductionServerName` admits exactly one value, so a document naming any
    other host cannot be loaded at all. That is where the server binding has to
    bite: `verify_bootstrap_authorization` never sees a foreign host, so a test
    written against it would be vacuous.
    """

    path = tmp_path / "bootstrap.json"
    write_bootstrap_authorization(
        path,
        ProductionBootstrapAuthorization(
            target_revision=SOURCE_REVISION,
            target_server=ProductionServerName("dotmac-sub-prod"),
            change_reference="CHG-2026-0829",
            reason="initialize the empty production application slot",
        ),
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    document["target_server"] = "dotmac-sub-staging"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(EvidenceDocumentError, match="dotmac-sub-prod"):
        read_bootstrap_authorization(path)
