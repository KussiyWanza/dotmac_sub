"""Typed evidence and policy contracts for build-once artifact promotion.

This module is intentionally side-effect free. Workflow and deployment adapters
populate these identities and observations; pure evaluators own the resulting
release and backup-policy decisions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALEMBIC_REVISION = re.compile(r"^[A-Za-z0-9_.-]+$")
_CHANGE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class ReleaseContractErrorCode(str, Enum):
    """Stable validation failures for release evidence adapters."""

    INVALID_GIT_COMMIT_SHA = "release_contract.invalid_git_commit_sha"
    INVALID_GIT_TREE_SHA = "release_contract.invalid_git_tree_sha"
    INVALID_IMAGE_DIGEST = "release_contract.invalid_image_digest"
    INVALID_PRODUCT_MANIFEST_DIGEST = "release_contract.invalid_product_manifest_digest"
    INVALID_WORKFLOW_RUN_ID = "release_contract.invalid_workflow_run_id"
    INVALID_DEPLOYMENT_ID = "release_contract.invalid_deployment_id"
    INVALID_MIGRATION_GRAPH_DIGEST = "release_contract.invalid_migration_graph_digest"
    INVALID_ALEMBIC_HEADS = "release_contract.invalid_alembic_heads"
    HOTFIX_CHANGE_REFERENCE_REQUIRED = (
        "release_contract.hotfix_change_reference_required"
    )
    HOTFIX_REASON_REQUIRED = "release_contract.hotfix_reason_required"
    ROLLBACK_REVISIONS_IDENTICAL = "release_contract.rollback_revisions_identical"
    ROLLBACK_CHANGE_REFERENCE_REQUIRED = (
        "release_contract.rollback_change_reference_required"
    )
    ROLLBACK_REASON_REQUIRED = "release_contract.rollback_reason_required"
    INVALID_PRODUCTION_SERVER = "release_contract.invalid_production_server"
    BOOTSTRAP_CHANGE_REFERENCE_REQUIRED = (
        "release_contract.bootstrap_change_reference_required"
    )
    BOOTSTRAP_REASON_REQUIRED = "release_contract.bootstrap_reason_required"


class ReleaseContractError(RuntimeError):
    """Malformed authoritative release input at an adapter boundary."""

    def __init__(
        self,
        *,
        code: ReleaseContractErrorCode,
        field: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


def _require_full_git_sha(
    value: str,
    *,
    field: str,
    code: ReleaseContractErrorCode,
) -> None:
    if not isinstance(value, str) or _FULL_GIT_SHA.fullmatch(value) is None:
        raise ReleaseContractError(code=code, field=field, message=f"invalid {field}")


def _require_sha256_digest(
    value: str,
    *,
    field: str,
    code: ReleaseContractErrorCode,
) -> None:
    if not isinstance(value, str) or _SHA256_DIGEST.fullmatch(value) is None:
        raise ReleaseContractError(code=code, field=field, message=f"invalid {field}")


@dataclass(frozen=True, slots=True)
class GitCommitSha:
    """Exact source commit embedded in an application image."""

    value: str

    def __post_init__(self) -> None:
        _require_full_git_sha(
            self.value,
            field="git commit SHA",
            code=ReleaseContractErrorCode.INVALID_GIT_COMMIT_SHA,
        )


@dataclass(frozen=True, slots=True)
class GitTreeSha:
    """Content identity used to prove staged source and release match."""

    value: str

    def __post_init__(self) -> None:
        _require_full_git_sha(
            self.value,
            field="git tree SHA",
            code=ReleaseContractErrorCode.INVALID_GIT_TREE_SHA,
        )


@dataclass(frozen=True, slots=True)
class OCIImageDigest:
    """Immutable OCI manifest digest shared by staging and production."""

    value: str

    def __post_init__(self) -> None:
        _require_sha256_digest(
            self.value,
            field="OCI image digest",
            code=ReleaseContractErrorCode.INVALID_IMAGE_DIGEST,
        )


@dataclass(frozen=True, slots=True)
class ProductManifestDigest:
    """Digest of the canonical manifest embedded in one application image."""

    value: str

    def __post_init__(self) -> None:
        _require_sha256_digest(
            self.value,
            field="product manifest digest",
            code=ReleaseContractErrorCode.INVALID_PRODUCT_MANIFEST_DIGEST,
        )


@dataclass(frozen=True, slots=True)
class WorkflowRunId:
    """GitHub Actions workflow-run evidence identifier."""

    value: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int)
            or self.value <= 0
        ):
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.INVALID_WORKFLOW_RUN_ID,
                field="workflow run ID",
                message="workflow run ID must be a positive integer",
            )


@dataclass(frozen=True, slots=True)
class StagingDeploymentId:
    """Durable GitHub staging-deployment evidence identifier."""

    value: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int)
            or self.value <= 0
        ):
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.INVALID_DEPLOYMENT_ID,
                field="staging deployment ID",
                message="staging deployment ID must be a positive integer",
            )


@dataclass(frozen=True, slots=True)
class ProductionServerName:
    """Exact production host identity accepted by the release control plane."""

    value: str

    def __post_init__(self) -> None:
        if self.value != "dotmac-sub-prod":
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.INVALID_PRODUCTION_SERVER,
                field="production server name",
                message="production server name must be dotmac-sub-prod",
            )


class EvidenceConclusion(str, Enum):
    """Normalized conclusion for external workflow/deployment observations."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ReleaseArtifactEvidence:
    """Immutable output of the one application-image build.

    ``build_run_id`` and ``evidence_run_id`` are deliberately distinct
    identities (schema v3). They coincide on a fresh build, and diverge on a
    resumed run that reuses a previously published image: ``build_run_id`` is
    the run that BUILT the image (baked into the image's own
    ``io.dotmac.release.build-run`` label, and never rewritten), while
    ``evidence_run_id`` is the run that wrote/uploaded THIS evidence document.
    Conflating them previously broke whichever downstream check needed the
    other meaning -- the image-producing run for the deployed-container label
    assertion, or the evidence-writing run for the artifact-download identity.
    """

    source_revision: GitCommitSha
    source_tree: GitTreeSha
    image_digest: OCIImageDigest
    product_manifest_digest: ProductManifestDigest
    build_run_id: WorkflowRunId
    evidence_run_id: WorkflowRunId
    source_ci_conclusion: EvidenceConclusion


