"""Reconcile a `candidate-<sha>` tag that may already exist in the registry.

`release-candidate.yml`'s "Refuse duplicate candidate build" step used to be a
pure existence check on the mutable `candidate-<sha>` tag: if the tag was
there, the workflow refused to run again for that commit, forever. That was
correct for the case it was written for (someone re-dispatching a build that
already succeeded) and wrong for the case that actually happened in
production: the build succeeded, pushed the image, and then the immediately
following digest-visibility check failed on a transient GHCR propagation
delay. The tag existed; the run still failed; nothing could ever rerun it
except a human deleting the GHCR package version by hand.

This module is the pure decision the workflow now makes instead. Every field
of a re-derived candidate evidence document -- except which run wrote it -- is
recoverable from the immutable image itself, so a resume needs zero state from
the failed run. Prior evidence, when it can still be found, is used only as a
cross-check constraint, never as the source of the new document.

Two supporting pieces of hardening live here alongside the reconciliation
decision itself:

- `parse_extracted_digest` / `MalformedRegistryDigestError`: a digest string
  read off the registry (or off a downloaded evidence document) is validated
  against the exact `sha256:` + 64-lowercase-hex shape this repository
  already requires everywhere else, and a malformed value fails closed with a
  distinct error naming the raw value -- rather than flowing on into a
  comparison, an evidence write, or an unrelated `docker pull` failure later.
- `RegistryReadOutcome` / `resolve_registry_read` / `classify_registry_inspect_error`:
  the generic, injectable shape of a bounded, classified registry-read retry
  -- FOUND and the definite-negative NOT_FOUND resolve immediately, AMBIGUOUS
  retries up to a budget and then fails closed -- mirroring the tag-existence
  check's own retry loop in `release-candidate.yml` so that shape is
  independently proven correct under simulated registry behavior (GHCR
  propagation delay, transient errors, exhaustion, a genuine absence) without
  needing a live registry.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from scripts.release_artifact_contract import (
    GitCommitSha,
    GitTreeSha,
    OCIImageDigest,
    ReleaseContractError,
    WorkflowRunId,
)


class MalformedRegistryDigestError(RuntimeError):
    """A digest string read off the registry is not OCI-digest-shaped.

    Distinct from `ReleaseContractError` (which only ever names the FIELD,
    e.g. "invalid OCI image digest") because a value extracted from
    `docker buildx imagetools inspect`'s output has nowhere else it could have
    come from malformed -- naming the field back to the operator is not
    enough to debug it. A digest is not a secret, so the raw extracted value
    is reported in full, never redacted.
    """

    def __init__(self, *, source: str, raw_value: str) -> None:
        super().__init__(
            f"{source} extracted a value that is not a valid OCI digest: {raw_value!r}"
        )
        self.source = source
        self.raw_value = raw_value


def parse_extracted_digest(raw_value: str, *, source: str) -> OCIImageDigest:
    """Validate a digest string extracted from the registry before any use.

    Reuses `OCIImageDigest`'s own `sha256:` + 64 lowercase hex check (this
    repository's one digest-shape pattern, also enforced in
    `release-candidate.yml`'s "Verify published digest" step) rather than a
    second regex, but raises a distinct, ungeneric error that names the
    offending source and the raw value -- the ambiguous-read case this
    reconciliation module already fails closed for elsewhere.
    """

    try:
        return OCIImageDigest(raw_value)
    except ReleaseContractError as exc:
        raise MalformedRegistryDigestError(source=source, raw_value=raw_value) from exc


class RegistryReadOutcome(str, Enum):
    """The classification of one registry-read attempt."""

    FOUND = "found"
    # A definite negative -- e.g. `manifest unknown` / `no such manifest` on a
    # tag-EXISTENCE check. Never retried: retrying "this commit was never
    # built" only spends the retry budget on a question that already has a
    # confident answer.
    NOT_FOUND = "not_found"
    # Everything else -- a transient error, a timeout, or (on a digest-
    # VISIBILITY check, which has no "not found" concept because the digest
    # was just pushed) a genuine propagation delay. Always retried, bounded.
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class RegistryReadAttempt:
    """One classified registry-read attempt."""

    outcome: RegistryReadOutcome
    digest: OCIImageDigest | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome is RegistryReadOutcome.FOUND and self.digest is None:
            raise ValueError("a FOUND registry read requires its resolved digest")
        if self.outcome is not RegistryReadOutcome.FOUND and self.digest is not None:
            raise ValueError("only a FOUND registry read carries a resolved digest")


def registry_read_found(raw_digest: str, *, source: str) -> RegistryReadAttempt:
    """Build a FOUND attempt, validating the extracted digest's shape first.

    A malformed digest is never allowed to reach a FOUND attempt silently --
    `parse_extracted_digest` raises `MalformedRegistryDigestError` instead of
    letting an invalid value flow into a comparison or an evidence write.
    """

    return RegistryReadAttempt(
        outcome=RegistryReadOutcome.FOUND,
        digest=parse_extracted_digest(raw_digest, source=source),
    )


_DEFINITE_NEGATIVE_PATTERN = re.compile(
    r"manifest unknown|no such manifest|^error: .*: not found$",
    re.IGNORECASE | re.MULTILINE,
)


def classify_registry_inspect_error(stderr: str) -> RegistryReadOutcome:
    """Classify one failed `docker buildx imagetools inspect` attempt.

    Mirrors the exact
    `grep -qiE 'manifest unknown|no such manifest|^error: .*: not found$'`
    pattern `release-candidate.yml`'s tag-existence check uses to resolve a
    definite negative on the first attempt; everything else is ambiguous and
    safe to retry. The third alternative was added 2026-09-10 after
    `docker buildx imagetools inspect` (used here, not the older `docker
    manifest inspect`) was observed live against GHCR reporting a genuinely
    missing ref as `ERROR: <ref>: not found` -- a shape the first two
    alternatives don't cover -- and is anchored to the line start so an
    unrelated ambiguous error that merely contains the words "not found"
    (e.g. a DNS failure) is not misread as this specific, well-known CLI
    message.
    """

    if _DEFINITE_NEGATIVE_PATTERN.search(stderr):
        return RegistryReadOutcome.NOT_FOUND
    return RegistryReadOutcome.AMBIGUOUS


class RegistryReadExhaustedError(RuntimeError):
    """Every attempt within the configured budget stayed ambiguous."""

    def __init__(self, *, attempts: int, last_detail: str) -> None:
        super().__init__(
            f"cannot safely determine registry state after {attempts} "
            f"attempt(s): {last_detail}"
        )
        self.attempts = attempts
        self.last_detail = last_detail


def resolve_registry_read(
    *,
    check: Callable[[int], RegistryReadAttempt],
    max_attempts: int,
    sleep: Callable[[int], None] = lambda attempt: None,
) -> RegistryReadAttempt:
    """Resolve one registry read via a bounded, classified retry loop.

    `check(attempt)` performs and classifies one attempt (1-indexed). FOUND
    and NOT_FOUND resolve immediately, on whichever attempt first returns
    them -- NOT_FOUND is a definite negative and is never retried, matching
    the tag-existence check's short-circuit. AMBIGUOUS is retried up to
    `max_attempts`, with `sleep(attempt)` called between attempts (a no-op by
    default, so tests never actually sleep); exhausting the budget without a
    resolved outcome raises `RegistryReadExhaustedError` naming the attempt
    count and the last detail, rather than returning a false negative.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    last: RegistryReadAttempt | None = None
    for attempt in range(1, max_attempts + 1):
        result = check(attempt)
        if result.outcome is not RegistryReadOutcome.AMBIGUOUS:
            return result
        last = result
        if attempt < max_attempts:
            sleep(attempt)
    assert last is not None  # max_attempts >= 1 guarantees at least one iteration
    raise RegistryReadExhaustedError(attempts=max_attempts, last_detail=last.detail)


