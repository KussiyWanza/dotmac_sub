from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

# Every workflow that participates in selecting, staging, authorizing, or
# deploying a release. The trunk assertions below sweep this set rather than a
# hand-listed file, so a new release-chain workflow cannot reintroduce a second
# trunk unnoticed.
RELEASE_CHAIN = (
    ".github/workflows/ci.yml",
    ".github/workflows/mobile.yml",
    ".github/workflows/engineering-standards.yml",
    ".github/workflows/version-impact.yml",
    ".github/workflows/version-bump-pr.yml",
    ".github/workflows/version-tag.yml",
    ".github/workflows/release-candidate.yml",
    ".github/workflows/release-freeze-gate.yml",
    ".github/workflows/staging-deploy.yml",
    ".github/workflows/release-promotion.yml",
    ".github/workflows/production-deploy.yml",
)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_main_is_the_only_release_trunk_in_the_whole_chain() -> None:
    """The `dev` hop is retired; no release workflow may name a second trunk.

    Removing `dev-first-gate.yml` alone would not have removed the hop: the
    candidate build checked out `dev`, staging triggered on `dev`, and the
    promotion demanded candidate evidence recorded on `dev`. A grep-shaped
    assertion is the honest one here, because the failure mode is exactly a
    single surviving `dev` reference quietly splitting the release in two.
    """

    for path in RELEASE_CHAIN:
        workflow = _read(path)
        for line_number, line in enumerate(workflow.splitlines(), start=1):
            code = line.split("#", maxsplit=1)[0]
            # `/dev/null` is a shell device path and `development` is a word;
            # neither is a branch name. Everything else that spells `dev` is.
            code = re.sub(r"/dev/\w+", "", code).replace("development", "")
            assert "dev" not in code, (
                f"{path}:{line_number} still names a `dev` branch: {line.strip()}"
            )

    assert not (ROOT / ".github/workflows/dev-first-gate.yml").exists()


def test_pull_requests_into_main_run_the_required_ci_gates() -> None:
    for path in (".github/workflows/ci.yml", ".github/workflows/mobile.yml"):
        workflow = _read(path)
        # `main` stays covered; both batch prefixes join it so adoption and
        # consolidation branches run the required gates too.
        assert (
            "pull_request:\n"
            "    branches: [main, 'integration/**', 'consolidate/**']" in workflow
        )

    version_impact = _read(".github/workflows/version-impact.yml")
    assert "pull_request:" in version_impact
    assert "branches: [main]" in version_impact


def _extract_declared_active_statuses(workflow: str) -> set[str]:
    # Accepts both the array-literal form (`const activeStatuses = [...]`,
    # the fixed shape, which the queries are derived FROM) and the
    # `new Set([...])` form (the pre-fix shape, kept parseable here on
    # purpose so the comparison below fails on "declared != queried" rather
    # than merely on "workflow shape changed").
    match = re.search(
        r"const activeStatuses = (?:new Set\()?\[(.*?)\]\)?;", workflow, re.DOTALL
    )
    assert match, (
        "release-freeze-gate.yml must declare activeStatuses as a list of statuses"
    )
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _extract_queried_statuses(workflow: str) -> set[str]:
    """The statuses this workflow actually asks the GitHub API for.

    If every query is built by mapping over `activeStatuses` and passing the
    loop variable straight through (`status,` shorthand, no literal), the
    queried set IS the declared set by construction -- that structural
    derivation is the fix, and no per-call status can silently diverge from
    the declared list again. Anything else -- a literal `status: "..."` on
    each call, the shape of the original bug -- is read back from those
    literals instead, so a future hardcoded, diverging query is still caught.
    """

    derived = re.search(r"activeStatuses\.map\(", workflow) and re.search(
        r"listWorkflowRunsForRepo\(\{[^}]*?\n\s*status,\s*\n", workflow, re.DOTALL
    )
    if derived:
        return _extract_declared_active_statuses(workflow)

    calls = re.findall(r"listWorkflowRunsForRepo\(\{(.*?)\}\)", workflow, re.DOTALL)
    literal_statuses: set[str] = set()
    for call in calls:
        found = re.search(r'status:\s*"([^"]+)"', call)
        if found:
            literal_statuses.add(found.group(1))
    return literal_statuses


def test_release_freeze_gate_queries_every_status_it_declares_as_active() -> None:
    """A status declared but never queried is a dead literal, not a guard.

    `waiting` -- the status a production-gated run holds for its ENTIRE
    lifetime while parked on a manual environment approval -- was exactly
    such a dead literal: declared in `activeStatuses`, but the code only ever
    fetched `in_progress` and `queued` from the API, so the gate was blind to
    every approval-gated run without ever showing a failing assertion here.
    """

    workflow = _read(".github/workflows/release-freeze-gate.yml")
    declared = _extract_declared_active_statuses(workflow)
    queried = _extract_queried_statuses(workflow)

    assert declared, "expected at least one declared active status"
    assert declared == queried, (
        f"declared active statuses {sorted(declared)} do not match the "
        f"statuses actually queried from the GitHub API {sorted(queried)} -- "
        "a status present in one set but not the other is either dead "
        "configuration or an unmonitored blind spot"
    )


