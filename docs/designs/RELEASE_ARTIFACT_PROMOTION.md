# Build-once release artifact promotion

**Status:** Step 2 candidate build and digest-bound staging path implemented;
production promotion and publisher cutover remain inactive

**Owner:** Dotmac Sub release control plane

## Decision

The standard release path will build one immutable application image from the
final, green `dev` release candidate. Staging will accept that exact OCI digest.
After promotion, production eligibility will be derived from green `main` CI,
Git-tree equality, ancestry, and the recorded staging acceptance. Registry-side
tagging may add version or `latest` aliases, but it must not create a second
application image.

The explicit candidate path now uses the contracts through
`scripts/release_candidate_evidence.py`. The final current green `dev` commit
is selected manually after the rolling version bump, built once, and recorded
as a digest-bound artifact. Staging consumes and records that digest. The
existing dev/main GHCR publisher remains in place during migration, and the
separate main build remains the production path until the later authorization
and cutover slices retire it.

## Ownership and authoritative evidence

The release control plane owns candidate selection, artifact promotion, and
production-eligibility decisions. Its authoritative inputs are:

- GitHub-hosted CI workflow conclusions for exact source and release commits;
- Git commit and tree identities supplied by GitHub;
- the immutable OCI manifest digest published to GHCR;
- the GitHub staging deployment result for that exact digest; and
- explicit, typed migration evidence for a production hotfix backup exception.

GHCR owns artifact bytes and aliases; it does not decide whether an artifact is
safe for production. GitHub-hosted CI owns test execution and its conclusions;
deployment hosts do not reinterpret or reproduce test results. Staging and
production scripts remain transport/operation adapters around the release
decision.

The release record has one canonical identity tuple:

```text
(source commit, source tree, OCI digest, build workflow run)
```

Staging acceptance must repeat the same source commit, tree, and digest.
Production authorization adds a distinct `main` release commit whose tree must
equal the source tree and whose ancestry must contain the source commit.

## State model

The control plane derives these states from evidence rather than mutable tags:

1. **Built:** exact `dev` source CI is green and one OCI digest exists.
2. **Staging accepted:** the staging deployment succeeded for the same source
   commit, source tree, and OCI digest.
3. **Main authorized:** exact `main` CI is green, its tree equals the staged
   source tree, and the staged source commit is in its ancestry.
4. **Production eligible:** all prior evidence remains exact and available.

A missing, pending, failed, cancelled, stale, or mismatched observation fails
closed. A changed source tree requires a new build and a new staging acceptance.
Attaching a new tag to a digest never changes its state.

## Backup policy

Backup behavior is owned by the identified deployment environment:

- Staging always skips the pre-deployment database backup through its exact-host
  adapter. A hotfix exception is not a staging control.
- Production requires a backup by default.
- Production may omit the backup only for an explicitly attributed hotfix whose
  complete migration-graph digest is unchanged, whose running and candidate
  image heads are identical, and whose database is already at those exact
  candidate heads.
- Missing or ambiguous hotfix evidence resolves to taking the backup.

The later enforcement step will retire unrestricted `SKIP_BACKUP=1` use. It
will replace it with an explicit production hotfix command carrying a non-secret
change reference and reason. Step 1 does not alter current backup behavior.

## Test and host boundary

Formatters, linters, type checks, security scans, unit tests, architecture
tests, integration tests, migration rehearsals, and browser/mobile tests run on
GitHub-hosted CI. They do not run on staging or production hosts.

Deployment hosts perform only bounded operational checks: host identity,
release evidence and digest verification, backup policy, current database
migration-state observation, migrations, schema/readiness checks, service
replacement, health checks, and rollback. These checks do not constitute a
replacement test suite.

The staging deployment worktree checks out the verified candidate at detached
`HEAD` and never advances, merges, resets, or force-updates a local branch.
Local branch refs are preserved operational context, not release evidence and
not an input to the release-control decision.

## Authority migration and cutover

### Old owner and paths

- `.github/workflows/ghcr.yml` builds on every `dev` and `main` push.
- `.github/workflows/staging-deploy.yml` treats each successful dev image build
  as a staging candidate.
- `scripts/deploy.sh` assumes the image source revision and environment release
  authorization revision are the same commit.
- `SKIP_BACKUP=1` is a generic process-environment override.

### New owner and paths

- An explicit release-candidate workflow will select the final current `dev`
  tip after the version bump and build the application image once.
- A staging deployment record will bind acceptance to the immutable digest.
- A promotion workflow will prove main tree equality, ancestry, main CI, and
  staging acceptance before adding production aliases to the same digest.
- The production adapter will verify distinct source and release revisions and
  deploy by digest.
- A typed backup-policy decision will own the staging skip, production default,
  and proven no-migration hotfix exception.

### Shadow and verification phase

The candidate workflow and staging evidence run without publishing production
aliases. They reject stale candidates, a non-green exact `dev` SHA, malformed
or mismatched build evidence, and any staging result not bound to the built
digest. The existing main build remains the production path during this phase.
The one promotion that places the new workflow on GitHub's default branch uses
the previously active staging path because `workflow_dispatch` cannot activate
a workflow that does not yet exist on the default branch.

### Cutover gate

Cutover requires all of the following:

- one candidate build reports its immutable OCI digest;
- staging deploys and records success for that digest;
- main CI passes for a commit with the identical tree and required ancestry;
- the promotion workflow attaches aliases without invoking a Docker build;
- the production verifier approves the separated source/release evidence;
- backup-policy tests prove staging skip, production default, and hotfix
  fail-closed behavior; and
- architecture tests prevent ordinary dev/main pushes from restoring duplicate
  application builds or host-side test execution.

### Fallback retirement

After one accepted end-to-end rehearsal, remove ordinary application image
publishing from dev/main push workflows, remove staging deployment triggers for
intermediate dev builds, and retire the generic production backup-skip path.
Keep any emergency path explicit, attributable, digest-pinned, and subject to
the same staging and production evidence rules.

## Planned implementation slices

1. Typed contracts and design evidence (complete).
2. Explicit one-time candidate build and digest-bound staging evidence
   (implemented in this step).
3. Main tree/ancestry authorization and registry-side digest promotion.
4. Digest-based production verification and hotfix backup enforcement.
5. Trigger cutover, duplicate publisher removal, and GenieACS build isolation.