class CandidateMode(str, Enum):
    """The resolved action for one `candidate-<sha>` tag."""

    BUILD = "build"
    REUSE = "reuse"
    CONFLICT = "conflict"


class CandidateConflictReason(str, Enum):
    """Stable reasons an existing candidate tag cannot be safely reused."""

    LABELS_UNREADABLE = "labels_unreadable"
    REVISION_MISMATCH = "revision_mismatch"
    TREE_MISMATCH = "tree_mismatch"
    PRIOR_EVIDENCE_MISMATCH = "prior_evidence_mismatch"


@dataclass(frozen=True, slots=True)
class ExistingCandidateLabels:
    """Provenance labels read directly off an already-published candidate image."""

    revision: GitCommitSha
    source_tree: GitTreeSha
    build_run_id: WorkflowRunId


@dataclass(frozen=True, slots=True)
class PriorCandidateEvidence:
    """A previously uploaded `candidate.json` found for this same commit.

    Absence is never proof the candidate was never verified -- the evidence
    artifact carries a 90-day retention window, so it can simply have expired.
    Presence is used only as an extra cross-check against the registry.
    """

    image_digest: OCIImageDigest
    source_revision: GitCommitSha
    source_tree: GitTreeSha


@dataclass(frozen=True, slots=True)
class CandidateProvenanceObservation:
    """Everything independently observable about one candidate tag right now."""

    expected_source_revision: GitCommitSha
    expected_source_tree: GitTreeSha
    tag_present: bool
    tag_digest: OCIImageDigest | None = None
    labels: ExistingCandidateLabels | None = None
    prior_evidence: PriorCandidateEvidence | None = None

    def __post_init__(self) -> None:
        if self.tag_present and self.tag_digest is None:
            raise ValueError("a present candidate tag requires its resolved digest")
        if not self.tag_present and (
            self.tag_digest is not None
            or self.labels is not None
            or self.prior_evidence is not None
        ):
            raise ValueError(
                "an absent candidate tag carries no further registry observations"
            )