def test_release_freeze_gate_guarded_names_match_the_real_workflow_names() -> None:
    """A rename of a guarded workflow must not silently un-guard it."""

    workflow = _read(".github/workflows/release-freeze-gate.yml")
    guarded_match = re.search(
        r"const guarded = new Set\(\[(.*?)\]\);", workflow, re.DOTALL
    )
    assert guarded_match, (
        "release-freeze-gate.yml must declare guarded as a Set literal"
    )
    guarded_names = set(re.findall(r'"([^"]+)"', guarded_match.group(1)))

    real_names: set[str] = set()
    for path in (
        ".github/workflows/release-candidate.yml",
        ".github/workflows/staging-deploy.yml",
        ".github/workflows/release-promotion.yml",
        ".github/workflows/production-deploy.yml",
    ):
        content = _read(path)
        name_match = re.search(r"^name:\s*(.+)$", content, re.MULTILINE)
        assert name_match, f"{path} has no top-level name:"
        real_names.add(name_match.group(1).strip())

    assert guarded_names == real_names, (
        f"release-freeze-gate.yml guards {sorted(guarded_names)} but the real "
        f"release-chain workflow files are named {sorted(real_names)} -- a "
        "rename here silently un-guards a release-chain workflow"
    )


def test_release_freeze_gate_blocks_main_merges_during_deployment() -> None:
    workflow = _read(".github/workflows/release-freeze-gate.yml")
    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")
    guidance = " ".join(_read("AGENTS.md").split())

    assert yaml.safe_load(workflow)
    assert "name: Release Freeze Gate" in workflow
    assert "pull_request:\n    branches: [main]" in workflow
    assert "merge_group:" in workflow
    assert "actions: read" in workflow
    for guarded in (
        "Build release candidate once",
        "Deploy main to staging",
        "Promote staged digest for production",
        "Deploy authorized digest to production",
    ):
        assert guarded in workflow
        assert guarded in runbook
    assert "Release freeze is active" in workflow
    assert "approve, reject, or cancel the parked run" in workflow
    assert "queued, running, or awaiting approval" in runbook
    assert "gh pr" not in workflow
    assert "pulls" not in workflow
    assert "Open pull requests" not in workflow
    # On a single trunk the freeze is the only thing holding the release base
    # still, so the runbook must say that rather than merely restate the rule.
    assert "carries more weight on a single trunk" in runbook
    assert "does not inspect open pull requests" in runbook
    assert "does not block feature branch pushes" in guidance


def test_ghcr_isolated_to_the_pinned_genieacs_runtime() -> None:
    workflow = _read(".github/workflows/ghcr.yml")

    assert "branches: [main]" in workflow
    assert "docker/genieacs/**" in workflow
    assert "context: docker/genieacs" in workflow
    assert workflow.count("uses: docker/build-push-action@v6") == 1
    assert "context: .\n" not in workflow
    assert "type=raw,value=latest" not in workflow


def test_release_candidate_build_is_explicit_green_main_and_digest_evidenced() -> None:
    workflow = _read(".github/workflows/release-candidate.yml")
    dockerfile = _read("Dockerfile")

    assert yaml.safe_load(workflow)
    assert "on:\n  workflow_dispatch:" in workflow
    assert "on:\n  push:" not in workflow
    assert "candidate_sha:" in workflow
    assert "ref: main" in workflow
    assert "WORKFLOW_REF: ${{ github.ref }}" in workflow
    assert '"refs/heads/main"' in workflow
    assert 'const required = ["CI", "Mobile CI"]' in workflow
    assert "Refusing stale candidate" in workflow
    # A pre-existing `candidate-<sha>` tag no longer permanently refuses the
    # build (that was the 2026-09-09 incident: one flaky digest-visibility
    # failure after a successful push blocked every future rerun for that
    # commit). Ambiguous registry reads still fail closed with the original
    # message; only a genuine provenance disagreement is a new, distinct
    # fail-closed message.
    assert "Candidate image already exists" not in workflow
    assert (
        "Refusing candidate build: existing candidate image disagrees with "
        "this commit's provenance." in workflow
    )
    assert "Cannot safely determine whether" in workflow
    assert "runs-on: ubuntu-latest" in workflow
    assert "self-hosted" not in workflow
    assert workflow.count("uses: docker/build-push-action@v6") == 1
    assert "id: build" in workflow
    assert "${{ steps.build.outputs.digest }}" in workflow
    assert "python -m scripts.product_manifest emit" in dockerfile
    assert "--version-file VERSION" in dockerfile
    assert "product-manifest.json" in dockerfile
    assert 'candidate_ref="${REGISTRY}/${IMAGE_NAME}@${IMAGE_DIGEST}"' in workflow
    assert 'docker create "$candidate_ref"' in workflow
    assert ":/app/product-manifest.json" in workflow
    assert "-m scripts.product_manifest verify" in workflow
    assert "--product-manifest-digest" in workflow
    assert "path: |" in workflow
    assert "product-manifest.json" in workflow
    assert "python -m scripts.release_candidate_evidence write-candidate" in workflow
    assert "name: release-candidate-evidence" in workflow
    assert "retention-days: 90" in workflow

    # The build step alone is skipped in reuse mode; every other
    # accepted-mode step (visibility check, manifest extraction, evidence
    # write, upload) must run in EVERY accepted mode, or a reused candidate
    # would strand staging with a missing evidence artifact.
    assert workflow.count("if: steps.reconcile.outputs.mode == 'build'") == 1
    build_step_index = workflow.index("- name: Build and push candidate once")
    gate_index = workflow.index(
        "if: steps.reconcile.outputs.mode == 'build'", build_step_index
    )
    assert gate_index - build_step_index < 200
    for never_gated in (
        "- name: Verify published digest",
        "- name: Extract and verify the embedded product manifest",
        "- name: Write typed candidate evidence",
        "- name: Upload candidate evidence",
    ):
        step_index = workflow.index(never_gated)
        next_step_marker = workflow.find("\n      - name:", step_index + 1)
        step_body = workflow[step_index:next_step_marker]
        assert "if: steps.reconcile.outputs.mode" not in step_body

    # A registry-read retry loop, bounded and configurable by repository
    # variable, on both reads exposed to the same GHCR propagation delay that
    # caused the incident (the post-push digest check, and the product-
    # manifest pull immediately after it).
    assert "REGISTRY_VISIBILITY_MAX_ATTEMPTS" in workflow
    assert workflow.count("backoffs=(2 4 8 15 15 30 30)") == 2


