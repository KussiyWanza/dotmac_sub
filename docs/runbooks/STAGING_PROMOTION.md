# Main-to-staging-to-production release

This repository releases from a single trunk. A feature branch merges into
`main`; `main` is then built once into an immutable image, that exact image is
deployed to staging, and only the accepted digest is authorized for production.

**There is no `dev` branch and no branch-to-branch promotion.** The former
dev-first hop was removed on 2026-08-28 because it cost a merge, a
reconciliation pull request, and a second full CI cycle per release without
adding a check that the digest gate did not already make.

**Staging did not become optional.** It moved from being a branch gate to being
a digest gate. `Promote staged digest for production` refuses to authorize any
digest that has no matching staging acceptance document, so nothing reaches
production that a real host has not already run.
What was removed is the merge, not the proof.

## Release sequence

1. Open the feature pull request against `main` with the appropriate
   `version:major`, `version:minor`, `version:patch`, or `version:none` label.
   Do not edit `VERSION` in the source pull request; the version-bump workflow
   owns the separate rolling bump pull request after merge.
2. Require CI, Mobile CI, and Version Impact to pass, then squash-merge into
   `main`.
3. Select the exact `origin/main` SHA intended for deployment and require CI and
   Mobile CI to pass on that exact commit.
   An open rolling version-bump pull request does not block a digest-bound
   candidate for an already-selected source SHA; it governs semver metadata and
   aliases, not deployment authority.
