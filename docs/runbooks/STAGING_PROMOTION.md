# Dev-to-staging-to-main promotion

This repository promotes release changes through `dev` and the staging
environment before `main`. A feature branch must not bypass this path.

**Open your pull request against `dev`, not `main`.** The `Dev-First Gate`
check (`.github/workflows/dev-first-gate.yml`) fails any pull request targeting
`main` whose head is not `dev`, `agent/promote-*`, `agent/reconcile-*`,
`promote/*`, or `reconcile/*`. Retarget with `gh pr edit <number> --base dev`.

For a production incident that genuinely cannot wait for the staging cycle, the
`dev-first:override` label lets a pull request through. It is deliberately
visible: the label stays on the pull request and the run records a warning.
Overridden work reaches production without staging having run it, so reconcile
`main` back into `dev` afterwards.

## Promotion sequence

1. Open the feature pull request against `dev` with the appropriate
   `version:major`, `version:minor`, `version:patch`, or `version:none` label.
   Do not edit `VERSION` in the source pull request; the version-bump workflow
   owns the separate rolling bump pull request after merge.
2. Require CI, Mobile CI, and Version Impact to pass before merging.
3. Merge into `dev` and let the rolling version-bump pull request be created
   when the version-impact automation requires one.
4. Select the exact `origin/dev` SHA intended for deployment and require CI and
   Mobile CI to pass on that exact commit.
   An open rolling version-bump pull request does not block
   a digest-bound candidate for an already-selected source SHA; it governs
   semver metadata and aliases, not deployment authority.
5. Dispatch `Build release candidate once` on `dev`, supplying that full dev
   SHA as `candidate_sha`. The workflow refuses a stale SHA or non-green source,
   builds the application once on GitHub, and records its immutable OCI digest.
6. Let `Deploy dev to staging` deploy that exact digest, then complete staging
   acceptance against `http://10.120.121.20:8001`. **That acceptance covers
   application behaviour only — it does not exercise network equipment.** See
   "What staging acceptance does not cover" below.
7. Promote the accepted dev tree to `main` without unrelated changes. The
   promotion PR uses `version:none` with a body explaining that the version was
   already established and validated on dev. **Merge it with a merge commit,
   never a squash** — see "Merge methods" below.
8. Require CI and Mobile CI on the exact resulting `main` commit. Dispatch
   `Promote staged digest for production` on `main` with the candidate build
   run, staging run, and full main SHA. It proves tree equality and ancestry,
   records typed authorization, and attaches version and `latest` aliases to
   the staged digest without rebuilding.
9. Synchronize `main` back into `dev` through a zero-file pull request and merge
   it with a merge commit. Branch protection rejects direct ref updates even
   when they are fast-forwards; see "Synchronize `dev` after promotion" below.
10. Dispatch `Deploy authorized digest to production` only after Michael names
    `dotmac-sub-prod`, supplies the authorization run ID, and the protected
    production environment approves it. The default path takes a backup.

Invoke the candidate workflow only for the exact source SHA intended for this
release. Do not build and stage every intermediate feature merge. The isolated
GHCR workflow publishes only the pinned GenieACS runtime; it never builds an
application image. If the rolling version-bump pull request remains open, the
production promotion still authorizes and deploys the immutable digest; the
existing semver tag is not moved when it already points at an older digest, and
only `latest` is advanced to the authorized production digest.

## Release Freeze

Once release deployment is in flight, `dev` is frozen for merges until the
candidate is deployed to production or the release is explicitly abandoned. The
freeze is intentionally narrow: feature branch pushes, pull request creation,
and pull request updates continue, but no pull request should merge into `dev`
while `Build release candidate once`, `Deploy dev to staging`,
`Promote staged digest for production`, or `Deploy authorized digest to
production` is queued or running.

The `Release Freeze Gate` workflow is the required pull-request check that
enforces this boundary on `dev`. It reads active GitHub Actions runs and fails
only when one of those release-control workflows is queued or in progress. It
does not inspect open pull requests and does not reinterpret the selected
candidate; the candidate SHA and OCI digest remain the deployment authority.

### One-time workflow bootstrap

GitHub accepts `workflow_dispatch` only after the workflow file exists on the
default branch. Therefore, the promotion that first introduces
`release-candidate.yml` cannot use that workflow to stage itself. Use the
previously active dev-image staging path for that one promotion, with all of its
existing CI, staging, and approval gates. Once the change reaches `main`, every
later release uses the explicit candidate workflow. Do not fabricate an
evidence artifact or bypass staging to shorten the bootstrap.

## What staging acceptance does not cover

Staging cannot execute any live OLT operation. Its OpenBao instance is reachable
but unseeded — `/admin/system/secrets` reports **0 secret paths, 0 fields** —
while the staging database carries `bao://` credential references inherited from
a production copy. Every reference therefore 404s:

```
Autofind query failed: Failed to resolve credential secret reference:
404: OpenBao secret not found
```

Confirmed 2026-08-08 against BOI and Gudu, on staging `v7.141.6`. Resolution
happens in `credential_crypto.decrypt_credential` -> `secrets.resolve_openbao_ref`,
inside `_open_shell` and before any SSH session opens, so no application change
compensates and no amount of staging soak exercises device behaviour.

**Consequence.** Changes to OLT/ONT device interaction — Huawei CLI transport,
command construction, response classification, ONT authorization, service ports,
TR-069 binding — reach production having been exercised against a shelf at **no
point** in the pipeline, because CI cannot reach one either. Do not read a green
staging acceptance as evidence that device-facing behaviour works.

For such a change, either arrange verification another way before promoting, or
promote deliberately with a named post-deploy check and record that decision in
the promotion pull request.

`scripts/setup/openbao_init.sh` seeds project-level secrets from environment
variables. It does **not** create per-OLT credential paths and will not fix this.

Tracked remediation: provision read-only accounts on the Huawei shelves and seed
those at the referenced paths, giving staging real read acceptance (autofind,
status, inventory, config readback) with no ability to write to production fibre
plant. Seeding staging with the existing full-access credentials would let a
staging bug mutate live plant, and is not recommended.

## Merge methods

The method is not a style preference here; it decides whether the two branches
stay related.

| Pull request | Method |
|---|---|
| Feature or fix into `dev` | Squash |
| Promotion, `dev` into `main` | **Merge commit** |
| Reconciliation, `main` into `dev` | **Merge commit** |

A squash-merged promotion lands on `main` as a single new commit that `dev` has
never seen. `main` stops being an ancestor of `dev` the moment it merges, and
the next promotion opened with `head=dev` reports `CONFLICTING` — typically on
`VERSION`, `CHANGELOG.md`, `pyproject.toml`, `package.json`,
`package-lock.json`, and the three mobile version files, because both branches
edited the same lines to different values from a common older base. Squashing
the reconciliation does not repair it either: squash discards the record that
main's value was considered and deliberately rejected, so the same conflict
returns next release.

A merge commit keeps `main` a real ancestor of `dev`, so promotions
fast-forward cleanly and no reconciliation is owed.

Check ancestry before opening a promotion:

```
git merge-base --is-ancestor origin/main origin/dev && echo ok
```

If that fails, the branches have already diverged. Do not open the promotion
from `dev`. Reconcile first, as described in the next section.

### Branch protection: green is required, an approving review is not

`dev` requires its status checks to pass and **zero** approving reviews. That is
deliberate, not an oversight. Do not add a review requirement without first
satisfying the precondition below.

Release automation here is single-account. The rolling version-bump pull request
is generated on nearly every merge, agent-authored pull requests merge on green
throughout the day, and **all of them are authored by the same account**, because
the automation pushes with that account's `VERSION_BUMP_TOKEN`. GitHub does not
permit self-approval, and that account is the only admin. So the only person who
could satisfy a review requirement is the only person who could bypass it, and
every automated merge would become an admin override.

That is worse than having no gate. It normalises the bypass and makes the audit
trail dishonest, because routine traffic then looks like a deliberate exception.
This was demonstrated in practice on 2026-07-31: the requirement was added and
blocked a fully green bump pull request, stalling a waiting deployment, and was
reverted within the hour.

**Precondition for re-enabling.** Give automation its own identity — point
`VERSION_BUMP_TOKEN` at a GitHub App installation token or a dedicated bot
account, so its pull requests are authored by that identity and a human can
approve them in one click. Never point it at `GITHUB_TOKEN`: GitHub raises no
workflow runs for events made with it, so required checks never report and the
pull request becomes permanently unmergeable. `version-bump-pr.yml` already
validates the token and warns rather than falling back silently.

Once automation is a separate identity, a review requirement becomes meaningful
rather than ceremonial, and can be reconsidered.

### Synchronize `dev` after a promotion merges

A merge-commit promotion creates a commit **on `main` only**. `main` becomes
merge(`main`, `dev`), while `dev` stays at the commit that was promoted — one of
that merge's own parents. So the moment the promotion lands, `main` is no longer
an ancestor of `dev`, and the ancestry check above starts failing again.

This is not content divergence. `dev` is simply behind by the merge commit and
its head is still an ancestor of `main`. Close it with a zero-file
reconciliation pull request from a short-lived branch at `main` into `dev`.
GitHub branch protection requires every `dev` update to arrive through a pull
request and rejects even a non-force fast-forward ref update:

```
git fetch origin main dev
git switch -c agent/reconcile-main-to-dev origin/main
git push origin agent/reconcile-main-to-dev
gh pr create --base dev --head agent/reconcile-main-to-dev \
  --title "chore: reconcile main into dev" --label version:none
```

Verify both invariants afterwards:

```
git merge-base --is-ancestor origin/main origin/dev && echo ok
gh api repos/michaelayoade/dotmac_sub/compare/main...dev \
  --jq '"\(.status) ahead=\(.ahead_by) behind=\(.behind_by)"'   # identical 0 0
```

Skip this and the next promotion re-enters the reconciliation path the merge
methods above exist to eliminate — the branches drift apart one merge commit per
release, which is slower to notice than a squash-driven divergence because the
trees stay identical while only the history separates.

### Merging a promotion deletes `dev` unless `dev` is protected

The repository sets `delete_branch_on_merge: true`, which deletes the **head**
branch of every merged pull request. That is what you want for topic branches.
A promotion PR's head is `dev`, so merging one deletes `dev`.

Passing or omitting `--delete-branch` on `gh pr merge` makes no difference. That
flag only controls whether the CLI removes your *local* branch; the remote
deletion comes from the repository setting.

`dev` is protected against deletion and force-pushes, and GitHub silently skips
auto-deletion for protected branches, so this is handled. Do not remove that
protection. If a long-lived branch is ever added — a release or maintenance
branch that pull requests will be opened *from* — protect it the same way
before the first such pull request merges:

```
gh api -X PUT repos/michaelayoade/dotmac_sub/branches/<branch>/protection --input - <<'JSON'
{"required_status_checks":null,"enforce_admins":false,
 "required_pull_request_reviews":null,"restrictions":null,
 "allow_deletions":false,"allow_force_pushes":false}
JSON
```

If it does happen, nothing is lost: the deleted branch's head is a parent of the
new merge commit, so every commit is still reachable. Recreate it at the base
branch's **new head**, not at its own former head — the former head would leave
the branches diverged again, while the merge commit makes them identical, which
is the correct post-promotion state:

```
git push origin <new-main-sha>:refs/heads/dev
```

Watch for the misleading first symptom: `git fetch --prune` drops
`origin/dev` and later commands fail with `fatal: Not a valid object name
origin/dev`, which reads like a stale ref or a fetch race. Confirm against
`git ls-remote --heads origin` or the branches API before concluding either way.

## When `main` receives commits directly

Anything merged straight into `main` — a hotfix, or a pull request opened
against `main` by someone not following this runbook — is invisible to `dev`
and to staging. It was never staged, and the next promotion will collide with
it.

Reconcile before promoting:

1. Branch from `dev` and `git merge origin/main` into it.
2. Resolve conflicts by authority, not by taking a side wholesale:
   - Version metadata → dev's, the higher version being promoted.
   - `CHANGELOG.md` → union of both; entries are already descending.
   - Migration head assertions → do not guess. Diff `alembic/versions/` and
     assert what the DAG actually is.
   - The same test edited differently on both sides → keep both cases. Taking
     one side silently drops the other's coverage.
3. Run the full validation on the reconciled tree, then merge it into `dev`
   with a merge commit.

The reconciliation branch has `main` as a real ancestor, so it can also serve
as the promotion head if one is needed immediately.

## Automatic staging deployment contract

`.github/workflows/staging-deploy.yml` is disabled unless the repository
variable `STAGING_AUTO_DEPLOY_ENABLED` is exactly `true`. Do not enable it until
all of the following are true:

- A runner registered specifically to `michaelayoade/dotmac_sub` is installed
  on the staging host and has the labels `self-hosted`, `linux`, `x64`, and
  `dotmac-sub-staging`.
- The runner service uses the staging operator account with access to Docker,
  the persistent checkout, the deploy lock, and the database-backup directory.
- The runner account is already authenticated to
  `ghcr.io/michaelayoade/dotmac_sub` with read-only package access. Never place
  that credential in the repository or runner labels.
- The GitHub `staging` environment defines
  `STAGING_DEPLOY_DIR=/home/dotmac/deploy-worktrees/dotmac-sub-staging`.
- The persistent checkout contains the staging `.env` and the required
  host-local `docker-compose.override.yml`.
- The persistent checkout is a dedicated Git worktree used only by the staging
  deployment runner. Interactive development, agents, and diagnostic edits use
  separate worktrees and never write into the deployment worktree.
- The tracked deployment worktree is clean. The workflow refuses to discard
  tracked changes, verifies the exact fetched `origin/dev` candidate, checks it
  out at detached `HEAD`, and leaves every local branch pointer unchanged. A
  local development branch is preserved as a reference but never controls or
  blocks deployment checkout state.