@dataclass(frozen=True, slots=True)
class StagingAcceptanceEvidence:
    """Observed staging result for one exact release artifact."""

    deployment_id: StagingDeploymentId
    source_revision: GitCommitSha
    source_tree: GitTreeSha
    image_digest: OCIImageDigest
    conclusion: EvidenceConclusion


@dataclass(frozen=True, slots=True)
class MainAuthorizationEvidence:
    """Main-branch evidence authorizing an already-staged artifact.

    Two revisions are deliberately distinct identities, and conflating them is
    what forced a rebuild whenever `main` moved:

    ``authorization_main_revision``
        The protected `main` tip whose checked-in workflow and verifier code
        performed this authorization. It identifies the AUTHORITY, never the
        deployed application.
    ``release_revision``
        The exact staged candidate whose tree, digest, CI and staging
        acceptance are being authorized. It identifies the ARTIFACT, and it is
        what production actually runs.

    They coincide only when nothing has landed on `main` since the candidate
    was built. Requiring that coincidence is a freshness rule, not a safety
    rule; safety comes from ``release_revision`` matching the staged candidate
    exactly and remaining an ancestor of ``authorization_main_revision``.
    """

    authorization_run_id: WorkflowRunId
    authorization_main_revision: GitCommitSha
    release_revision: GitCommitSha
    release_tree: GitTreeSha
    required_ci_conclusion: EvidenceConclusion
    source_revision_is_ancestor: bool


@dataclass(frozen=True, slots=True)
class ReleaseCandidateRecord:
    """Evidence assembled for one build-once release candidate."""

    artifact: ReleaseArtifactEvidence
    staging: StagingAcceptanceEvidence | None = None
    main: MainAuthorizationEvidence | None = None


@dataclass(frozen=True, slots=True)
class ProductionRollbackAuthorization:
    """Explicit, transition-bound authority to deploy backwards.

    Production normally refuses any deploy whose revision is not a descendant
    of the revision already running: going backwards silently re-introduces
    every defect fixed in between, and after migrations have run it can put
    older code against a newer schema.

    This is deliberately NOT a boolean escape. It names the exact transition,
    so it authorizes one rollback rather than granting a standing permission:
    a document that does not match the observed running revision and the
    incoming staged revision is refused, and it cannot be reused for a later,
    different rollback.
    """

    from_revision: GitCommitSha
    to_revision: GitCommitSha
    change_reference: str
    reason: str

    def __post_init__(self) -> None:
        if self.from_revision == self.to_revision:
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.ROLLBACK_REVISIONS_IDENTICAL,
                field="to_revision",
                message="a rollback authorization must name two different revisions",
            )
        if not self.change_reference.strip():
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.ROLLBACK_CHANGE_REFERENCE_REQUIRED,
                field="change_reference",
                message="a rollback authorization requires a change reference",
            )
        if not self.reason.strip():
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.ROLLBACK_REASON_REQUIRED,
                field="reason",
                message="a rollback authorization requires a reason",
            )


