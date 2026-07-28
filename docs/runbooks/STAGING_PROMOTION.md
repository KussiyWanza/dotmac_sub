# Dev-to-staging-to-main promotion

This repository promotes release changes through `dev` and the staging
environment before `main`. A feature branch must not bypass this path.

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
   already established and validated on dev.
8. Require main CI and the main GHCR build to pass. Only the default branch may
   receive the moving `latest` image tag.
9. Deploy the immutable main image to production only after Michael explicitly
   requests the named production host.

The main build remains separate from the dev build. GitHub Actions layer
caching makes the second build faster while preserving commit-specific OCI
revision evidence.

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
