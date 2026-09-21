from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.release_artifact_contract import (
    GitCommitSha,
    GitTreeSha,
    OCIImageDigest,
    WorkflowRunId,
)
from scripts.release_candidate_reconciliation import (
    _DEFINITE_NEGATIVE_PATTERN,
    CandidateConflictReason,
    CandidateMode,
    CandidateProvenanceObservation,
    ExistingCandidateLabels,
    MalformedRegistryDigestError,
    PriorCandidateEvidence,
    RegistryReadAttempt,
    RegistryReadExhaustedError,
    RegistryReadOutcome,
    classify_registry_inspect_error,
    main,
    parse_extracted_digest,
    registry_read_found,
    resolve_candidate_provenance,
    resolve_registry_read,
)

REVISION = GitCommitSha("1" * 40)
OTHER_REVISION = GitCommitSha("2" * 40)
TREE = GitTreeSha("3" * 40)
OTHER_TREE = GitTreeSha("4" * 40)
DIGEST = OCIImageDigest("sha256:" + "5" * 64)
OTHER_DIGEST = OCIImageDigest("sha256:" + "6" * 64)
BUILD_RUN_ID = WorkflowRunId(100)


def _labels(
    *, revision: GitCommitSha = REVISION, source_tree: GitTreeSha = TREE
) -> ExistingCandidateLabels:
    return ExistingCandidateLabels(
        revision=revision, source_tree=source_tree, build_run_id=BUILD_RUN_ID
    )


def _prior(
    *,
    image_digest: OCIImageDigest = DIGEST,
    source_revision: GitCommitSha = REVISION,
    source_tree: GitTreeSha = TREE,
) -> PriorCandidateEvidence:
    return PriorCandidateEvidence(
        image_digest=image_digest,
        source_revision=source_revision,
        source_tree=source_tree,
    )


def test_state_a_neither_tag_nor_evidence_exists_builds() -> None:
    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=False,
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.BUILD
    assert decision.accepted
    assert decision.reasons == ()


def test_state_b_tag_and_agreeing_prior_evidence_reuse_without_rebuilding() -> None:
    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=_labels(),
        prior_evidence=_prior(),
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.REUSE
    assert decision.accepted
    assert decision.resolved_digest == DIGEST
    assert decision.resolved_build_run_id == BUILD_RUN_ID


def test_state_c_registry_exists_but_evidence_expired_still_reuses() -> None:
    """The actual incident: the record is gone (90-day retention), not wrong."""

    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=_labels(),
        prior_evidence=None,
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.REUSE
    assert decision.resolved_digest == DIGEST
    assert decision.resolved_build_run_id == BUILD_RUN_ID


def test_state_d_unreadable_labels_fail_closed() -> None:
    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=None,
        prior_evidence=None,
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.CONFLICT
    assert not decision.accepted
    assert decision.reasons == (CandidateConflictReason.LABELS_UNREADABLE,)


def test_state_d_label_revision_mismatch_fails_closed() -> None:
    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=_labels(revision=OTHER_REVISION),
        prior_evidence=None,
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.CONFLICT
    assert decision.reasons == (CandidateConflictReason.REVISION_MISMATCH,)


def test_state_d_label_tree_mismatch_fails_closed() -> None:
    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=_labels(source_tree=OTHER_TREE),
        prior_evidence=None,
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.CONFLICT
    assert decision.reasons == (CandidateConflictReason.TREE_MISMATCH,)


def test_state_d_prior_evidence_digest_disagrees_with_registry_fails_closed() -> None:
    """The tag was moved: registry digest and the recorded digest disagree."""

    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=_labels(),
        prior_evidence=_prior(image_digest=OTHER_DIGEST),
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.CONFLICT
    assert decision.reasons == (CandidateConflictReason.PRIOR_EVIDENCE_MISMATCH,)


def test_state_d_both_label_mismatches_are_reported_together() -> None:
    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=_labels(revision=OTHER_REVISION, source_tree=OTHER_TREE),
        prior_evidence=None,
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.reasons == (
        CandidateConflictReason.REVISION_MISMATCH,
        CandidateConflictReason.TREE_MISMATCH,
    )


def test_present_tag_without_digest_is_rejected_as_malformed_observation() -> None:
    with pytest.raises(ValueError, match="resolved digest"):
        CandidateProvenanceObservation(
            expected_source_revision=REVISION,
            expected_source_tree=TREE,
            tag_present=True,
            tag_digest=None,
        )