def test_staging_deploy_is_disabled_and_pinned_to_the_staging_host() -> None:
    workflow = _read(".github/workflows/staging-deploy.yml")

    # Parsing catches malformed YAML independently of the text contract checks.
    assert yaml.safe_load(workflow)
    assert "name: Deploy main to staging" in workflow
    assert 'workflows: ["Build release candidate once"]' in workflow
    assert "branches: [main]" in workflow
    assert "vars.STAGING_AUTO_DEPLOY_ENABLED == 'true'" in workflow
    assert "github.event.workflow_run.event == 'workflow_dispatch'" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert (
        "github.event.workflow_run.head_repository.full_name == github.repository"
        in workflow
    )
    assert 'const required = ["CI", "Mobile CI"]' in workflow
    assert "Refusing stale staging deploy" in workflow
    assert "runs-on: [self-hosted, linux, x64, dotmac-sub-staging]" in workflow
    assert "environment: staging" in workflow
    assert 'expected_dir="/home/dotmac/deploy-worktrees/dotmac-sub-staging"' in workflow
    assert "/home/dotmac/projects/dotmac_sub" not in workflow
    assert 'test -e "$STAGING_DEPLOY_DIR/.git"' in workflow
    assert "rev-parse --is-inside-work-tree" in workflow
    assert "Check out exact candidate without moving deployment branches" in workflow
    assert 'git -C "$STAGING_DEPLOY_DIR" checkout --detach "$CANDIDATE_SHA"' in workflow
    assert 'git -C "$STAGING_DEPLOY_DIR" symbolic-ref -q HEAD' in workflow
    for forbidden_branch_mutation in (
        'git -C "$STAGING_DEPLOY_DIR" checkout main',
        'git -C "$STAGING_DEPLOY_DIR" merge --ff-only',
        'git -C "$STAGING_DEPLOY_DIR" branch --force',
        "git reset --hard",
    ):
        assert forbidden_branch_mutation not in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "python -m scripts.release_candidate_evidence verify-candidate" in workflow
    assert 'bash scripts/deploy_staging.sh "$IMAGE_DIGEST"' in workflow
    assert 'bash scripts/deploy.sh "$IMAGE_DIGEST"' not in workflow
    assert 'expected_image="ghcr.io/michaelayoade/dotmac_sub@$IMAGE_DIGEST"' in workflow
    assert "io.dotmac.release.source-tree" in workflow
    assert "io.dotmac.release.build-run" in workflow
    assert (
        "python -m scripts.release_candidate_evidence write-staging-acceptance"
        in workflow
    )
    assert (
        "name: staging-acceptance-${{ needs.verify.outputs.candidate_sha }}" in workflow
    )
    assert "10.120.121.20:8001:8001/tcp" in workflow
    assert "grep -qx celery-beat" in workflow

    deploy_job = workflow[
        workflow.index("  deploy:\n") : workflow.index("  record-acceptance:\n")
    ]
    for forbidden in (
        "pytest",
        "make test",
        "ruff",
        "mypy",
        "lint-imports",
        "bandit",
        "docker build",
    ):
        assert forbidden not in deploy_job
    acceptance_job = workflow[workflow.index("  record-acceptance:\n") :]
    assert "runs-on: ubuntu-latest" in acceptance_job

    staging_adapter = _read("scripts/deploy_staging.sh")
    assert 'require_exact_env_line "APP_ENV=staging"' in staging_adapter
    assert 'require_exact_env_line "SERVER_NAME=dotmac-sub-staging"' in staging_adapter
    assert (
        'require_exact_env_line "HEALTH_URL=http://10.120.121.20:8001/health"'
        in staging_adapter
    )
    assert "unset SKIP_BACKUP" in staging_adapter
    assert "export DEPLOY_BACKUP_MODE=skip_staging" in staging_adapter
    assert "export REQUIRE_PROXY_HANDOFF=0" in staging_adapter
    assert "export HEALTH_TIMEOUT_SECONDS=600" in staging_adapter
    assert 'exec bash "${ROOT_DIR}/scripts/deploy.sh" "$@"' in staging_adapter