The workflow accepts only a successful manually dispatched `Build release
candidate once` run from this repository's current `dev` tip. Start it with an
exact full SHA, for example:

```bash
gh workflow run release-candidate.yml --ref dev -f candidate_sha=<full-dev-sha>
```

The candidate workflow requires the exact dev SHA to have green CI and Mobile
CI, refuses to overwrite an existing `candidate-<full-sha>` bootstrap tag,
builds only on a GitHub-hosted runner, and uploads
`release-candidate-evidence`. That typed document binds the source commit, Git
tree, OCI digest, source-CI conclusion, and build run ID.
Open pull requests, including rolling version-bump pull requests,
are not candidate authority once that immutable evidence exists.

The staging workflow downloads evidence only from its triggering run,
independently recomputes the candidate tree, waits for the exact source checks,
rejects stale or mismatched evidence, verifies that Celery Beat is absent, and
verifies the private `10.120.121.20:8001` binding. It invokes
`scripts/deploy_staging.sh` with the digest and verifies the running image
reference plus revision, source-tree, and build-run labels. After successful
health checks, a GitHub-hosted job uploads `staging-acceptance-<source-sha>` for
the same commit, tree, and digest. The deployment owner independently repeats
the GitHub API decision for the image's full OCI revision before any database
or service change.

## Production workflow activation

The production workflows are manual and run no repository test suite.
`Promote staged digest for production` runs on a GitHub-hosted runner behind the
protected `production` environment. It downloads exact candidate and staging
artifacts by run ID, rechecks green `main`, tree equality, and ancestry, then
registry-tags the same digest. It never invokes a Docker build.

GitHub records the manually dispatched candidate run on `dev`, but records the
downstream `Deploy dev to staging` `workflow_run` on the default `main` branch
where that workflow executes. Production promotion validates those transport
branches independently from the typed staging acceptance, which still binds the
exact `dev` candidate revision, tree, digest, and build run.

`Deploy authorized digest to production` remains fail-closed until the
repository variable `PRODUCTION_DEPLOY_ENABLED` is exactly `true`. The
`production` environment must define `PRODUCTION_DEPLOY_DIR`, and its runner
must carry the dedicated `dotmac-sub-production` label. Each dispatch must name
`dotmac-sub-prod` and provide the successful authorization run ID. Do not enable
the variable or dispatch the workflow until Michael names and approves that
production target.

The GitHub-hosted verification job validates authorization before the production
runner is scheduled. The host job checks out the exact authorized `main`
revision in its runner workspace and uses the persistent directory only for
host-owned `.env`, Compose overrides, and deployment state.

## Staging database-backup policy

The staging database is non-authoritative and its PostgreSQL workload and local
backup destination share the staging host disk. A full `pg_dump` before every
staging deployment is therefore disabled: repeated dumps can starve the
application and workers of disk I/O without protecting production data.

Every automatic or manual staging deployment must invoke
`scripts/deploy_staging.sh`; operators must not call `scripts/deploy.sh`
directly on staging. The adapter fails closed unless `.env` contains the exact
staging host contract, then selects `DEPLOY_BACKUP_MODE=skip_staging` and the staging-only
proxy opt-out before delegating to `scripts/deploy.sh`. It also owns a fixed
ten-minute health budget for candidate, primary, and rollback startup. Seabone
has repeatedly needed more than three minutes to import the application under
measured disk and swap pressure; the longer staging budget prevents a healthy
cold start from being rolled back while preserving every health assertion.

Production requires a backup by default and rejects generic `SKIP_BACKUP=1`.
Only `scripts/deploy_production.sh --hotfix-no-migrations` may request an
exception, and it requires an incident/change reference plus reason. The
adapter fingerprints the complete migration file set in the running and
candidate images, derives both image-head sets, reads the exact database heads,
and skips only when all comparisons match. Missing, changed, or malformed
evidence keeps the backup enabled. The offsite jobs under `scripts/backup/` and
existing staging backup files are unchanged.

## Failure behavior

- A missing runner, disabled repository switch, wrong host path, dirty tracked
  checkout, stale dev SHA, failed check, malformed or mismatched evidence,
  missing digest, unexpected port, or active Celery Beat prevents deployment.
- The deployment checkout never moves, merges, resets, or force-updates a local
  branch. It detaches at the verified candidate so an unrelated local branch
  cannot diverge from `origin/dev` and block or influence a release.
- `scripts/deploy.sh` still owns backup, migration, candidate health, primary
  health, worker readiness, rollback, and image-retention behavior; the guarded
  staging adapter supplies only the staging-specific backup and proxy opt-outs.
- A failed staging deployment never authorizes promotion to `main`.
- Production deploys only a typed, authorized digest. The self-hosted production
  job runs bounded operational checks and no repository test suite.