@dataclass(frozen=True, slots=True)
class ProductionBootstrapAuthorization:
    """Explicit authority for the first deploy to an empty production host.

    Bootstrap is not inferred from a failed container inspection and is not a
    standing boolean permission. It names the exact production host and staged
    revision, so it can authorize only one confirmed container-absent deploy.
    """

    target_revision: GitCommitSha
    target_server: ProductionServerName
    change_reference: str
    reason: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.change_reference, str)
            or _CHANGE_REFERENCE.fullmatch(self.change_reference) is None
        ):
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.BOOTSTRAP_CHANGE_REFERENCE_REQUIRED,
                field="bootstrap change reference",
                message="bootstrap change reference must be a stable identifier",
            )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.BOOTSTRAP_REASON_REQUIRED,
                field="bootstrap reason",
                message="bootstrap reason is required",
            )


class ProductionEligibilityBlocker(str, Enum):
    """Stable reasons an artifact cannot be promoted to production."""

    SOURCE_CI_NOT_GREEN = "source_ci_not_green"
    STAGING_EVIDENCE_MISSING = "staging_evidence_missing"
    STAGING_NOT_ACCEPTED = "staging_not_accepted"
    STAGING_SOURCE_REVISION_MISMATCH = "staging_source_revision_mismatch"
    STAGING_SOURCE_TREE_MISMATCH = "staging_source_tree_mismatch"
    STAGING_IMAGE_DIGEST_MISMATCH = "staging_image_digest_mismatch"
    MAIN_AUTHORIZATION_MISSING = "main_authorization_missing"
    MAIN_CI_NOT_GREEN = "main_ci_not_green"
    MAIN_TREE_MISMATCH = "main_tree_mismatch"
    SOURCE_REVISION_NOT_IN_MAIN = "source_revision_not_in_main"
    RELEASE_REVISION_NOT_STAGED = "release_revision_not_staged"


@dataclass(frozen=True, slots=True)
class ProductionEligibilityOutcome:
    """Pure production-promotion decision over normalized evidence."""

    candidate: ReleaseCandidateRecord
    blockers: tuple[ProductionEligibilityBlocker, ...]

    @property
    def approved(self) -> bool:
        return not self.blockers


def evaluate_production_eligibility(
    candidate: ReleaseCandidateRecord,
) -> ProductionEligibilityOutcome:
    """Require one green staged digest, authorized from a main that contains it.

    The authorization identity (``authorization_main_revision``) and the
    artifact identity (``release_revision``) are checked separately: the
    artifact must match the staged candidate exactly, and must still be
    reachable from the authorizing ``main``.
    """

    blockers: list[ProductionEligibilityBlocker] = []
    artifact = candidate.artifact
    if artifact.source_ci_conclusion is not EvidenceConclusion.SUCCESS:
        blockers.append(ProductionEligibilityBlocker.SOURCE_CI_NOT_GREEN)

    staging = candidate.staging
    if staging is None:
        blockers.append(ProductionEligibilityBlocker.STAGING_EVIDENCE_MISSING)
    else:
        if staging.conclusion is not EvidenceConclusion.SUCCESS:
            blockers.append(ProductionEligibilityBlocker.STAGING_NOT_ACCEPTED)
        if staging.source_revision != artifact.source_revision:
            blockers.append(
                ProductionEligibilityBlocker.STAGING_SOURCE_REVISION_MISMATCH
            )
        if staging.source_tree != artifact.source_tree:
            blockers.append(ProductionEligibilityBlocker.STAGING_SOURCE_TREE_MISMATCH)
        if staging.image_digest != artifact.image_digest:
            blockers.append(ProductionEligibilityBlocker.STAGING_IMAGE_DIGEST_MISMATCH)

    main = candidate.main
    if main is None:
        blockers.append(ProductionEligibilityBlocker.MAIN_AUTHORIZATION_MISSING)
    else:
        if main.required_ci_conclusion is not EvidenceConclusion.SUCCESS:
            blockers.append(ProductionEligibilityBlocker.MAIN_CI_NOT_GREEN)
        if main.release_tree != artifact.source_tree:
            blockers.append(ProductionEligibilityBlocker.MAIN_TREE_MISMATCH)
        if not main.source_revision_is_ancestor:
            blockers.append(ProductionEligibilityBlocker.SOURCE_REVISION_NOT_IN_MAIN)
        # The authorized release must BE the staged candidate. Previously this
        # was expressed as "must differ from it", which was only ever true
        # because the candidate lived on another branch.
        if main.release_revision != artifact.source_revision:
            blockers.append(ProductionEligibilityBlocker.RELEASE_REVISION_NOT_STAGED)

    return ProductionEligibilityOutcome(
        candidate=candidate,
        blockers=tuple(blockers),
    )


class DeploymentTarget(str, Enum):
    """Exact environment class that owns deployment backup policy."""

    STAGING = "staging"
    PRODUCTION = "production"