def test_staging_remains_mandatory_as_a_digest_gate_not_a_branch_gate() -> None:
    """Retiring the branch hop must not have retired the staging requirement.

    This is the load-bearing property of the whole change. `dev` used to make
    staging unskippable by topology: nothing reached `main` without passing
    through the branch staging deployed from. With one trunk, the ONLY thing
    standing between a merge and production is the authorization step refusing
    a digest that carries no staging acceptance document — so that refusal has
    to be checked directly, not assumed from the pipeline's shape.
    """

    promotion = _read(".github/workflows/release-promotion.yml")
    evidence = _read("scripts/release_candidate_evidence.py")

    # The authorization cannot be dispatched without naming a real staging run,
    # and that run is verified to be the staging workflow, successful, and ours.
    assert "staging_deployment_run_id:" in promotion
    assert "required: true" in promotion
    assert '[stagingId, "Deploy main to staging", "main", "workflow_run"]' in promotion
    assert 'run.conclusion !== "success"' in promotion
    assert "run.head_repository?.full_name !== expectedRepository" in promotion

    # Exactly one acceptance document, downloaded from that exact run, is the
    # input to authorization. "Exactly one" matters: a zero-document path would
    # otherwise authorize an unstaged digest silently.
    assert "pattern: staging-acceptance-*" in promotion
    assert "run-id: ${{ inputs.staging_deployment_run_id }}" in promotion
    assert 'test "${#acceptance_files[@]}" -eq 1' in promotion
    assert "Expected exactly one staging acceptance document." in promotion
    assert "--staging " in promotion
    assert "--expected-staging-deployment-id" in promotion

    # And the verifier itself requires the staging record, so the requirement
    # does not live only in YAML that a future edit could drop.
    assert "authorize-production" in evidence
    assert "staging" in evidence

    # The production deploy accepts nothing but that typed authorization.
    production = _read(".github/workflows/production-deploy.yml")
    assert "verify-production" in production
    assert 'run.name !== "Promote staged digest for production"' in production


def test_agents_guidance_requires_staging_before_production() -> None:
    guidance = " ".join(_read("AGENTS.md").split())

    assert "`main` is the single release trunk" in guidance
    assert (
        "no long-lived `dev` branch and no branch-to-branch promotion hop" in guidance
    )
    assert "explicit one-time candidate build" in guidance
    assert "immutable candidate digest -> staging deployment and acceptance" in guidance
    assert "select the exact validated `origin/main` SHA" in guidance
    assert "open rolling version-bump pull request does not block" in guidance
    assert "Deploy that exact OCI" in guidance
    # The removal must be stated as a removed merge, not a removed gate.
    assert "Staging is still mandatory" in guidance
    assert "DIGEST gate rather than a branch gate" in guidance
    assert "refuses any digest without a matching staging acceptance" in guidance
    assert "must never receive the `latest` tag" in guidance
    assert "Require the exact candidate `main` commit's CI" in guidance
    assert "source pull requests do not edit `VERSION`" in guidance
    assert "automation owns the separate rolling" in guidance


def test_staging_promotion_runbook_records_activation_and_failure_contracts() -> None:
    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")

    assert "STAGING_AUTO_DEPLOY_ENABLED" in runbook
    assert "dotmac-sub-staging" in runbook
    assert (
        "STAGING_DEPLOY_DIR=/home/dotmac/deploy-worktrees/dotmac-sub-staging" in runbook
    )
    assert "used only by the staging" in runbook
    assert "never write into the deployment worktree" in runbook
    assert "leaves every local branch pointer unchanged" in runbook
    assert "detached `HEAD`" in runbook
    assert "scripts/deploy_staging.sh" in runbook
    assert "Build release candidate once" in runbook
    assert "release-candidate-evidence" in runbook
    assert "staging-acceptance-<source-sha>" in runbook
    assert "builds only on a GitHub-hosted runner" in runbook
    assert "### One-time workflow bootstrap" in runbook
    assert "workflow_dispatch" in runbook
    assert "Do not fabricate an" in runbook
    assert "Production requires a backup by default" in runbook
    assert "ten-minute health budget" in runbook
    assert "Do not edit `VERSION` in the source pull request" in runbook
    assert "An open rolling version-bump pull request does not block" in runbook
    assert "Open pull requests, including rolling version-bump pull requests" in runbook
    assert "A failed staging deployment never authorizes a production digest" in runbook


def test_runbook_explains_that_staging_moved_from_branch_gate_to_digest_gate() -> None:
    """The reader has to be told what did NOT change, or they will assume it did.

    "We removed the dev branch" reads, to the next person picking up a release,
    as "staging is optional now". The runbook has to close that reading in the
    same place it announces the removal.
    """

    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")
    header = runbook[: runbook.index("## Release sequence")]

    assert "There is no `dev` branch and no branch-to-branch promotion" in header
    assert "Staging did not become optional" in header
    assert "digest gate" in header
    assert "What was removed is the merge, not the proof." in header
    assert "no post-release branch reconciliation step" in runbook


