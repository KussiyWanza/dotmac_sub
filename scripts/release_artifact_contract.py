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


class ReleaseContractErrorCode(str, Enum):
    """Stable validation failures for release evidence adapters."""

    INVALID_GIT_COMMIT_SHA = "release_contract.invalid_git_commit_sha"
    INVALID_GIT_TREE_SHA = "release_contract.invalid_git_tree_sha"
    INVALID_IMAGE_DIGEST = "release_contract.invalid_image_digest"
    INVALID_WORKFLOW_RUN_ID = "release_contract.invalid_workflow_run_id"
    INVALID_DEPLOYMENT_ID = "release_contract.invalid_deployment_id"
    INVALID_MIGRATION_GRAPH_DIGEST = "release_contract.invalid_migration_graph_digest"
    INVALID_ALEMBIC_HEADS = "release_contract.invalid_alembic_heads"
    HOTFIX_CHANGE_REFERENCE_REQUIRED = (
        "release_contract.hotfix_change_reference_required"
    )
    HOTFIX_REASON_REQUIRED = "release_contract.hotfix_reason_required"


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
    """Content identity used to prove dev and main contain the same tree."""

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


class EvidenceConclusion(str, Enum):
    """Normalized conclusion for external workflow/deployment observations."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ReleaseArtifactEvidence:
    """Immutable output of the one application-image build."""

    source_revision: GitCommitSha
    source_tree: GitTreeSha
    image_digest: OCIImageDigest
    build_run_id: WorkflowRunId
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
    """Main-branch evidence authorizing an already-staged artifact."""

    authorization_run_id: WorkflowRunId
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
    """Require one green, staged digest and an identical authorized main tree."""

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
            or not self.change_reference.strip()
        ):
            raise ReleaseContractError(
                code=ReleaseContractErrorCode.HOTFIX_CHANGE_REFERENCE_REQUIRED,
                field="hotfix change reference",
                message="hotfix change reference is required",
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