class BackupMode(str, Enum):
    """Resolved database-backup behavior for one deployment."""

    REQUIRED = "required"
    SKIP_STAGING = "skip_staging"
    SKIP_PRODUCTION_HOTFIX = "skip_production_hotfix"


@dataclass(frozen=True, slots=True)
class MigrationGraphDigest:
    """Fingerprint of the complete migration graph embedded in an image."""

    value: str

    def __post_init__(self) -> None:
        _require_sha256_digest(
            self.value,
            field="migration graph digest",
            code=ReleaseContractErrorCode.INVALID_MIGRATION_GRAPH_DIGEST,
        )


@dataclass(frozen=True, slots=True)
class AlembicHeads:
    """Canonical exact set of Alembic revisions for an image or database."""

    revisions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.revisions or any(
            not isinstance(revision, str) for revision in self.revisions
        ):
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.INVALID_ALEMBIC_HEADS,
                field="Alembic heads",
                message="Alembic heads must be a non-empty set of revision identifiers",
            )
        normalized = tuple(sorted(set(self.revisions)))
        if any(
            _ALEMBIC_REVISION.fullmatch(revision) is None for revision in normalized
        ):
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.INVALID_ALEMBIC_HEADS,
                field="Alembic heads",
                message="Alembic heads must be a non-empty set of revision identifiers",
            )
        object.__setattr__(self, "revisions", normalized)


@dataclass(frozen=True, slots=True)
class MigrationStateEvidence:
    """Proof that a production hotfix introduces no migration change or work."""

    running_graph_digest: MigrationGraphDigest
    candidate_graph_digest: MigrationGraphDigest
    running_image_heads: AlembicHeads
    candidate_image_heads: AlembicHeads
    database_heads: AlembicHeads


@dataclass(frozen=True, slots=True)
class HotfixNoMigrationEvidence:
    """Explicit, attributable request to omit a production hotfix backup."""

    change_reference: str
    reason: str
    migration_state: MigrationStateEvidence

    def __post_init__(self) -> None:
        if (
            not isinstance(self.change_reference, str)
            or _CHANGE_REFERENCE.fullmatch(self.change_reference) is None
        ):
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.HOTFIX_CHANGE_REFERENCE_REQUIRED,
                field="hotfix change reference",
                message="hotfix change reference must be a stable identifier",
            )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.HOTFIX_REASON_REQUIRED,
                field="hotfix reason",
                message="hotfix reason is required",
            )


class BackupPolicyIssue(str, Enum):
    """Stable explanation for a rejected backup-skip request."""

    HOTFIX_EXCEPTION_NOT_ALLOWED_ON_STAGING = "hotfix_exception_not_allowed_on_staging"
    MIGRATION_GRAPH_CHANGED = "migration_graph_changed"
    IMAGE_HEADS_CHANGED = "image_heads_changed"
    DATABASE_NOT_AT_CANDIDATE_HEADS = "database_not_at_candidate_heads"


@dataclass(frozen=True, slots=True)
class BackupPolicyDecision:
    """Resolved backup behavior and any rejected-exception evidence."""

    target: DeploymentTarget
    mode: BackupMode
    issues: tuple[BackupPolicyIssue, ...] = ()

    @property
    def hotfix_exception_accepted(self) -> bool:
        return self.mode is BackupMode.SKIP_PRODUCTION_HOTFIX


def resolve_backup_policy(
    *,
    target: DeploymentTarget,
    hotfix: HotfixNoMigrationEvidence | None = None,
) -> BackupPolicyDecision:
    """Resolve environment-owned backup policy, failing safe to a backup."""

    if target is DeploymentTarget.STAGING:
        issues = (
            (BackupPolicyIssue.HOTFIX_EXCEPTION_NOT_ALLOWED_ON_STAGING,)
            if hotfix is not None
            else ()
        )
        return BackupPolicyDecision(
            target=target,
            mode=BackupMode.SKIP_STAGING,
            issues=issues,
        )

    if hotfix is None:
        return BackupPolicyDecision(target=target, mode=BackupMode.REQUIRED)

    state = hotfix.migration_state
    issues: list[BackupPolicyIssue] = []
    if state.running_graph_digest != state.candidate_graph_digest:
        issues.append(BackupPolicyIssue.MIGRATION_GRAPH_CHANGED)
    if state.running_image_heads != state.candidate_image_heads:
        issues.append(BackupPolicyIssue.IMAGE_HEADS_CHANGED)
    if state.database_heads != state.candidate_image_heads:
        issues.append(BackupPolicyIssue.DATABASE_NOT_AT_CANDIDATE_HEADS)

    if issues:
        return BackupPolicyDecision(
            target=target,
            mode=BackupMode.REQUIRED,
            issues=tuple(issues),
        )
    return BackupPolicyDecision(
        target=target,
        mode=BackupMode.SKIP_PRODUCTION_HOTFIX,
    )