def test_absent_tag_carrying_registry_observations_is_rejected() -> None:
    with pytest.raises(ValueError, match="absent candidate tag"):
        CandidateProvenanceObservation(
            expected_source_revision=REVISION,
            expected_source_tree=TREE,
            tag_present=False,
            tag_digest=DIGEST,
        )


# --- Addition 1: digest-format validation --------------------------------


@pytest.mark.parametrize(
    "malformed",
    [
        "sha256:" + "5" * 63,  # truncated -- one hex character short
        "sha256:" + "5" * 65,  # one hex character too many
        "sha256:" + "G" * 64,  # not hex
        "sha256:" + "A" * 64,  # uppercase hex -- the pattern requires lowercase
        "sha512:" + "5" * 128,  # wrong algorithm prefix
        "5" * 64,  # missing the `sha256:` prefix entirely
        "",
        "sha256:",
    ],
)
def test_parse_extracted_digest_rejects_malformed_values(malformed: str) -> None:
    with pytest.raises(MalformedRegistryDigestError) as excinfo:
        parse_extracted_digest(malformed, source="candidate tag digest")

    # The raw extracted value is named in full, not redacted or dropped --
    # a digest is not a secret, and the whole point is to debug the read.
    assert "candidate tag digest" in str(excinfo.value)
    assert repr(malformed) in str(excinfo.value)
    assert excinfo.value.source == "candidate tag digest"
    assert excinfo.value.raw_value == malformed


def test_parse_extracted_digest_accepts_a_well_formed_digest() -> None:
    parsed = parse_extracted_digest(DIGEST.value, source="candidate tag digest")

    assert parsed == DIGEST


def test_registry_read_found_rejects_a_malformed_digest_before_building_an_attempt() -> (
    None
):
    """The near-miss: a well-formed FOUND attempt still requires validation."""

    with pytest.raises(MalformedRegistryDigestError):
        registry_read_found("sha256:not-hex", source="candidate tag digest")

    # Sensitivity: a genuinely well-formed digest is accepted and produces a
    # normal FOUND attempt, so the guard above is about the malformed value,
    # not about `registry_read_found` itself being broken.
    attempt = registry_read_found(DIGEST.value, source="candidate tag digest")
    assert attempt.outcome is RegistryReadOutcome.FOUND
    assert attempt.digest == DIGEST


def test_cli_resolve_rejects_a_malformed_tag_digest_through_the_full_flow(
    tmp_path: Path,
) -> None:
    """Addition 1, exercised through `main()` -- the real bash-to-Python seam.

    `--tag-digest` is exactly the value `release-candidate.yml`'s "Inspect
    existing candidate tag" step extracts from `docker buildx imagetools
    inspect` and passes straight through to this CLI. Before this change the
    malformed value still failed (via `OCIImageDigest`'s own validation) but
    with a generic message that dropped the offending value; this asserts the
    new, distinct, value-naming message instead.
    """

    output = tmp_path / "github_output"
    output.touch()
    malformed = "sha256:" + "z" * 64

    exit_code = main(
        [
            "resolve",
            "--expected-source-revision",
            REVISION.value,
            "--expected-source-tree",
            TREE.value,
            "--tag-present",
            "--tag-digest",
            malformed,
            "--github-output",
            str(output),
        ]
    )

    assert exit_code == 1
    # No output was written -- the malformed read never reached a decision.
    assert output.read_text() == ""


def test_cli_resolve_rejects_a_malformed_prior_evidence_digest(tmp_path: Path) -> None:
    """The prior-evidence digest is downloaded artifact input, not registry
    input, but it flows through the identical CLI seam and previously
    constructed `OCIImageDigest` OUTSIDE any `try` block -- a malformed value
    here used to crash with an uncaught `ReleaseContractError`, not fail
    closed with a clean message. This is the near-miss this fix also covers.
    """

    output = tmp_path / "github_output"
    output.touch()

    exit_code = main(
        [
            "resolve",
            "--expected-source-revision",
            REVISION.value,
            "--expected-source-tree",
            TREE.value,
            "--tag-present",
            "--tag-digest",
            DIGEST.value,
            "--label-revision",
            REVISION.value,
            "--label-tree",
            TREE.value,
            "--label-build-run-id",
            "100",
            "--prior-evidence-digest",
            "not-a-digest-at-all",
            "--prior-evidence-revision",
            REVISION.value,
            "--prior-evidence-tree",
            TREE.value,
            "--github-output",
            str(output),
        ]
    )

    assert exit_code == 1
    assert output.read_text() == ""


