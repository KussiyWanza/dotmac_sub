from __future__ import annotations

import pytest

from scripts.release_artifact_contract import (
    AlembicHeads,
    BackupMode,
    BackupPolicyIssue,
    DeploymentTarget,
    EvidenceConclusion,
    GitCommitSha,
    GitTreeSha,
    HotfixNoMigrationEvidence,
    MainAuthorizationEvidence,
    MigrationGraphDigest,
    MigrationStateEvidence,
    OCIImageDigest,
    ProductionEligibilityBlocker,
    ReleaseArtifactEvidence,
    ReleaseCandidateRecord,
    ReleaseContractError,
    ReleaseContractErrorCode,
    StagingAcceptanceEvidence,
    StagingDeploymentId,
    WorkflowRunId,
    evaluate_production_eligibility,
    resolve_backup_policy,
)

DEV_SHA = GitCommitSha("1" * 40)
MAIN_SHA = GitCommitSha("2" * 40)
TREE_SHA = GitTreeSha("3" * 40)
IMAGE_DIGEST = OCIImageDigest("sha256:" + "4" * 64)
MIGRATION_DIGEST = MigrationGraphDigest("sha256:" + "5" * 64)


def _artifact(
    *,
    ci: EvidenceConclusion = EvidenceConclusion.SUCCESS,
) -> ReleaseArtifactEvidence:
    return ReleaseArtifactEvidence(
        source_revision=DEV_SHA,
        source_tree=TREE_SHA,
        image_digest=IMAGE_DIGEST,
        build_run_id=WorkflowRunId(100),
        source_ci_conclusion=ci,
    )


def _staging(
    *,
    conclusion: EvidenceConclusion = EvidenceConclusion.SUCCESS,
    source_revision: GitCommitSha = DEV_SHA,
    source_tree: GitTreeSha = TREE_SHA,
    image_digest: OCIImageDigest = IMAGE_DIGEST,
) -> StagingAcceptanceEvidence:
    return StagingAcceptanceEvidence(
        deployment_id=StagingDeploymentId(200),
        source_revision=source_revision,
        source_tree=source_tree,
        image_digest=image_digest,
        conclusion=conclusion,
    )


def _main(
    *,
    conclusion: EvidenceConclusion = EvidenceConclusion.SUCCESS,
    release_tree: GitTreeSha = TREE_SHA,
    source_revision_is_ancestor: bool = True,
) -> MainAuthorizationEvidence:
    return MainAuthorizationEvidence(
        authorization_run_id=WorkflowRunId(300),
        release_revision=MAIN_SHA,
        release_tree=release_tree,
        required_ci_conclusion=conclusion,
        source_revision_is_ancestor=source_revision_is_ancestor,
    )


def _migration_state(
    *,
    running_digest: MigrationGraphDigest = MIGRATION_DIGEST,
    candidate_digest: MigrationGraphDigest = MIGRATION_DIGEST,
    running_heads: AlembicHeads = AlembicHeads(("473",)),
    candidate_heads: AlembicHeads = AlembicHeads(("473",)),
    database_heads: AlembicHeads = AlembicHeads(("473",)),
) -> MigrationStateEvidence:
    return MigrationStateEvidence(
        running_graph_digest=running_digest,
        candidate_graph_digest=candidate_digest,
        running_image_heads=running_heads,
        candidate_image_heads=candidate_heads,
        database_heads=database_heads,
    )


def _hotfix(
    migration_state: MigrationStateEvidence | None = None,
) -> HotfixNoMigrationEvidence:
    return HotfixNoMigrationEvidence(
        change_reference="INC-2026-001",
        reason="Restore a route without changing the deployed schema",
        migration_state=migration_state or _migration_state(),
    )


@pytest.mark.parametrize(
    ("factory", "value", "code"),
    [
        (GitCommitSha, "ABC", ReleaseContractErrorCode.INVALID_GIT_COMMIT_SHA),
        (GitTreeSha, "3" * 39, ReleaseContractErrorCode.INVALID_GIT_TREE_SHA),
        (
            OCIImageDigest,
            "sha256:xyz",
            ReleaseContractErrorCode.INVALID_IMAGE_DIGEST,
        ),
        (
            MigrationGraphDigest,
            "5" * 64,
            ReleaseContractErrorCode.INVALID_MIGRATION_GRAPH_DIGEST,
        ),
    ],
)
def test_release_identifiers_fail_closed(
    factory: type[GitCommitSha]
    | type[GitTreeSha]
    | type[OCIImageDigest]
    | type[MigrationGraphDigest],
    value: str,
    code: ReleaseContractErrorCode,
) -> None:
    with pytest.raises(ReleaseContractError) as exc_info:
        factory(value)

    assert exc_info.value.code is code