def test_production_promotion_reuses_the_staged_digest_without_a_build() -> None:
    workflow = _read(".github/workflows/release-promotion.yml")

    assert yaml.safe_load(workflow)
    assert "on:\n  workflow_dispatch:" in workflow
    # The authorizing checkout is pinned to the dispatched commit, not the
    # moving branch tip -- see test_promotion_pins_the_authorizing_checkout_
    # to_the_dispatched_commit for the full pin/canary/reachability-reproof
    # assertions.
    assert "ref: ${{ github.sha }}" in workflow
    assert 'for (const workflowName of ["CI", "Mobile CI"])' in workflow
    assert "git merge-base --is-ancestor" in workflow
    assert "authorize-production" in workflow
    assert (
        "const expectedRepository = `${context.repo.owner}/${context.repo.repo}`;"
        in workflow
    )
    assert "run.head_repository?.full_name !== expectedRepository" in workflow
    assert "run.head_repository.full_name !== context.repo.repo" not in workflow
    assert "docker buildx imagetools create" in workflow
    assert "--prefer-index=false" in workflow
    assert "production-authorization-${{ steps.release.outputs.sha }}" in workflow
    assert "docker/build-push-action" not in workflow
    assert "docker build " not in workflow
    assert "self-hosted" not in workflow


def test_a_disagreeing_version_alias_is_preserved_during_promotion() -> None:
    """Published versions stay immutable while ``latest`` advances.

    A candidate selected before the rolling version-bump PR merges still carries
    the prior VERSION. Promotion must leave that existing version alias where it
    is and attach only ``latest`` to the staging-accepted digest.
    """

    workflow = _read(".github/workflows/release-promotion.yml")

    match = re.search(
        r'if \[ "\$existing_digest" = "\$IMAGE_DIGEST" \]; then\n'
        r".*?\n"
        r"            else\n"
        r"(.*?)"
        r"\n            fi",
        workflow,
        re.DOTALL,
    )
    assert match, "release-promotion.yml must preserve a mismatched version alias"
    mismatch_branch = match.group(1)
    assert "Version alias preserved" in mismatch_branch
    assert "advancing only 'latest'" in mismatch_branch
    assert 'alias_args+=(--tag "$version_ref")' not in mismatch_branch
    assert "exit 1" not in mismatch_branch


def test_promotion_separates_the_authorizing_main_from_the_staged_release() -> None:
    """Two identities, derived independently, never conflated.

    The authorizing `main` tip says WHO authorized; the staged revision says
    WHAT is deployed. Deriving the release SHA, its tree, or its VERSION from
    the checkout silently substitutes the former for the latter the moment
    anything lands on main -- which a version-bump PR does after every merge.
    """

    workflow = _read(".github/workflows/release-promotion.yml")

    # The artifact is named by the operator, not inferred from the tip.
    assert "staged_release_sha:" in workflow
    assert "authorization_main_sha=$(git rev-parse HEAD)" in workflow.replace('"', "")

    # Reachability replaces tip-equality: main may move, the staged commit must
    # still be on it. The old equality check must be gone, not merely relaxed.
    assert (
        'git merge-base --is-ancestor "$STAGED_SHA" "$authorization_main_sha"'
        in workflow
    )
    assert "is not an ancestor of main" in workflow
    assert "Refusing stale production promotion" not in workflow

    # The authorized artifact IS the staged candidate.
    assert 'test "$source_revision" = "$RELEASE_SHA"' in workflow
    assert 'test "$source_tree" = "$RELEASE_TREE"' in workflow

    # Tree and VERSION are read from the staged commit, never the checkout.
    assert 'staged_tree="$(git rev-parse "${STAGED_SHA}^{tree}")"' in workflow
    assert 'git show "${RELEASE_SHA}:VERSION"' in workflow
    assert "tr -d '[:space:]' < VERSION" not in workflow

    # Both identities reach the typed document.
    assert "--authorization-main-revision" in workflow
    assert "--source-revision-is-ancestor" in workflow


def test_promotion_pins_the_authorizing_checkout_to_the_dispatched_commit() -> None:
    """A merge landing during the human approval wait must not rebind authorization.

    `authorize-and-promote` carries a job-level `environment: production`
    approval gate, so every step in the job -- including its checkout -- runs
    AFTER the human approval, not before. Checking out `ref: main` therefore
    resolved to whatever main was AFTER the wait, not what was approved. This
    reproduced live in production on 2026-09-09: a promotion entered its
    approval wait, a PR merged during the wait, and the resulting
    authorization document was bound to a commit nobody had approved --
    caught one stage later by production-deploy.yml's verify step, after GHCR
    aliases had already been mutated. The fix pins the checkout to
    `github.sha` (the exact commit dispatched, frozen before the approval
    wait, matching what production-deploy.yml separately checks), asserts
    that pin holds as a canary, and re-proves reachability -- not equality --
    against main's live tip immediately before the first GHCR-mutating step.
    """

    workflow = _read(".github/workflows/release-promotion.yml")

    # The checkout no longer floats to whatever main is once the approval
    # gate releases the job. (The YAML `ref:` line, not a comment mentioning
    # the retired value -- this workflow's own comments cite `ref: main` by
    # name to explain why it changed.)
    assert "\n          ref: ${{ github.sha }}\n" in workflow
    assert "\n          ref: main\n" not in workflow

    # The pin-assertion canary: if github.sha and the checked-out HEAD ever
    # diverge, fail loudly and closed here, before any side effect.
    assert 'test "$authorization_main_sha" = "$GITHUB_SHA"' in workflow
    assert "is not the dispatched commit" in workflow

    # The reachability re-proof step: re-fetches main's live tip AFTER the
    # approval wait and re-checks ancestry (not equality, so a routine advance
    # of main during the wait stays informational, not a failure), and it must
    # run before anything that mutates GHCR.
    assert "git fetch --no-tags origin main" in workflow
    assert 'current_main_sha="$(git rev-parse FETCH_HEAD)"' in workflow
    assert 'git merge-base --is-ancestor "$STAGED_SHA" "$current_main_sha"' in workflow
    assert "no longer an ancestor of main's current tip" in workflow
    assert "::notice title=main advanced during the approval window" in workflow

    reproof_index = workflow.index("Re-prove staged reachability")
    buildx_index = workflow.index("Set up Buildx")
    ghcr_login_index = workflow.index("Log in to GHCR")
    imagetools_index = workflow.index("docker buildx imagetools create")
    assert reproof_index < buildx_index < ghcr_login_index < imagetools_index

    # The ordering assertion above is text-position only: a future
    # `continue-on-error: true` or a step-level `if:` on the authorizing job
    # would let a mutating step run even after a fail-closed check "failed",
    # while every assertion above still passes. Guard that directly.
    job_start = workflow.index("authorize-and-promote:")
    job_body = workflow[
        job_start : workflow.index("\njobs:", job_start)
        if "\njobs:" in workflow[job_start:]
        else len(workflow)
    ]
    assert "continue-on-error" not in job_body
    assert "\n        if:" not in job_body


