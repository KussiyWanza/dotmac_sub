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
    ProductionBootstrapAuthorization,
    ProductionEligibilityBlocker,
    ProductionServerName,
    ProductManifestDigest,
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

# The staged candidate: built, CI-green, deployed to staging, and the
# revision production actually runs.
STAGED_SHA = GitCommitSha("1" * 40)
# The protected main tip whose workflow code authorized the release. It
# moves independently of the artifact and is NOT what gets deployed.
AUTHORIZATION_MAIN_SHA = GitCommitSha("2" * 40)
TREE_SHA = GitTreeSha("3" * 40)
IMAGE_DIGEST = OCIImageDigest("sha256:" + "4" * 64)
PRODUCT_MANIFEST_DIGEST = ProductManifestDigest("sha256:" + "6" * 64)
MIGRATION_DIGEST = MigrationGraphDigest("sha256:" + "5" * 64)


def _artifact(
    *,
    ci: EvidenceConclusion = EvidenceConclusion.SUCCESS,
) -> ReleaseArtifactEvidence:
    return ReleaseArtifactEvidence(
        source_revision=STAGED_SHA,
        source_tree=TREE_SHA,
        image_digest=IMAGE_DIGEST,
        product_manifest_digest=PRODUCT_MANIFEST_DIGEST,
        build_run_id=WorkflowRunId(100),
        evidence_run_id=WorkflowRunId(100),
        source_ci_conclusion=ci,
    )


def _staging(
    *,
    conclusion: EvidenceConclusion = EvidenceConclusion.SUCCESS,
    source_revision: GitCommitSha = STAGED_SHA,
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
    release_revision: GitCommitSha = STAGED_SHA,
    release_tree: GitTreeSha = TREE_SHA,
    source_revision_is_ancestor: bool = True,
    authorization_main_revision: GitCommitSha = AUTHORIZATION_MAIN_SHA,
) -> MainAuthorizationEvidence:
    return MainAuthorizationEvidence(
        authorization_run_id=WorkflowRunId(300),
        authorization_main_revision=authorization_main_revision,
        release_revision=release_revision,
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
            ProductManifestDigest,
            "sha256:xyz",
            ReleaseContractErrorCode.INVALID_PRODUCT_MANIFEST_DIGEST,
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
    | type[ProductManifestDigest]
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


def test_main_only_release_authorizes_the_staged_source_commit() -> None:
    """The authorized artifact IS the staged candidate, and main has moved on.

    This is the ordinary case on a single trunk, and the one the old
    "must be a distinct commit" rule made impossible: the release revision
    equals the staged candidate while the AUTHORIZING main tip is a later,
    different commit. Both identities present, neither conflated.
    """

    outcome = evaluate_production_eligibility(
        ReleaseCandidateRecord(
            artifact=_artifact(),
            staging=_staging(),
            main=_main(
                release_revision=STAGED_SHA,
                authorization_main_revision=AUTHORIZATION_MAIN_SHA,
            ),
        )
    )

    assert outcome.approved
    assert outcome.blockers == ()


def test_authorizing_main_may_equal_the_staged_revision() -> None:
    """Nothing landed since the candidate; the two identities coincide.

    Coincidence must be permitted, because it is what happens on a quiet
    trunk. The pair of tests above and here pin both sides: the rule is
    agreement with the staged candidate, never a relationship to the tip.
    """

    outcome = evaluate_production_eligibility(
        ReleaseCandidateRecord(
            artifact=_artifact(),
            staging=_staging(),
            main=_main(
                release_revision=STAGED_SHA,
                authorization_main_revision=STAGED_SHA,
            ),
        )
    )

    assert outcome.approved


def test_authorizing_a_revision_other_than_the_staged_candidate_is_refused() -> None:
    """Negative half of the same rule.

    Releasing main's newer tip while presenting the older candidate's staging
    evidence would deploy an image built from code that was never staged.
    Without this, relaxing the tip requirement would have opened exactly that.
    """

    outcome = evaluate_production_eligibility(
        ReleaseCandidateRecord(
            artifact=_artifact(),
            staging=_staging(),
            main=_main(release_revision=AUTHORIZATION_MAIN_SHA),
        )
    )

    assert not outcome.approved
    assert ProductionEligibilityBlocker.RELEASE_REVISION_NOT_STAGED in outcome.blockers


def test_staged_revision_must_still_be_reachable_from_authorizing_main() -> None:
    """Reverted or orphaned work must not reach production.

    Ancestry is the check that replaced tip-equality; if it did not bite, a
    candidate whose commit was reverted off main could still be deployed.
    """

    outcome = evaluate_production_eligibility(
        ReleaseCandidateRecord(
            artifact=_artifact(),
            staging=_staging(),
            main=_main(source_revision_is_ancestor=False),
        )
    )

    assert not outcome.approved
    assert ProductionEligibilityBlocker.SOURCE_REVISION_NOT_IN_MAIN in outcome.blockers


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


def test_production_server_name_is_exact() -> None:
    assert ProductionServerName("dotmac-sub-prod").value == "dotmac-sub-prod"

    with pytest.raises(ReleaseContractError) as exc_info:
        ProductionServerName("dotmac-sub-staging")

    assert exc_info.value.code is ReleaseContractErrorCode.INVALID_PRODUCTION_SERVER


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        (
            "change_reference",
            "",
            ReleaseContractErrorCode.BOOTSTRAP_CHANGE_REFERENCE_REQUIRED,
        ),
        ("reason", " ", ReleaseContractErrorCode.BOOTSTRAP_REASON_REQUIRED),
    ],
)
def test_production_bootstrap_requires_typed_attribution(
    field: str,
    value: str,
    code: ReleaseContractErrorCode,
) -> None:
    values = {
        "change_reference": "CHG-2026-0829",
        "reason": "initialize the empty production application slot",
    }
    values[field] = value

    with pytest.raises(ReleaseContractError) as exc_info:
        ProductionBootstrapAuthorization(
            target_revision=STAGED_SHA,
            target_server=ProductionServerName("dotmac-sub-prod"),
            change_reference=values["change_reference"],
            reason=values["reason"],
        )

    assert exc_info.value.code is code