4. Dispatch `Build release candidate once` on `main`, supplying that full main
   SHA as `candidate_sha`. The workflow refuses a stale SHA or non-green source,
   builds the application once on GitHub, and records its immutable OCI digest.
   The build also derives `/app/product-manifest.json` from the image's exact
   `SUB_ASSEMBLY` and `VERSION`. The OCI digest therefore binds the canonical
   manifest bytes. The workflow pulls that exact digest, verifies the embedded
   document inside the image, and publishes `candidate.json` schema v3 plus
   `product-manifest.json`; the typed candidate record carries the manifest's
   `product_manifest_digest`.

   **If a `candidate-<sha>` tag already exists for this commit** (most often
   because an earlier run for the same SHA built and pushed successfully but
   then failed a later step, such as the post-push digest-visibility check),
   the workflow does not refuse to run again and it does not rebuild. It reads
   the existing image's own provenance labels
   (`org.opencontainers.image.revision`, `io.dotmac.release.source-tree`,
   `io.dotmac.release.build-run`), looks for a prior `candidate.json` from an
   earlier successful run of this same workflow for this same commit, and:
   - if the labels agree with this commit and any prior evidence found agrees
     with the registry digest, it **reuses** the existing image: no rebuild, no
     re-push, but the candidate evidence is still re-derived from the image
     itself and re-uploaded under the CURRENT run (this run's artifact is what
     `Deploy main to staging` actually downloads, so evidence must exist under
     this run's ID even when nothing was rebuilt);
   - if the labels disagree with this commit, are unreadable, or a prior
     evidence document disagrees with the registry digest, it **fails closed**
     naming the exact disagreement — never silently rebuilds over a
     conflicting tag, and never permanently blocks the commit either.
   Every field in the re-derived evidence document except which run wrote it
   is recoverable from the immutable image; a resume needs no state carried
   over from the failed run.
5. Let `Deploy main to staging` deploy that exact digest, then complete staging
   acceptance against `http://10.120.121.20:8001`. **That acceptance covers
   application behaviour only — it does not exercise network equipment.** See
   "What staging acceptance does not cover" below.
6. Dispatch `Promote staged digest for production` on `main` with the candidate
   build run, the staging run, and the **staged release SHA** (`staged_release_sha`
   — the commit the candidate was built from, NOT whatever `main` happens to be
   now). It proves tree equality and ancestry, records typed authorization, and
   attaches the version and `latest` aliases to the staged digest without
   rebuilding. A candidate that never reached staging has no acceptance
   document and is refused here.

   **`main` is allowed to have moved on.** The workflow separates two
   identities: the *authorizing* `main` tip, which supplies the workflow and
   verifier code, and the *staged release*, which supplies the tree, digest and
   VERSION actually deployed. Only the second is released; the first merely has
   to still contain it. Requiring them to be the same commit is what previously
   forced a rebuild and a fresh staging cycle every time anything landed on
   `main` — which a version-bump pull request does after every merge.
7. Dispatch `Deploy authorized digest to production` only after Michael names
   `dotmac-sub-prod`, supplies the authorization run ID, and the protected
   production environment approves it. The default path takes a backup.

There is no post-release branch reconciliation step. The release ran on the
branch it was authored on, so no second branch is left behind to catch up.

Invoke the candidate workflow only for the exact source SHA intended for this
release. Do not build and stage every intermediate feature merge. The isolated
GHCR workflow publishes only the pinned GenieACS runtime; it never builds an
application image. If the rolling version-bump pull request remains open, the
production promotion still authorizes and deploys the immutable digest; the
existing semver tag is not moved when it already points at an older digest, and
only `latest` is advanced to the authorized production digest.

Schema-v1 release evidence predates the product-manifest identity, and
schema-v2 evidence predates the `build_run_id`/`evidence_run_id` split; neither
is accepted by the current schema-v3 readers. Do not combine evidence across
schema versions or retrofit a manifest onto an old candidate: select the
current green `main` SHA and build a new candidate once.

**Schema v3's two run-id fields are separate identities, never
interchangeable:**

- `build_run_id` — the run that BUILT the image. Baked into the image itself
  as the `io.dotmac.release.build-run` label, and never rewritten. This is
  what `Deploy main to staging` compares against the deployed container's own
  label.
- `evidence_run_id` — the run that wrote/uploaded THIS `candidate.json`
  document. On a fresh build the two are equal. On a resumed run that reuses a
  previously published image (see above), they diverge: the image (and
  `build_run_id`) still name the original build, while `evidence_run_id` names
  the resume run whose artifact was actually downloaded. This is what a
  downstream workflow's "does this artifact match the run I just downloaded
  it from" check compares against.

The v1-to-v2 migration conflated these into one `build_run_id` field, which
worked only because build and evidence always happened in the same run. A
production incident on 2026-09-09 — a successful build whose post-push
digest-visibility check then failed on a transient GHCR propagation delay —
showed that a resume needs to write evidence under a different run than the
one that built the image, and the single overloaded field could not carry
both meanings at once.

## Release Freeze

Once release deployment is in flight, `main` is frozen for merges until the
candidate is deployed to production or the release is explicitly abandoned. The
freeze is intentionally narrow: feature branch pushes, pull request creation,
and pull request updates continue, but no pull request should merge into `main`
while `Build release candidate once`, `Deploy main to staging`,
`Promote staged digest for production`, or `Deploy authorized digest to
production` is queued, running, or awaiting approval.

This freeze carries more weight on a single trunk than it did with a `dev` hop:
`main` is now both the branch people merge into and the branch a candidate is
selected from, so an unfrozen merge moves the release base directly. The
`Release Freeze Gate` workflow is the required pull-request check that enforces
this boundary on `main`. It reads active GitHub Actions runs and fails
only when one of those release-control workflows is queued, in progress, or
awaiting approval. It does not inspect open pull requests and does not
reinterpret the selected candidate; the candidate SHA and OCI digest remain
the deployment authority.

**Known limitation, not an oversight.** The gate's result is cached at a pull
request's head SHA and is only re-evaluated when that PR's own branch moves --
not when a release enters its approval wait after the check already reported
green. While a promotion or deploy is sitting in its production approval-wait
window, avoid merging other pull requests even if the freeze gate still shows
green on them. Real merge-time enforcement would need a GitHub merge queue,
which this repository cannot currently enable safely (see the header comment
in `release-freeze-gate.yml`). What actually protects production if this
discipline lapses is `release-promotion.yml`'s authorization pinning
(`ref: ${{ github.sha }}`, not `ref: main`) and its post-approval reachability
re-proof against main's live tip -- confirmed live in production 2026-09-09,
these close the window this freeze gate cannot.

### One-time workflow bootstrap

GitHub accepts `workflow_dispatch` only after the workflow file exists on the
default branch. A pull request that ADDS or RENAMES a release-chain workflow
therefore cannot be staged by the workflow it introduces: the dispatchable
version does not exist until that pull request has merged to `main`. Merge it
first, then dispatch the candidate build against the resulting `main` SHA as
usual. Do not fabricate an evidence artifact or bypass staging to shorten the
bootstrap.

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

Every pull request into `main` is squash-merged. Retiring the `dev` branch
retired the whole class of problem this section used to describe: there is no
promotion pull request whose head is a long-lived branch, so there is no
ancestry to preserve between two trunks and no reconciliation owed after a
release.

| Pull request | Method |
|---|---|
| Feature or fix into `main` | Squash |
| Rolling version bump into `main` | Squash |
| `integration/**` or `consolidate/**` batch into `main` | Merge commit |

A rolling version bump changes `pyproject.toml`, which is the complete source
of the `dependency-manifest` Kernel composition observation. The version bump
generator therefore refreshes both dependency-manifest digests in
`docs/kernel-runtime-composition.json` in the same generated commit. A digest
mismatch is a failed generation, not a lint exception; do not disable or skip
the composition-observation check to merge a bump.

A batch branch keeps a merge commit because its individual commits carry the
migration sequence that `migration_sequence_gate.py` reads; squashing a batch
collapses that ordering into one commit and the gate can no longer see it.

Why the old rule existed, so it is not reintroduced by habit: a squash-merged
`dev`-into-`main` promotion landed on `main` as a commit `dev` had never seen,
`main` stopped being an ancestor of `dev`, and the next promotion reported
`CONFLICTING` on `VERSION`, `CHANGELOG.md`, `pyproject.toml`, `package.json`,
`package-lock.json`, and the three mobile version files. That failure mode is
structural to two-trunk promotion and cannot occur with one trunk. If a
long-lived branch is ever reintroduced, restore the merge-commit rule with it.

### Branch protection: green is required, an approving review is not

`main` requires its status checks to pass and **zero** approving reviews. That
is deliberate, not an oversight. Do not add a review requirement without first
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

### `delete_branch_on_merge` deletes the head branch of every merged PR

The repository sets `delete_branch_on_merge: true`. That is what you want for
topic branches, and with a single trunk every merged head IS a topic branch, so
the trap that used to exist here is gone: a promotion pull request's head was
`dev`, and merging one deleted `dev` outright (hit live on 2026-07-31).

The rule survives its instance. If a long-lived branch is ever added — a release
or maintenance branch that pull requests will be opened *from* — protect it
against deletion before the first such pull request merges:

```
gh api -X PUT repos/michaelayoade/dotmac_sub/branches/<branch>/protection --input - <<'JSON'
{"required_status_checks":null,"enforce_admins":false,
 "required_pull_request_reviews":null,"restrictions":null,
 "allow_deletions":false,"allow_force_pushes":false}
JSON
```

Passing or omitting `--delete-branch` on `gh pr merge` makes no difference. That
flag only controls whether the CLI removes your *local* branch; the remote
deletion comes from the repository setting. Mistaking the two is the whole trap.

If it does happen, nothing is lost: the deleted branch's head is a parent of the
new merge commit, so every commit is still reachable. Recreate it at the base
branch's **new head**, not at its own former head.

Watch for the misleading first symptom: `git fetch --prune` drops the remote
ref and later commands fail with `fatal: Not a valid object name`, which reads
like a stale ref or a fetch race. Confirm against `git ls-remote --heads origin`
or the branches API before concluding either way.

## Hotfixes

A hotfix is not a special path any more. It is a pull request into `main` like
any other, and it goes through the same candidate build, staging deploy, and
digest authorization. The `dev-first:override` label no longer exists; there is
nothing left for it to override.

The only production exception that remains is the backup exception:
`scripts/deploy_production.sh --hotfix-no-migrations`, which requires an
incident/change reference plus a reason, and verifies that the running and
candidate images carry an identical migration set.
It shortens the deploy, not the pipeline.

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
  tracked changes, verifies the exact fetched `origin/main` candidate, checks it
  out at detached `HEAD`, and leaves every local branch pointer unchanged. A
  local development branch is preserved as a reference but never controls or
  blocks deployment checkout state.

The workflow accepts only a successful manually dispatched `Build release
candidate once` run from this repository's current `main` tip. Start it with an
exact full SHA, for example:

```bash
gh workflow run release-candidate.yml --ref main -f candidate_sha=<full-main-sha>
```

The candidate workflow requires the exact main SHA to have green CI and Mobile
CI, refuses to overwrite an existing `candidate-<full-sha>` bootstrap tag,
builds only on a GitHub-hosted runner, and uploads
`release-candidate-evidence`. That typed document binds the source commit, Git
tree, OCI digest, source-CI conclusion, `build_run_id`, and `evidence_run_id`.
Open pull requests, including rolling version-bump pull requests,
are not candidate authority once that immutable evidence exists.

The staging workflow downloads evidence only from its triggering run and
verifies the document's `evidence_run_id` against that same triggering run —
never `build_run_id`, which may legitimately name an earlier resumed build. It
independently recomputes the candidate tree, waits for the exact source
checks, rejects stale or mismatched evidence, verifies that Celery Beat is
absent, and verifies the private `10.120.121.20:8001` binding. It invokes
`scripts/deploy_staging.sh` with the digest and verifies the running image
reference plus revision, source-tree, and build-run labels — the deployed
container's `io.dotmac.release.build-run` label is compared against
`build_run_id`, the run that produced the image, not the run that deployed it.
After successful
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

The candidate run and the downstream `Deploy main to staging` `workflow_run`
are both recorded on `main` — the dispatched candidate because that is the
branch it is dispatched against, and the staging run because a `workflow_run`
is always attributed to the default branch where the workflow executes.
Production promotion validates those transport branches independently from the
typed staging acceptance, which binds the exact candidate revision, tree, and
digest. It downloads the candidate evidence artifact from the supplied
candidate-build run ID and checks that run against the document's
`evidence_run_id`; `build_run_id` continues to flow unchanged into the
production authorization document, identifying only which run built the
deployed image.

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
host-owned `.env`, Compose overrides, and deployment state. Host-side Python
verifiers run with safe-path isolation and an explicit `PYTHONPATH` rooted at
that authorized workspace; the persistent checkout must never shadow their
release-evidence or backup-policy modules.

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
  checkout, stale main SHA, failed check, malformed or mismatched evidence,
  missing digest, unexpected port, or active Celery Beat prevents deployment.
- The deployment checkout never moves, merges, resets, or force-updates a local
  branch. It detaches at the verified candidate so an unrelated local branch
  cannot diverge from `origin/main` and block or influence a release.
- `scripts/deploy.sh` still owns backup, migration, candidate health, primary
  health, worker readiness, rollback, and image-retention behavior; the guarded
  staging adapter supplies only the staging-specific backup and proxy opt-outs.
- A failed staging deployment never authorizes a production digest: the
  authorization step requires a staging acceptance document and finds none.
- Production refuses any deploy that is not a descendant of the revision
  already running, before the database backup or migrations. A deliberate
  rollback requires a typed authorization naming the exact
  `from_revision`/`to_revision` transition plus a change reference and reason;
  there is no boolean override, and a document written for a different
  transition is refused rather than reused.
- An empty running-revision observation never bypasses that gate. Docker daemon,
  inventory, and inspection failures and missing/malformed labels fail closed.
  Only a confirmed absent production container may use the separately typed
  first-deployment authorization, which names the exact target revision,
  `dotmac-sub-prod`, a change reference, and a reason.
- Production deploys only a typed, authorized digest. The self-hosted production
  job runs bounded operational checks and no repository test suite.
