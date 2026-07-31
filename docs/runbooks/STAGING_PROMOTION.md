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
3. Merge into `dev`. Push CI and the GHCR workflow then validate and build the
   exact `origin/dev` commit.
4. Deploy the immutable `sha-<short-commit>` image only to the staging host.
5. Complete staging acceptance against `http://10.120.121.20:8001`.
6. Merge the rolling version-bump PR for `dev`, when one exists, and repeat the
   staging gate for that final versioned dev commit.
7. Promote the accepted dev tree to `main` without unrelated changes. The
   promotion PR uses `version:none` with a body explaining that the version was
   already established and validated on dev. **Merge it with a merge commit,
   never a squash** — see "Merge methods" below.
8. Require main CI and the main GHCR build to pass. Only the default branch may
   receive the moving `latest` image tag.
9. Deploy the immutable main image to production only after Michael explicitly
   requests the named production host.

The main build remains separate from the dev build. GitHub Actions layer
caching makes the second build faster while preserving commit-specific OCI
revision evidence.

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
  `STAGING_DEPLOY_DIR=/home/dotmac/projects/dotmac_sub`.
- The persistent checkout contains the staging `.env` and the required
  host-local `docker-compose.override.yml`.
- The tracked persistent checkout is clean. The workflow refuses to discard
  tracked changes and updates local `dev` only by fast-forward.

The workflow accepts only a successful push-triggered GHCR build from this
repository's current `dev` tip. It waits for the CI and Mobile CI push workflows
for the exact same commit, rejects stale or failed candidates, verifies that
Celery Beat is absent, verifies the private `10.120.121.20:8001` binding, and
then invokes the hardened deployment owner with the staging-only proxy opt-out.

## Failure behavior

- A missing runner, disabled repository switch, wrong host path, dirty tracked
  checkout, stale dev SHA, failed check, missing image, unexpected port, or
  active Celery Beat prevents deployment.
- `scripts/deploy.sh` still owns backup, migration, candidate health, primary
  health, worker readiness, rollback, and image-retention behavior.
- A failed staging deployment never authorizes promotion to `main`.