@dataclass(frozen=True, slots=True)
class CandidateReconciliationDecision:
    """The resolved mode plus, on reuse, what to reuse."""

    mode: CandidateMode
    reasons: tuple[CandidateConflictReason, ...] = ()
    resolved_digest: OCIImageDigest | None = None
    resolved_build_run_id: WorkflowRunId | None = None

    @property
    def accepted(self) -> bool:
        return self.mode is not CandidateMode.CONFLICT


def resolve_candidate_provenance(
    observation: CandidateProvenanceObservation,
) -> CandidateReconciliationDecision:
    """Resolve one candidate tag into build / reuse / conflict.

    - Neither tag nor prior evidence exists: BUILD (state A, unchanged).
    - The tag exists, its labels agree with this commit, and any prior
      evidence found agrees with the registry: REUSE without rebuilding --
      whether or not prior evidence was found (states B and C are the same
      decision; C is simply B with expired/missing evidence).
    - Labels are unreadable, disagree with this commit, or prior evidence
      disagrees with the registry: CONFLICT (state D), fail closed.
    """

    if not observation.tag_present:
        return CandidateReconciliationDecision(mode=CandidateMode.BUILD)

    if observation.labels is None:
        return CandidateReconciliationDecision(
            mode=CandidateMode.CONFLICT,
            reasons=(CandidateConflictReason.LABELS_UNREADABLE,),
        )

    reasons: list[CandidateConflictReason] = []
    labels = observation.labels
    if labels.revision != observation.expected_source_revision:
        reasons.append(CandidateConflictReason.REVISION_MISMATCH)
    if labels.source_tree != observation.expected_source_tree:
        reasons.append(CandidateConflictReason.TREE_MISMATCH)

    prior = observation.prior_evidence
    if prior is not None and (
        prior.image_digest != observation.tag_digest
        or prior.source_revision != observation.expected_source_revision
        or prior.source_tree != observation.expected_source_tree
    ):
        reasons.append(CandidateConflictReason.PRIOR_EVIDENCE_MISMATCH)

    if reasons:
        return CandidateReconciliationDecision(
            mode=CandidateMode.CONFLICT,
            reasons=tuple(reasons),
        )

    return CandidateReconciliationDecision(
        mode=CandidateMode.REUSE,
        resolved_digest=observation.tag_digest,
        resolved_build_run_id=labels.build_run_id,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("resolve")
    resolve.add_argument("--expected-source-revision", required=True)
    resolve.add_argument("--expected-source-tree", required=True)
    resolve.add_argument("--tag-present", action="store_true")
    resolve.add_argument("--tag-digest")
    resolve.add_argument("--label-revision")
    resolve.add_argument("--label-tree")
    resolve.add_argument("--label-build-run-id", type=int)
    resolve.add_argument("--prior-evidence-digest")
    resolve.add_argument("--prior-evidence-revision")
    resolve.add_argument("--prior-evidence-tree")
    resolve.add_argument("--github-output", required=True, type=Path)
    return parser


def _append_github_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        labels: ExistingCandidateLabels | None = None
        if args.label_revision and args.label_tree and args.label_build_run_id:
            labels = ExistingCandidateLabels(
                revision=GitCommitSha(args.label_revision),
                source_tree=GitTreeSha(args.label_tree),
                build_run_id=WorkflowRunId(args.label_build_run_id),
            )

        # `--prior-evidence-digest` came from a downloaded `candidate.json`
        # artifact and `--tag-digest` from the registry, via
        # `docker buildx imagetools inspect`. Both are validated through
        # `parse_extracted_digest`, not a bare `OCIImageDigest(...)` call, so
        # a malformed value fails closed HERE, inside this `try`, with a
        # message naming the offending value -- not as an uncaught exception
        # (this construction previously ran before the `try` below existed
        # for `--prior-evidence-digest`) and not as a generic "invalid OCI
        # image digest" that drops what was actually read.
        prior_evidence: PriorCandidateEvidence | None = None
        if (
            args.prior_evidence_digest
            and args.prior_evidence_revision
            and args.prior_evidence_tree
        ):
            prior_evidence = PriorCandidateEvidence(
                image_digest=parse_extracted_digest(
                    args.prior_evidence_digest, source="prior candidate evidence digest"
                ),
                source_revision=GitCommitSha(args.prior_evidence_revision),
                source_tree=GitTreeSha(args.prior_evidence_tree),
            )

        observation = CandidateProvenanceObservation(
            expected_source_revision=GitCommitSha(args.expected_source_revision),
            expected_source_tree=GitTreeSha(args.expected_source_tree),
            tag_present=args.tag_present,
            tag_digest=(
                parse_extracted_digest(args.tag_digest, source="candidate tag digest")
                if args.tag_digest
                else None
            ),
            labels=labels,
            prior_evidence=prior_evidence,
        )
    except (MalformedRegistryDigestError, ReleaseContractError, ValueError) as exc:
        print(f"candidate provenance observation is invalid: {exc}")
        return 1

    decision = resolve_candidate_provenance(observation)
    if not decision.accepted:
        print(
            "candidate provenance conflict: "
            + ", ".join(reason.value for reason in decision.reasons)
        )
        return 1

    outputs = {"mode": decision.mode.value}
    if decision.resolved_digest is not None:
        outputs["resolved_digest"] = decision.resolved_digest.value
    if decision.resolved_build_run_id is not None:
        outputs["resolved_build_run_id"] = str(decision.resolved_build_run_id.value)
    _append_github_outputs(args.github_output, outputs)
    print(decision.mode.value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