def test_every_production_blocker_is_individually_reachable() -> None:
    """Sensitivity proof for the whole eligibility rule set.

    A rule that can never fire is not a guard, and a rule that fires on every
    correct release is worse than none. Both failure modes were live in this
    file: `SOURCE_AND_RELEASE_REVISION_MATCH` blocked every main-only release
    until it was removed, and nothing detected it because no test asserted
    that each blocker is reachable *and* avoidable.

    Enumerating the enum (rather than a hand-listed set) is the point: a new
    blocker that no input can trigger fails here on the day it is added.
    """

    cases: dict[ProductionEligibilityBlocker, ReleaseCandidateRecord] = {
        ProductionEligibilityBlocker.SOURCE_CI_NOT_GREEN: ReleaseCandidateRecord(
            artifact=_artifact(ci=EvidenceConclusion.FAILURE),
            staging=_staging(),
            main=_main(),
        ),
        ProductionEligibilityBlocker.STAGING_EVIDENCE_MISSING: ReleaseCandidateRecord(
            artifact=_artifact(), staging=None, main=_main()
        ),
        ProductionEligibilityBlocker.STAGING_NOT_ACCEPTED: ReleaseCandidateRecord(
            artifact=_artifact(),
            staging=_staging(conclusion=EvidenceConclusion.CANCELLED),
            main=_main(),
        ),
        ProductionEligibilityBlocker.STAGING_SOURCE_REVISION_MISMATCH: (
            ReleaseCandidateRecord(
                artifact=_artifact(),
                staging=_staging(source_revision=GitCommitSha("9" * 40)),
                main=_main(),
            )
        ),
        ProductionEligibilityBlocker.STAGING_SOURCE_TREE_MISMATCH: (
            ReleaseCandidateRecord(
                artifact=_artifact(),
                staging=_staging(source_tree=GitTreeSha("9" * 40)),
                main=_main(),
            )
        ),
        ProductionEligibilityBlocker.STAGING_IMAGE_DIGEST_MISMATCH: (
            ReleaseCandidateRecord(
                artifact=_artifact(),
                staging=_staging(image_digest=OCIImageDigest("sha256:" + "9" * 64)),
                main=_main(),
            )
        ),
        ProductionEligibilityBlocker.MAIN_AUTHORIZATION_MISSING: (
            ReleaseCandidateRecord(artifact=_artifact(), staging=_staging(), main=None)
        ),
        ProductionEligibilityBlocker.MAIN_CI_NOT_GREEN: ReleaseCandidateRecord(
            artifact=_artifact(),
            staging=_staging(),
            main=_main(conclusion=EvidenceConclusion.PENDING),
        ),
        ProductionEligibilityBlocker.MAIN_TREE_MISMATCH: ReleaseCandidateRecord(
            artifact=_artifact(),
            staging=_staging(),
            main=_main(release_tree=GitTreeSha("9" * 40)),
        ),
        ProductionEligibilityBlocker.SOURCE_REVISION_NOT_IN_MAIN: (
            ReleaseCandidateRecord(
                artifact=_artifact(),
                staging=_staging(),
                main=_main(source_revision_is_ancestor=False),
            )
        ),
        ProductionEligibilityBlocker.RELEASE_REVISION_NOT_STAGED: (
            ReleaseCandidateRecord(
                artifact=_artifact(),
                staging=_staging(),
                main=_main(release_revision=AUTHORIZATION_MAIN_SHA),
            )
        ),
    }

    assert set(cases) == set(ProductionEligibilityBlocker), (
        "every blocker needs a reachability case"
    )

    for blocker, record in cases.items():
        outcome = evaluate_production_eligibility(record)
        assert blocker in outcome.blockers, f"{blocker.value} is unreachable"

    # Avoidability: the fully correct record trips none of them. Without this
    # half, a rule that always fires would still pass the loop above.
    healthy = evaluate_production_eligibility(
        ReleaseCandidateRecord(artifact=_artifact(), staging=_staging(), main=_main())
    )
    assert healthy.blockers == ()