def test_release_freeze_gate_documents_its_merge_time_limitation_accurately() -> None:
    """The freeze gate's known-limitation comment must name the real blockers.

    A prior draft of this comment blamed `ci.yml` and `version-bump-pr.yml`
    for blocking merge-queue adoption; `ci.yml` already carries `merge_group:`
    support (enforced by test_ci_workflow_contract.py) and is not the
    blocker. The actual pull_request-only required-context workflows are
    version-impact.yml and e2e-gate.yml. A future engineer evaluating merge
    queue adoption must not be misled by a stale/incorrect premise on a
    permanent, otherwise-unguarded documented limitation.
    """

    workflow = _read(".github/workflows/release-freeze-gate.yml")

    assert "KNOWN LIMITATION" in workflow
    assert "cached at the PR's" in workflow
    assert "version-impact.yml" in workflow
    assert "e2e-gate.yml" in workflow
    # The corrected comment must not re-blame ci.yml for the merge-queue gap.
    limitation = workflow[workflow.index("KNOWN LIMITATION") : workflow.index("on:")]
    assert "`ci.yml`\n# (" not in limitation


def test_production_deploy_binds_to_the_exact_digest_it_was_handed() -> None:
    """The on-host anti-rollback gate's own authorization read is bound to its inputs.

    `production-deploy.yml`'s `verify` job already binds
    `--expected-authorization-run-id`/`--expected-authorization-main-revision`
    (there is no `--expected-source-revision` flag anywhere in this repo --
    that CLI argument is dead surface). `deploy_production.sh`'s own
    `verify-production` call previously bound neither. In the automated path
    this closes a regression guard rather than a live gap today (DIGEST and
    AUTHORIZATION_RUN_ID both trace back to the same authorization.json the
    check reads), but it also protects the documented hand-invocation path on
    the host, where no such derivation guarantee exists.
    """

    adapter = _read("scripts/deploy_production.sh")

    assert "verify-production" in adapter
    assert '--expected-image-digest "${DIGEST}"' in adapter
    assert '--expected-authorization-run-id "${AUTHORIZATION_RUN_ID}"' in adapter
    # Bound before the gate's own verification call, not merely present
    # somewhere later in the script.
    gate = adapter.index("# --- Anti-rollback gate")
    verify_call_index = adapter.index('--expected-image-digest "${DIGEST}"')
    gate_end = adapter.index("# --- End anti-rollback gate")
    assert gate < verify_call_index < gate_end


def test_production_deploy_does_not_treat_head_sha_as_the_release_revision() -> None:
    """The authorization run's head_sha is the AUTHORIZER, not the artifact.

    production-deploy.yml checks out two different commits on purpose: the
    verifier from the authorizing main, and the application from the staged
    revision named inside the typed document. Collapsing them would deploy
    whatever main happened to be when the authorization ran.
    """

    workflow = _read(".github/workflows/production-deploy.yml")

    assert 'core.setOutput("authorization_main_sha", run.head_sha)' in workflow
    assert 'core.setOutput("release_sha", run.head_sha)' not in workflow
    assert "--expected-authorization-main-revision" in workflow
    # The application checkout comes from the document, not from the run.
    assert "ref: ${{ needs.verify.outputs.release_revision }}" in workflow