def test_exact_staged_digest_and_identical_main_tree_are_production_eligible() -> None:
    candidate = ReleaseCandidateRecord(
        artifact=_artifact(),
        staging=_staging(),
        main=_main(),
    )

    outcome = evaluate_production_eligibility(candidate)

    assert outcome.approved
    assert outcome.blockers == ()


def test_missing_acceptance_and_main_authorization_block_promotion() -> None:
    outcome = evaluate_production_eligibility(
        ReleaseCandidateRecord(artifact=_artifact())
    )

    assert not outcome.approved
    assert outcome.blockers == (
        ProductionEligibilityBlocker.STAGING_EVIDENCE_MISSING,
        ProductionEligibilityBlocker.MAIN_AUTHORIZATION_MISSING,
    )


def test_staging_must_accept_the_exact_built_digest() -> None:
    outcome = evaluate_production_eligibility(
        ReleaseCandidateRecord(
            artifact=_artifact(),
            staging=_staging(
                image_digest=OCIImageDigest("sha256:" + "6" * 64),
            ),
            main=_main(),
        )
    )

    assert outcome.blockers == (
        ProductionEligibilityBlocker.STAGING_IMAGE_DIGEST_MISMATCH,
    )


def test_main_must_contain_the_staged_source_as_the_identical_tree() -> None:
    outcome = evaluate_production_eligibility(
        ReleaseCandidateRecord(
            artifact=_artifact(),
            staging=_staging(),
            main=_main(
                release_tree=GitTreeSha("7" * 40),
                source_revision_is_ancestor=False,
            ),
        )
    )

    assert outcome.blockers == (
        ProductionEligibilityBlocker.MAIN_TREE_MISMATCH,
        ProductionEligibilityBlocker.SOURCE_REVISION_NOT_IN_MAIN,
    )


def test_non_green_source_staging_and_main_evidence_all_block() -> None:
    outcome = evaluate_production_eligibility(
        ReleaseCandidateRecord(
            artifact=_artifact(ci=EvidenceConclusion.FAILURE),
            staging=_staging(conclusion=EvidenceConclusion.CANCELLED),
            main=_main(conclusion=EvidenceConclusion.PENDING),
        )
    )

    assert outcome.blockers == (
        ProductionEligibilityBlocker.SOURCE_CI_NOT_GREEN,
        ProductionEligibilityBlocker.STAGING_NOT_ACCEPTED,
        ProductionEligibilityBlocker.MAIN_CI_NOT_GREEN,
    )


def test_staging_owns_its_no_backup_policy() -> None:
    decision = resolve_backup_policy(target=DeploymentTarget.STAGING)

    assert decision.mode is BackupMode.SKIP_STAGING
    assert decision.issues == ()


def test_production_requires_backup_without_hotfix_evidence() -> None:
    decision = resolve_backup_policy(target=DeploymentTarget.PRODUCTION)

    assert decision.mode is BackupMode.REQUIRED
    assert not decision.hotfix_exception_accepted


def test_production_hotfix_may_skip_only_with_identical_migration_state() -> None:
    decision = resolve_backup_policy(
        target=DeploymentTarget.PRODUCTION,
        hotfix=_hotfix(),
    )

    assert decision.mode is BackupMode.SKIP_PRODUCTION_HOTFIX
    assert decision.hotfix_exception_accepted
    assert decision.issues == ()


def test_changed_migrations_or_database_heads_restore_production_backup() -> None:
    decision = resolve_backup_policy(
        target=DeploymentTarget.PRODUCTION,
        hotfix=_hotfix(
            _migration_state(
                candidate_digest=MigrationGraphDigest("sha256:" + "8" * 64),
                candidate_heads=AlembicHeads(("474",)),
                database_heads=AlembicHeads(("473",)),
            )
        ),
    )

    assert decision.mode is BackupMode.REQUIRED
    assert decision.issues == (
        BackupPolicyIssue.MIGRATION_GRAPH_CHANGED,
        BackupPolicyIssue.IMAGE_HEADS_CHANGED,
        BackupPolicyIssue.DATABASE_NOT_AT_CANDIDATE_HEADS,
    )


def test_hotfix_exception_is_not_a_staging_control() -> None:
    decision = resolve_backup_policy(
        target=DeploymentTarget.STAGING,
        hotfix=_hotfix(),
    )

    assert decision.mode is BackupMode.SKIP_STAGING
    assert decision.issues == (
        BackupPolicyIssue.HOTFIX_EXCEPTION_NOT_ALLOWED_ON_STAGING,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("change_reference", ""), ("reason", " ")],
)
def test_production_hotfix_evidence_requires_attribution(
    field: str,
    value: str,
) -> None:
    values = {
        "change_reference": "INC-2026-001",
        "reason": "No migration hotfix",
    }
    values[field] = value

    with pytest.raises(ReleaseContractError):
        HotfixNoMigrationEvidence(
            change_reference=values["change_reference"],
            reason=values["reason"],
            migration_state=_migration_state(),
        )
