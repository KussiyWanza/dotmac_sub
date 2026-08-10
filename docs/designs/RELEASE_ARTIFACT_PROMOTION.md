# Build-once release artifact promotion

**Status:** Build-once candidate, digest-bound staging, production promotion,
production verification, backup enforcement, and publisher cutover implemented

**Owner:** Dotmac Sub release control plane

## Decision

The standard release path will build one immutable application image from the
final, green `dev` release candidate. Staging will accept that exact OCI digest.
After promotion, production eligibility will be derived from green `main` CI,
Git-tree equality, ancestry, and the recorded staging acceptance. Registry-side
tagging may add version or `latest` aliases, but it must not create a second
application image.

The active candidate path uses the contracts through
`scripts/release_candidate_evidence.py`. The exact intended green `dev` commit
is selected manually by full SHA, built once, and recorded as a digest-bound
artifact. An open rolling version-bump pull request is not deployment authority
for an already-selected candidate; version metadata and semver aliases are
separate from digest eligibility. Staging consumes and records that digest. The
production authorization is recorded separately after the staged tree reaches
green `main`. The application publisher no longer rebuilds on `dev` or `main`;
only the independent pinned GenieACS runtime remains in `ghcr.yml`.

The release freeze is merge control, not deployment authority. While a
candidate, staging deployment, production authorization, or production
deployment workflow is queued or running, pull requests into `dev` must not
merge. Branch pushes, pull request creation, and pull request updates remain
open. The freeze prevents `dev` from moving underneath an in-flight candidate;
the selected SHA and OCI digest remain the facts that deployment consumes.

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

Unrestricted production `SKIP_BACKUP=1` use is retired. The explicit production
adapter carries a non-secret change reference and reason, fingerprints both
images' complete migration trees, derives their heads, reads the database
heads, and re-evaluates the typed policy before `deploy.sh` permits a skip.

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

- The retired `.github/workflows/ghcr.yml` application job built on every `dev`
  and `main` push.
- The retired staging path treated each successful dev image build as a
  staging candidate.
- The retired production gate assumed image source and release authorization
  were the same commit and accepted a generic `SKIP_BACKUP=1` override.

### New owner and paths

- The explicit release-candidate workflow selects the exact current `dev`
  tip intended for release and builds the application image once.
- A staging deployment record will bind acceptance to the immutable digest.
- The promotion workflow proves main tree equality, ancestry, main CI, and
  staging acceptance before adding production aliases to the same digest.
- The production adapter verifies distinct source and release revisions and
  deploy by digest.
- A typed backup-policy decision owns the staging skip, production default,
  and proven no-migration hotfix exception.

### Shadow and verification phase

The candidate and staging slices ran in shadow before cutover and completed an
accepted exact-digest rehearsal. The one promotion that first placed the new
workflow on GitHub's default branch used the previously active staging path
because `workflow_dispatch` cannot activate a workflow absent from the default
branch. The shadow publisher is now retired.

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

Ordinary application publishing from dev/main pushes, intermediate staging
triggers, and the generic production backup skip are retired. The emergency
path remains explicit, attributable, digest-pinned, and subject to the same
staging and production evidence rules.

## Planned implementation slices

1. Typed contracts and design evidence (complete).
2. Explicit one-time candidate build and digest-bound staging evidence
   (complete).
3. Main tree/ancestry authorization and registry-side digest promotion
   (complete).
4. Digest-based production verification and hotfix backup enforcement
   (complete).
5. Trigger cutover, duplicate publisher removal, and GenieACS build isolation
   (complete).