def test_production_refuses_a_non_forward_deploy_without_typed_authorization() -> None:
    """Anti-rollback runs before backup and migrations, and is not a boolean.

    A flag would authorize any rollback; this authorizes ONE transition, so a
    document kept from an earlier incident cannot wave through a later,
    different one. Divergent and unprovable histories take the same path as a
    known rollback -- they are not safer, so they must not be easier.
    """

    adapter = _read("scripts/deploy_production.sh")
    workflow = _read(".github/workflows/production-deploy.yml")

    assert "Anti-rollback gate" in adapter
    assert "org.opencontainers.image.revision" in adapter
    assert "--rollback-authorization" in adapter
    assert "verify-rollback-authorization" in adapter
    assert 'DIRECTION="backward"' in adapter
    assert 'DIRECTION="divergent"' in adapter
    assert 'DIRECTION="unknown"' in adapter
    assert "docker info" in adapter
    assert "production container inventory is unreadable" in adapter
    assert "has no org.opencontainers.image.revision label" in adapter
    assert "has a malformed org.opencontainers.image.revision label" in adapter
    assert "verify-bootstrap-authorization" in adapter
    # The gate must precede everything that touches the production host: the
    # hotfix migration-evidence collection pulls images and creates throwaway
    # containers, and deploy.sh owns the backup and `alembic upgrade`.
    gate = adapter.index("# --- Anti-rollback gate")
    gate_end = adapter.index("# --- End anti-rollback gate")
    assert gate < gate_end
    assert gate_end < adapter.index("docker pull")
    assert gate_end < adapter.index('bash "${REPO_DIR}/scripts/deploy.sh"')
    # Naming the exact transition is the authorization; there is no flag.
    for required in (
        "rollback_from_revision:",
        "rollback_to_revision:",
        "rollback_change_reference:",
        "rollback_reason:",
        "bootstrap_target_revision:",
        "bootstrap_change_reference:",
        "bootstrap_reason:",
    ):
        assert required in workflow
    assert "write-rollback-authorization" in workflow
    assert "write-bootstrap-authorization" in workflow


def test_production_deploy_requires_authorization_and_runs_no_test_suite() -> None:
    workflow = _read(".github/workflows/production-deploy.yml")
    deploy = _read("scripts/deploy.sh")
    adapter = _read("scripts/deploy_production.sh")

    assert yaml.safe_load(workflow)
    assert "PRODUCTION_DEPLOY_ENABLED" in workflow
    assert "target_server_name" in workflow
    assert "dotmac-sub-prod" in workflow
    assert "runs-on: [self-hosted, linux, x64, dotmac-sub-production]" in workflow
    assert "environment: production" in workflow
    assert "verify-production" in workflow
    assert (
        "const expectedRepository = `${context.repo.owner}/${context.repo.repo}`;"
        in workflow
    )
    assert "run.head_repository?.full_name !== expectedRepository" in workflow
    assert "run.head_repository.full_name !== context.repo.repo" not in workflow
    assert "bash scripts/deploy_production.sh" in workflow
    for forbidden in ("pytest", "make test", "ruff", "mypy", "lint-imports", "bandit"):
        assert forbidden not in workflow

    assert "production does not accept SKIP_BACKUP=1" in deploy
    assert "verify-production-decision" in deploy
    assert "deploy_production.sh" in adapter
    assert "write-production-decision" in adapter
    assert "SELECT version_num FROM alembic_version ORDER BY version_num" in adapter


def test_runbook_records_the_merge_method_per_pull_request_kind() -> None:
    """Batch branches keep a merge commit; everything else squashes.

    The old two-trunk rule (promotion and reconciliation as merge commits) is
    gone with the branch it protected, but the batch-branch case survives on its
    own reason: `migration_sequence_gate.py` reads the individual commits, and a
    squash destroys the ordering it inspects.
    """

    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")

    assert "## Merge methods" in runbook
    assert "| Feature or fix into `main` | Squash |" in runbook
    assert "| Rolling version bump into `main` | Squash |" in runbook
    assert (
        "| `integration/**` or `consolidate/**` batch into `main` | Merge commit |"
        in runbook
    )
    assert "migration_sequence_gate.py" in runbook
    # The retired rule stays explained so it is not reintroduced by habit, and
    # names the condition that would make it correct again.
    assert (
        "If a\nlong-lived branch is ever reintroduced, restore the merge-commit rule"
        in runbook
    )


def test_runbook_explains_why_main_requires_no_approving_review() -> None:
    """The rule was added and reverted within an hour on 2026-07-31.

    Single-account automation cannot satisfy a review requirement: bump and
    agent pull requests are authored by the only admin, nobody may self-approve,
    so every automated merge becomes an admin override. Recording why keeps it
    from being re-added, and names the precondition for reconsidering it.
    """

    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")

    assert "an approving review is not" in runbook
    assert "VERSION_BUMP_TOKEN" in runbook
    # The deadlock that makes GITHUB_TOKEN the wrong identity must stay stated.
    assert "GITHUB_TOKEN" in runbook
    assert "permanently unmergeable" in runbook


def test_runbook_keeps_the_delete_branch_on_merge_lesson() -> None:
    """The instance is gone; the rule is not.

    `delete_branch_on_merge` deleted `dev` outright on 2026-07-31 because a
    promotion's head was a long-lived branch. With one trunk every merged head
    is a topic branch, so the trap cannot fire today — which is exactly when a
    warning gets deleted and the next long-lived branch rediscovers it.
    """

    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")

    assert "delete_branch_on_merge" in runbook
    assert "allow_deletions" in runbook
    # gh's --delete-branch governs only the local branch; the remote deletion
    # comes from the repository setting. Mistaking the two is the whole trap.
    assert "--delete-branch" in runbook
    assert "not at its own former head" in runbook