def test_cli_resolve_accepts_a_well_formed_tag_digest(tmp_path: Path) -> None:
    """Sensitivity control: a well-formed digest must still resolve normally."""

    output = tmp_path / "github_output"
    output.touch()

    exit_code = main(
        [
            "resolve",
            "--expected-source-revision",
            REVISION.value,
            "--expected-source-tree",
            TREE.value,
            "--tag-present",
            "--tag-digest",
            DIGEST.value,
            "--label-revision",
            REVISION.value,
            "--label-tree",
            TREE.value,
            "--label-build-run-id",
            "100",
            "--github-output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert "mode=reuse" in output.read_text()
    assert f"resolved_digest={DIGEST.value}" in output.read_text()


# --- Addition 2: behavioral registry stubs --------------------------------


class _ScriptedRegistryRead:
    """A fake registry read: a scripted sequence of outcomes, one per call.

    Mirrors the fake-over-a-typed-boundary shape this repository already
    uses for pure decision functions (see `resolve_candidate_provenance`'s
    own tests above): no real subprocess, no real registry, just a sequence
    of already-classified attempts standing in for what a real
    `docker buildx imagetools inspect` call, classified by
    `classify_registry_inspect_error`, would have produced.
    """

    def __init__(self, outcomes: list[RegistryReadAttempt]) -> None:
        self._outcomes = outcomes
        self.calls: list[int] = []

    def __call__(self, attempt: int) -> RegistryReadAttempt:
        self.calls.append(attempt)
        return self._outcomes[attempt - 1]


def _ambiguous(detail: str = "not yet visible") -> RegistryReadAttempt:
    return RegistryReadAttempt(outcome=RegistryReadOutcome.AMBIGUOUS, detail=detail)


def test_digest_visibility_recovers_after_a_simulated_propagation_delay() -> None:
    """GHCR-propagation-delay shape: "not found" (ambiguous) on early attempts,
    then a real digest becomes visible -- the exact 2026-09-09 incident shape,
    now proven to actually recover within budget.
    """

    check = _ScriptedRegistryRead(
        [
            _ambiguous("not yet visible"),
            _ambiguous("not yet visible"),
            _ambiguous("not yet visible"),
            registry_read_found(DIGEST.value, source="published digest"),
        ]
    )

    result = resolve_registry_read(check=check, max_attempts=8)

    assert result.outcome is RegistryReadOutcome.FOUND
    assert result.digest == DIGEST
    assert check.calls == [1, 2, 3, 4]


def test_digest_visibility_fails_closed_once_the_budget_is_exhausted() -> None:
    """Ambiguous on every attempt, past the budget: fails closed, not silently."""

    check = _ScriptedRegistryRead([_ambiguous("not yet visible") for _ in range(8)])

    with pytest.raises(RegistryReadExhaustedError) as excinfo:
        resolve_registry_read(check=check, max_attempts=8)

    assert check.calls == list(range(1, 9))
    assert excinfo.value.attempts == 8
    assert "8" in str(excinfo.value)
    assert "not yet visible" in str(excinfo.value)


def test_a_transient_error_is_retried_not_treated_as_a_definite_negative() -> None:
    """A 5xx/timeout-shaped error is AMBIGUOUS, never NOT_FOUND -- retried."""

    check = _ScriptedRegistryRead(
        [
            _ambiguous("500 Internal Server Error"),
            registry_read_found(DIGEST.value, source="published digest"),
        ]
    )

    result = resolve_registry_read(check=check, max_attempts=8)

    assert result.outcome is RegistryReadOutcome.FOUND
    assert check.calls == [1, 2]


def test_classify_registry_inspect_error_treats_definite_negative_distinctly() -> None:
    assert (
        classify_registry_inspect_error("manifest unknown")
        is RegistryReadOutcome.NOT_FOUND
    )
    assert (
        classify_registry_inspect_error("Error: No such manifest: ghcr.io/x@sha256:...")
        is RegistryReadOutcome.NOT_FOUND
    )
    # `docker buildx imagetools inspect`'s actual "not found" text (confirmed
    # live against GHCR 2026-09-10, incident behind #3061) -- a genuinely
    # nonexistent ref, distinct from the two shapes above.
    assert (
        classify_registry_inspect_error(
            "ERROR: ghcr.io/dotmac/app:candidate-abc123: not found"
        )
        is RegistryReadOutcome.NOT_FOUND
    )
    # Near-miss: a transient/unknown error must NOT be misclassified as the
    # definite negative -- it stays ambiguous and retryable.
    assert (
        classify_registry_inspect_error("500 Internal Server Error")
        is RegistryReadOutcome.AMBIGUOUS
    )
    assert (
        classify_registry_inspect_error("context deadline exceeded")
        is RegistryReadOutcome.AMBIGUOUS
    )
    # Near-miss: an unrelated error that merely contains the words "not
    # found" (e.g. a DNS failure) must not be misread as the well-known CLI
    # shape above -- the third alternative is anchored to the line start.
    assert (
        classify_registry_inspect_error(
            "dial tcp: lookup ghcr.io: no such host, host not found"
        )
        is RegistryReadOutcome.AMBIGUOUS
    )


def test_definite_negative_pattern_matches_release_candidate_workflow_exactly() -> None:
    """The classifier's pattern must never silently drift from the YAML's.

    `release-candidate.yml`'s "Inspect existing candidate tag" step used to
    recognize only `manifest unknown|no such manifest` as a definite
    negative; that gap caused two real production release-dispatch failures
    on 2026-09-10 (#3061) before the workflow's grep was fixed to also
    recognize `docker buildx imagetools inspect`'s actual
    `ERROR: <ref>: not found` text. This test reads the workflow file's exact
    grep pattern and asserts every one of its `|`-separated alternatives is
    also present in `classify_registry_inspect_error`'s own pattern, so the
    next person who wires this classifier into a live caller can't silently
    reintroduce that incident by having the two patterns diverge again.
    """

    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/release-candidate.yml"
    ).read_text(encoding="utf-8")
    match = re.search(r"grep -qiE '([^']+)'", workflow)
    assert match is not None, (
        "release-candidate.yml no longer has a `grep -qiE '...'` "
        "tag-existence check -- update this test to match its new shape"
    )
    yaml_alternatives = match.group(1).split("|")
    assert yaml_alternatives, "expected at least one alternative in the YAML pattern"

    python_pattern = _DEFINITE_NEGATIVE_PATTERN.pattern
    for alternative in yaml_alternatives:
        assert alternative in python_pattern, (
            f"release-candidate.yml's definite-negative pattern includes "
            f"{alternative!r}, but _DEFINITE_NEGATIVE_PATTERN does not -- "
            "the Python classifier has drifted from the workflow's grep"
        )


def test_a_definite_negative_on_tag_existence_is_not_retried_and_builds() -> None:
    """A real "manifest unknown" on the TAG-EXISTENCE check, specifically.

    Single attempt, never retried -- distinct from the digest-visibility
    check above, where the same "not found" shape is always ambiguous. The
    resolved outcome then flows into the real reconciliation decision and
    correctly resolves to `CandidateMode.build`.
    """

    check = _ScriptedRegistryRead(
        [
            RegistryReadAttempt(
                outcome=RegistryReadOutcome.NOT_FOUND, detail="manifest unknown"
            )
        ]
    )

    result = resolve_registry_read(check=check, max_attempts=3)

    assert result.outcome is RegistryReadOutcome.NOT_FOUND
    assert check.calls == [1]  # never retried

    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=(result.outcome is RegistryReadOutcome.FOUND),
    )
    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.BUILD


def test_malformed_digest_through_the_full_registry_read_and_reconciliation_flow() -> (
    None
):
    """Addition 1's guard, exercised at the registry-read integration seam.

    A "FOUND" attempt whose extracted digest is malformed must never reach
    `resolve_candidate_provenance` (and from there an evidence write or a
    comparison) -- it fails closed at the point of extraction, inside
    `resolve_registry_read`'s own `check` call.
    """

    def check(attempt: int) -> RegistryReadAttempt:
        return registry_read_found("sha256:" + "g" * 64, source="candidate tag digest")

    with pytest.raises(MalformedRegistryDigestError) as excinfo:
        resolve_registry_read(check=check, max_attempts=3)

    assert "candidate tag digest" in str(excinfo.value)
    assert "sha256:" + "g" * 64 in str(excinfo.value)