def test_promotion_and_deploy_refuse_a_rerun_of_the_approval_gated_job() -> None:
    """A rerun after a partial failure must not re-enter the approval gate.

    `authorize-and-promote` moves GHCR aliases and `deploy` mutates the live
    production host, both after a human approval -- exactly the shape two
    other production-gated workflows in this repo
    (infrastructure-reconcile-apply.yml, legacy-image-pin-bootstrap-apply.yml)
    already refuse a second attempt of. Neither `release-promotion.yml` nor
    `production-deploy.yml` had this refusal; this proves it is present and
    runs before the first mutating action in each job.
    """

    promotion = _read(".github/workflows/release-promotion.yml")
    deploy_workflow = _read(".github/workflows/production-deploy.yml")

    refusal_marker = 'process.env.RUN_ATTEMPT !== "1"'
    assert refusal_marker in promotion
    assert refusal_marker in deploy_workflow
    assert "reruns are not admissible" in promotion
    assert "reruns are not admissible" in deploy_workflow

    # In release-promotion.yml the guard must be the FIRST step of
    # authorize-and-promote -- before its own checkout, and long before the
    # GHCR-mutating "Attach production aliases without rebuilding" step.
    promotion_job = promotion[promotion.index("  authorize-and-promote:\n") :]
    guard_index = promotion_job.index(refusal_marker)
    checkout_index = promotion_job.index("Checkout current protected main")
    alias_index = promotion_job.index("Attach production aliases without rebuilding")
    assert guard_index < checkout_index < alias_index

    # In production-deploy.yml the guard belongs to `deploy`, not `verify`:
    # `verify` carries no `environment:` gate and is read-only, so it stays
    # freely re-runnable. The guard must precede deploy's own checkout and the
    # actual host-mutating "Deploy authorized digest" step.
    deploy_job = deploy_workflow[deploy_workflow.index("  deploy:\n") :]
    guard_index = deploy_job.index(refusal_marker)
    checkout_index = deploy_job.index("Checkout authorized staged release")
    deploy_step_index = deploy_job.index("Deploy authorized digest")
    assert guard_index < checkout_index < deploy_step_index

    verify_job = deploy_workflow[
        deploy_workflow.index("  verify:\n") : deploy_workflow.index("  deploy:\n")
    ]
    assert refusal_marker not in verify_job
    assert "environment:" not in verify_job


def test_release_candidate_stays_reruns_admissible() -> None:
    """`release-candidate.yml` must NOT gain this guard.

    PR #3048 made a stalled candidate build safely resumable by re-running the
    same workflow (4-state build/reuse/conflict reconciliation) specifically
    so a rerun after a flaky post-push verification failure recovers instead
    of being permanently refused. Copy-pasting the run-attempt guard here
    would silently defeat that fix, so this is an explicit negative
    assertion, not merely an absence noticed by accident.
    """

    workflow = _read(".github/workflows/release-candidate.yml")

    assert 'process.env.RUN_ATTEMPT !== "1"' not in workflow
    assert "RUN_ATTEMPT" not in workflow
    assert "reruns are not admissible" not in workflow


def test_hotfixes_have_no_pipeline_shortcut_left() -> None:
    """`dev-first:override` was the escape hatch; removing the gate removes it.

    A retired label that is still described somewhere invites someone to reach
    for it during an incident and conclude the pipeline is broken when nothing
    honours it. The only surviving production exception is the backup one, and
    it shortens the deploy rather than the pipeline.
    """

    runbook = _read("docs/runbooks/STAGING_PROMOTION.md")
    guidance = _read("AGENTS.md")

    assert "## Hotfixes" in runbook
    assert "The `dev-first:override` label no longer exists" in runbook
    assert "It shortens the deploy, not the pipeline." in runbook
    assert "dev-first:override" not in guidance
    for path in RELEASE_CHAIN:
        assert "dev-first" not in _read(path)


def test_version_tag_refuses_an_existing_tag_pointing_at_the_wrong_commit() -> None:
    """A `vX.Y.Z` tag is the release-identity oracle (rule 27): its peeled
    commit must genuinely be the release it names. Re-running on the commit
    that already carries the tag stays a no-op; an existing tag pointing at
    ANY other commit must fail the workflow rather than exit 0."""

    workflow = _read(".github/workflows/version-tag.yml")

    assert yaml.safe_load(workflow)
    assert 'if git rev-parse "$TAG" >/dev/null 2>&1; then' in workflow

    match = re.search(
        r'if git rev-parse "\$TAG" >/dev/null 2>&1; then\n(.*?)\n          fi',
        workflow,
        re.DOTALL,
    )
    assert match, "version-tag.yml must guard the already-exists branch"
    existing_tag_branch = match.group(1)

    # Peeled commit comparison, not just tag existence.
    assert "$TAG^{commit}" in existing_tag_branch
    assert "git rev-parse HEAD" in existing_tag_branch
    assert "existing_commit" in existing_tag_branch
    assert "current_commit" in existing_tag_branch

    # Matching commit: idempotent no-op.
    assert 'if [ "$existing_commit" = "$current_commit" ]; then' in existing_tag_branch
    assert "exit 0" in existing_tag_branch

    # Mismatched commit: hard failure, not a silent exit 0.
    lines = existing_tag_branch.splitlines()
    match_block_start = next(
        i
        for i, line in enumerate(lines)
        if '$existing_commit" = "$current_commit"' in line
    )
    match_block_end = next(
        i
        for i, line in enumerate(lines[match_block_start:], match_block_start)
        if line.strip() == "fi"
    )
    after_match_block = "\n".join(lines[match_block_end + 1 :])
    assert "exit 1" in after_match_block
    assert "exit 0" not in after_match_block
