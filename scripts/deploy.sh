#!/usr/bin/env bash
# Deploy dotmac_sub from a registry-built (GHCR) image — no host build.
#
# This is the RECOMMENDED production path: it runs the exact image CI built and
# tested, decoupled from the box's git working tree (which has repeatedly drifted
# — feature branches, dirty trees, hand-applied migrations). `make prod-deploy`
# (host build) is kept only as an air-gapped/registry-down fallback.
#
# Usage:
#   deploy.sh sha-abc1234        deploy an existing immutable image tag
#   deploy.sh sha256:<64-hex>    deploy the repository image by exact OCI digest
#   deploy.sh --status           show pinned vs running image
#   Production backups may be omitted only through deploy_production.sh with
#   a typed, verified no-migration hotfix decision.
#   HEALTH_CURL_TIMEOUT=N ...    cap each health-check curl attempt at N seconds
#                                (default 5) so a hung health endpoint can't stall
#                                a retry indefinitely
#   BACKGROUND_STABILITY_SECONDS=N
#                              require workers and Beat to remain restart-free
#                              for N seconds before accepting the release
#   REQUIRE_PROXY_HANDOFF=0    skip the Nginx warm-candidate contract, for hosts
#                              with no proxy in front of the app (staging).
#                              Defaults to 1 — production must keep it.
#   IGNORE_COMPOSE_OVERRIDE=1  ignore docker-compose.override.yml even if present
#
# Compose files: docker-compose.yml, plus docker-compose.override.yml when it
# exists — matching what a bare `docker compose` on the host resolves. Services
# are then intersected with `compose config --services`, so a profile-gated
# service (e.g. staging's celery-beat) is never force-started by being named.
#
# Host-specific values (HEALTH_URL, DB_CONTAINER) fall back to .env when not
# set in the environment.
#
# RUN IT DETACHED over SSH -- `nohup ./scripts/deploy.sh sha-... &` or inside
# tmux. A dropped SSH session sends SIGHUP and kills the deploy mid-flight; the
# script now cleans up after itself, but a deploy that dies during `alembic
# upgrade heads` still leaves the schema half-applied.
#
# Procedure:
#   pull image from GHCR -> verify OCI revision + exact GitHub CI evidence ->
#   DB backup -> pin APP_IMAGE + GIT_SHA in .env ->
#   migrate + verify -> warm candidate -> recreate app+workers ->
#   web + background-runtime health gates.
#
# On a failed health gate the previous image is re-pinned and the services are
# recreated on it. Migrations are NOT reverted automatically — new revisions must
# be backward-compatible with the previous release.
set -euo pipefail

# Deploy dir == repo root (sub deploys in place). Override with DEPLOY_DIR.
DEPLOY_DIR="${DEPLOY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO_DIR="${REPO_DIR:-${DEPLOY_DIR}}"
cd "${DEPLOY_DIR}"

# Read a key out of .env without sourcing it (sourcing a deploy .env pulls in
# every secret and DATABASE_URL as shell state). Defined up here because
# DB_CONTAINER is needed by the orphaned-pg_dump guard below, long before the
# rest of the config block.
env_value() {
  local key="$1"
  local value
  value="$(grep -E "^${key}=" .env 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
  printf '%s\n' "${value}"
}

IMAGE_REPO="ghcr.io/michaelayoade/dotmac_sub"
APP_CONTAINER="dotmac_sub_app"
GITHUB_RELEASE_REPOSITORY="michaelayoade/dotmac_sub"
PYTHON_BIN="${PYTHON_BIN:-python3}"
# Host-specific, so resolve shell env > .env > default. The default assumes the
# app publishes on loopback; staging binds its port to a host-internal address
# instead, and a wrong default here fails the gate rather than passing it.
HEALTH_URL="${HEALTH_URL:-$(env_value HEALTH_URL)}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8001/health}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-180}"
# Per-attempt cap on the health-check curl itself, distinct from the overall
# HEALTH_TIMEOUT_SECONDS retry budget above — without it a hung health
# endpoint stalls a single curl call indefinitely instead of failing fast
# into the next retry.
HEALTH_CURL_TIMEOUT="${HEALTH_CURL_TIMEOUT:-5}"
IMAGE_RETAIN_COUNT="${IMAGE_RETAIN_COUNT:-5}"
MIGRATION_MAX_ATTEMPTS="${MIGRATION_MAX_ATTEMPTS:-4}"
MIGRATION_RETRY_SECONDS="${MIGRATION_RETRY_SECONDS:-10}"
CANDIDATE_CONTAINER="${CANDIDATE_CONTAINER:-dotmac_sub_app_candidate}"
CANDIDATE_PORT="${CANDIDATE_PORT:-18001}"
CANDIDATE_HEALTH_URL="${CANDIDATE_HEALTH_URL:-http://127.0.0.1:${CANDIDATE_PORT}/health}"
CANDIDATE_DRAIN_SECONDS="${CANDIDATE_DRAIN_SECONDS:-2}"
BACKGROUND_RUNTIME_TIMEOUT_SECONDS="${BACKGROUND_RUNTIME_TIMEOUT_SECONDS:-90}"
BACKGROUND_STABILITY_SECONDS="${BACKGROUND_STABILITY_SECONDS:-15}"
CELERY_INSPECT_TIMEOUT_SECONDS="${CELERY_INSPECT_TIMEOUT_SECONDS:-5}"
# Every service that runs the app image and must be recreated on a new build.
APP_SERVICES=(app celery-worker celery-worker-bandwidth celery-worker-ingestion \
  celery-worker-monitoring celery-worker-billing celery-worker-tr069 celery-beat \
  bandwidth-poller syslog-listener)
CELERY_WORKER_SERVICES=(celery-worker celery-worker-bandwidth \
  celery-worker-ingestion celery-worker-monitoring celery-worker-billing \
  celery-worker-tr069)
CELERY_BEAT_SERVICE="celery-beat"

DB_CONTAINER="${DB_CONTAINER:-$(env_value DB_CONTAINER)}"
DB_CONTAINER="${DB_CONTAINER:-dotmac_pg_local}"

# --- One deploy at a time -------------------------------------------------
#
# Nothing used to stop two deploys running at once. On 2026-07-12 two did: each
# started a full pg_dump of the production database, load hit 52 on 16 cores,
# the app was starved out and the site served 502s for ~10 minutes. Had both
# reached `alembic upgrade heads` they would have raced the migration chain
# against the same database.
LOCK_FILE="${DEPLOY_LOCK_FILE:-/var/lock/dotmac_sub_deploy.lock}"
command -v flock >/dev/null || {
  # Fail closed, but say WHY. Without this, `! flock` succeeds on a missing
  # binary and the script claims another deploy holds the lock -- sending you
  # to hunt a process that does not exist.
  echo "REFUSING TO DEPLOY: flock(1) not found; cannot guarantee only one deploy runs." >&2
  echo "Install util-linux, or set DEPLOY_LOCK_FILE= to opt out (NOT recommended)." >&2
  exit 1
}
if ! { exec 9>"${LOCK_FILE}"; } 2>/dev/null; then
  echo "Cannot open deploy lock ${LOCK_FILE}" >&2
  exit 1
fi
if ! flock -n 9; then
  echo "REFUSING TO DEPLOY: another deploy already holds ${LOCK_FILE}." >&2
  pgrep -af "scripts/deploy.sh" | grep -v "^$$ " | sed "s/^/  running: /" >&2 || true
  exit 1
fi

# A pg_dump left behind by a deploy that died (e.g. its SSH session dropped)
# outlives its parent and keeps hammering the DB. The lock will not catch that
# -- the dead process released it. Refuse to pile a second dump on top.
if pgrep -f "pg_dump .*-d ${DB_NAME:-dotmac_sub}" >/dev/null 2>&1; then
  echo "REFUSING TO DEPLOY: a pg_dump is already running against ${DB_NAME:-dotmac_sub}." >&2
  pgrep -af "pg_dump" | sed "s/^/  /" >&2
  echo "It is probably orphaned from a deploy that died. Kill it, then retry:" >&2
  echo "  pkill -f pg_dump; docker exec ${DB_CONTAINER} pkill -f pg_dump" >&2
  exit 1
fi

log() { printf '\n==> %s\n' "$*"; }

wait_for_health() {
  local url="$1"
  local label="$2"
  local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
  while true; do
    if curl -fsS --connect-timeout "${HEALTH_CURL_TIMEOUT}" \
      --max-time "${HEALTH_CURL_TIMEOUT}" -o /dev/null "${url}" 2>/dev/null; then
      return 0
    fi
    if ((SECONDS >= deadline)); then
      echo "${label} health gate failed: ${url}" >&2
      return 1
    fi
    sleep 5
  done
}

service_container_id() {
  local service="$1"
  "${COMPOSE[@]}" ps -q "${service}" 2>/dev/null | tail -n 1
}

assert_background_service_running() {
  local service="$1"
  local container
  local state
  local restart_count

  container="$(service_container_id "${service}")"
  if [[ -z "${container}" ]]; then
    echo "BACKGROUND RUNTIME FAILURE: ${service} has no Compose container." >&2
    return 1
  fi
  state="$(docker inspect "${container}" \
    --format '{{.State.Status}}' 2>/dev/null || true)"
  restart_count="$(docker inspect "${container}" \
    --format '{{.RestartCount}}' 2>/dev/null || true)"
  if [[ "${state}" != "running" ]]; then
    echo "BACKGROUND RUNTIME FAILURE: ${service} is ${state:-unavailable}." >&2
    return 1
  fi
  if [[ ! "${restart_count}" =~ ^[0-9]+$ ]] || ((restart_count != 0)); then
    echo "BACKGROUND RUNTIME FAILURE: ${service} restart count is ${restart_count:-unknown}; expected 0." >&2
    return 1
  fi
}

background_runtime_ready() {
  local service
  local container
  local hostname
  local probe_container=""
  local -a expected_nodes=()

  # Beat is profile-gated off on staging (it must never originate production
  # work there), so assert it only where it is actually declared.
  if service_is_declared "${CELERY_BEAT_SERVICE}"; then
    assert_background_service_running "${CELERY_BEAT_SERVICE}" || return 1
  fi
  for service in "${CELERY_WORKER_SERVICES[@]}"; do
    assert_background_service_running "${service}" || return 1
    container="$(service_container_id "${service}")"
    hostname="$(docker inspect "${container}" \
      --format '{{.Config.Hostname}}' 2>/dev/null || true)"
    if [[ -z "${hostname}" ]]; then
      echo "BACKGROUND RUNTIME FAILURE: cannot resolve ${service} Celery node name." >&2
      return 1
    fi
    if [[ -z "${probe_container}" ]]; then
      probe_container="${container}"
    fi
    expected_nodes+=("celery@${hostname}")
  done

  if ! docker exec "${probe_container}" \
    python -m scripts.verify_celery_workers \
    --timeout "${CELERY_INSPECT_TIMEOUT_SECONDS}" \
    "${expected_nodes[@]}" >/dev/null 2>&1; then
    echo "BACKGROUND RUNTIME FAILURE: not every declared Celery worker answered ping." >&2
    return 1
  fi
}

wait_for_background_runtime() {
  local deadline=$((SECONDS + BACKGROUND_RUNTIME_TIMEOUT_SECONDS))

  until background_runtime_ready; do
    if ((SECONDS >= deadline)); then
      echo "Background runtime health gate failed after ${BACKGROUND_RUNTIME_TIMEOUT_SECONDS}s." >&2
      return 1
    fi
    sleep 5
  done

  if ((BACKGROUND_STABILITY_SECONDS > 0)); then
    log "Celery workers and Beat are ready; verifying ${BACKGROUND_STABILITY_SECONDS}s restart-free stability"
    sleep "${BACKGROUND_STABILITY_SECONDS}"
  fi
  if ! background_runtime_ready; then
    echo "Background runtime health gate failed during the restart-free stability check." >&2
    return 1
  fi
}

# The warm-candidate handoff keeps the site served while Compose replaces the
# primary, so it is required wherever the app is public-facing. Staging has no
# nginx in front of it and nothing to keep warm; without an opt-out the gate
# aborts the deploy on its very first step. Default 1 preserves the production
# contract exactly -- a host must opt OUT deliberately.
assert_proxy_handoff_contract() {
  local config
  if [[ "${REQUIRE_PROXY_HANDOFF:-1}" != "1" ]]; then
    log "Skipping Nginx warm-handoff contract (REQUIRE_PROXY_HANDOFF=${REQUIRE_PROXY_HANDOFF})"
    return 0
  fi
  if ! command -v nginx >/dev/null; then
    echo "DEPLOY AVAILABILITY FAILURE: nginx is not installed or not on PATH." >&2
    return 1
  fi
  if ! config="$(nginx -T 2>&1)"; then
    echo "DEPLOY AVAILABILITY FAILURE: nginx configuration cannot be read." >&2
    echo "Run 'nginx -T' on the host for the configuration error." >&2
    return 1
  fi
  if ! grep -Eq \
    "^[[:space:]]*server[[:space:]]+127\\.0\\.0\\.1:${CANDIDATE_PORT}[[:space:]]+backup" \
    <<<"${config}"; then
    echo "DEPLOY AVAILABILITY FAILURE: nginx has no warm candidate upstream on" >&2
    echo "127.0.0.1:${CANDIDATE_PORT}. Install nginx/selfcare.dotmac.io.conf and reload nginx." >&2
    return 1
  fi
}

CANDIDATE_STARTED=0
PRIMARY_REPLACED=0

cleanup_candidate() {
  if [[ "${CANDIDATE_STARTED}" == "1" ]]; then
    docker rm -f "${CANDIDATE_CONTAINER}" >/dev/null 2>&1 || true
    CANDIDATE_STARTED=0
  fi
}

stop_candidate_gracefully() {
  if [[ "${CANDIDATE_STARTED}" != "1" ]]; then
    return
  fi
  sleep "${CANDIDATE_DRAIN_SECONDS}"
  docker stop --time 30 "${CANDIDATE_CONTAINER}" >/dev/null 2>&1 || true
  docker rm -f "${CANDIDATE_CONTAINER}" >/dev/null 2>&1 || true
  CANDIDATE_STARTED=0
}

run_migrations() {
  local attempt=1
  local output
  local rc
  while ((attempt <= MIGRATION_MAX_ATTEMPTS)); do
    if output="$("${COMPOSE[@]}" run --rm --no-deps app alembic upgrade heads 2>&1)"; then
      printf '%s\n' "${output}"
      return 0
    else
      rc=$?
    fi
    printf '%s\n' "${output}" >&2
    if ! grep -qiE "lock timeout|canceling statement due to lock" <<<"${output}"; then
      return "${rc}"
    fi
    if ((attempt == MIGRATION_MAX_ATTEMPTS)); then
      echo "Migration remained lock-blocked after ${MIGRATION_MAX_ATTEMPTS} attempts." >&2
      return "${rc}"
    fi
    log "Migration lock timeout (attempt ${attempt}/${MIGRATION_MAX_ATTEMPTS}); retrying in ${MIGRATION_RETRY_SECONDS}s"
    sleep "${MIGRATION_RETRY_SECONDS}"
    attempt=$((attempt + 1))
  done
}

# Deploy-integrity gate. The immutable image must not be shadowed by a host
# source bind-mount: a `/app/app` mount means a dev overlay (docker-compose.dev.yml,
# or an override carrying a working-tree mount) got layered on, so the RUNNING
# code is the host working tree — not the tag we just deployed. This is
# invisible to the health gate (host code can be perfectly healthy), so check it
# explicitly. `/app/uploads` and other named volumes are fine; only `/app/app`
# (the Python package) shadowing the image is the failure.
#
# This — not excluding docker-compose.override.yml — is what actually guards
# deploy integrity. An override may legitimately carry deployment config
# (profiles, host-specific storage); it is a source bind-mount that is fatal.
assert_no_source_mount() {
  local container="${1:-${APP_CONTAINER}}"
  local mounts
  mounts="$(docker inspect "${container}" \
    --format '{{range .Mounts}}{{println .Destination}}{{end}}' 2>/dev/null || true)"
  if grep -qx '/app/app' <<<"${mounts}"; then
    echo "DEPLOY INTEGRITY FAILURE: ${container} has a host bind-mount at /app/app —" >&2
    echo "the working tree is shadowing the image, so '${TAG:-?}' is NOT the running code." >&2
    echo "Cause: a dev overlay was loaded (a compose file mounting the working tree," >&2
    echo "or a bare 'docker compose up/restart' on the host). Fix on the host, then redeploy:" >&2
    echo "  ${COMPOSE[*]} up -d --force-recreate ${APP_SERVICES[*]}" >&2
    return 1
  fi
}

# Compose file set.
#
# This used to be `-f docker-compose.yml` alone, which silently diverges from
# every bare `docker compose` command on the host: Compose auto-loads
# docker-compose.override.yml, this script did not. On a host whose override
# carries real deployment configuration that divergence is not cosmetic --
# staging's override gates `celery-beat` behind a `production-scheduler`
# profile (added after a staging scheduler wrote to production) and pins
# staging-only object storage. Deploying without it resurrects both.
#
# Production has no override file, so this resolves to exactly the previous
# single-file behaviour there. Set IGNORE_COMPOSE_OVERRIDE=1 to force it.
COMPOSE=(docker compose -f docker-compose.yml)
COMPOSE_FILES_DESC="docker-compose.yml"
if [[ "${IGNORE_COMPOSE_OVERRIDE:-0}" != "1" && -f docker-compose.override.yml ]]; then
  COMPOSE+=(-f docker-compose.override.yml)
  COMPOSE_FILES_DESC+=" + docker-compose.override.yml"
fi

# Restrict the services this deploy touches to the ones the resolved Compose
# config actually declares. Naming a profile-gated service explicitly ACTIVATES
# its profile, so a hardcoded `up -d ... celery-beat` would create the very
# scheduler the staging override exists to suppress. Also drops services a host
# genuinely does not run, instead of failing the deploy on them.
DECLARED_SERVICES=""
load_declared_services() {
  if ! DECLARED_SERVICES="$("${COMPOSE[@]}" config --services 2>&1)"; then
    echo "DEPLOY CONFIG FAILURE: cannot resolve Compose services from ${COMPOSE_FILES_DESC}." >&2
    printf '%s\n' "${DECLARED_SERVICES}" >&2
    return 1
  fi
  DECLARED_SERVICES=" $(tr '\n' ' ' <<<"${DECLARED_SERVICES}") "
}

service_is_declared() {
  [[ "${DECLARED_SERVICES}" == *" $1 "* ]]
}

# Prints the subset of "$@" that the resolved Compose config declares.
declared_subset() {
  local service
  for service in "$@"; do
    if service_is_declared "${service}"; then
      printf '%s\n' "${service}"
    fi
  done
}

set_env_value() {
  local key="$1"
  local value="$2"
  local backup_suffix=".deploy-$$.bak"
  if grep -q "^${key}=" .env; then
    sed -i"${backup_suffix}" "s|^${key}=.*|${key}=${value}|" .env
    rm -f -- ".env${backup_suffix}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> .env
  fi
}

restore_env_value() {
  local key="$1"
  local was_present="$2"
  local value="$3"
  if [[ "${was_present}" == "1" ]]; then
    set_env_value "${key}" "${value}"
  else
    local backup_suffix=".deploy-$$.bak"
    sed -i"${backup_suffix}" "/^${key}=/d" .env
    rm -f -- ".env${backup_suffix}"
  fi
}

pinned_image() { env_value APP_IMAGE; }
pinned_git_sha() { env_value GIT_SHA; }

image_revision() {
  docker image inspect "$1" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
}

validate_image_revision() {
  local image="$1"
  local tag="$2"
  local revision="$3"
  local repo_digests
  local tag_sha
  if [[ ! "${revision}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "IMAGE INTEGRITY FAILURE: ${image} has no full OCI revision label." >&2
    return 1
  fi
  if [[ "${tag}" == sha-* ]]; then
    tag_sha="${tag#sha-}"
    if [[ "${revision:0:${#tag_sha}}" != "${tag_sha}" ]]; then
      echo "IMAGE INTEGRITY FAILURE: tag ${tag} does not match OCI revision ${revision}." >&2
      return 1
    fi
  fi
  if [[ "${tag}" == sha256:* ]]; then
    repo_digests="$(
      docker image inspect "${image}" \
        --format '{{range .RepoDigests}}{{println .}}{{end}}'
    )"
    if ! grep -Fxq "${image}" <<<"${repo_digests}"; then
      echo "IMAGE INTEGRITY FAILURE: pulled image is not pinned to ${image}." >&2
      return 1
    fi
  fi
}

if [[ "${1:-}" == "--status" ]]; then
  echo "pinned:  $(pinned_image)"
  echo "git sha: $(pinned_git_sha)"
  echo "running: $(docker inspect "${APP_CONTAINER}" --format '{{.Config.Image}}' 2>/dev/null || echo 'not running')"
  exit 0
fi

TAG="${1:?usage: deploy.sh <image-tag-or-digest>, e.g. sha-abc1234 or sha256:<64-hex> (or --status)}"
if [[ "${TAG}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  IMAGE="${IMAGE_REPO}@${TAG}"
elif [[ "${TAG}" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
  IMAGE="${IMAGE_REPO}:${TAG}"
else
  echo "IMAGE INTEGRITY FAILURE: image selector must be a valid tag or sha256 digest." >&2
  exit 1
fi
PREV_IMAGE="$(pinned_image)"
PREV_IMAGE_PRESENT="$(grep -q '^APP_IMAGE=' .env && printf 1 || printf 0)"
PREV_GIT_SHA="$(pinned_git_sha)"
PREV_GIT_SHA_PRESENT="$(grep -q '^GIT_SHA=' .env && printf 1 || printf 0)"

# The deployment environment determines the only branch whose GitHub-owned
# decision can authorize this host. An operator cannot point production at a
# green dev-only build, and an ambiguous host fails before any external query
# or database backup.
case "$(env_value APP_ENV):$(env_value SERVER_NAME)" in
  production:dotmac-sub-prod)
    GITHUB_RELEASE_BRANCH="main"
    DEPLOYMENT_TARGET="production"
    if [[ "${SKIP_BACKUP:-0}" == "1" ]]; then
      echo "BACKUP POLICY REJECTED: production does not accept SKIP_BACKUP=1." >&2
      exit 1
    fi
    ;;
  staging:dotmac-sub-staging)
    GITHUB_RELEASE_BRANCH="dev"
    DEPLOYMENT_TARGET="staging"
    if [[ "${SKIP_BACKUP:-0}" == "1" ]]; then
      echo "BACKUP POLICY REJECTED: use the verified staging adapter." >&2
      exit 1
    fi
    ;;
  *)
    echo "GITHUB RELEASE GATE REJECTED: APP_ENV and SERVER_NAME do not identify an approved deployment host." >&2
    exit 1
    ;;
esac

# A public repository needs no token. Private/restricted installations inject
# a read-only token through the process environment or the host's untracked
# .env. Never echo it or place it on the command line.
if [[ -z "${GITHUB_DEPLOY_GATE_TOKEN:-}" ]]; then
  GITHUB_DEPLOY_GATE_TOKEN="$(env_value GITHUB_DEPLOY_GATE_TOKEN)"
fi
export GITHUB_DEPLOY_GATE_TOKEN

if [[ "${IMAGE}" == "${PREV_IMAGE}" ]]; then
  log "Image ${IMAGE} is already pinned — re-running deploy steps idempotently."
fi

log "Deploying ${IMAGE} (currently pinned: ${PREV_IMAGE:-none})"
log "Compose files: ${COMPOSE_FILES_DESC}"

# Cheap, local preconditions first: this gate must be able to refuse without
# having touched anything, including read-only queries.
log "Verifying Nginx warm-handoff contract"
assert_proxy_handoff_contract

log "Resolving declared Compose services"
load_declared_services
mapfile -t APP_SERVICES < <(declared_subset "${APP_SERVICES[@]}")
mapfile -t CELERY_WORKER_SERVICES < <(declared_subset "${CELERY_WORKER_SERVICES[@]}")
if ((${#APP_SERVICES[@]} == 0)); then
  echo "DEPLOY CONFIG FAILURE: none of the app services are declared in ${COMPOSE_FILES_DESC}." >&2
  exit 1
fi
log "Services this deploy will recreate: ${APP_SERVICES[*]}"

log "Verifying image exists on registry"
docker manifest inspect "${IMAGE}" >/dev/null

# Pulling changes only the host's local image cache. The release SHA must be
# known and accepted by GitHub before the backup or any database/application
# mutation begins.
log "Pulling image"
docker pull "${IMAGE}"

log "Verifying image release metadata"
if ! FULL_SHA="$(image_revision "${IMAGE}")"; then
  echo "IMAGE INTEGRITY FAILURE: could not inspect ${IMAGE}." >&2
  exit 1
fi
if ! validate_image_revision "${IMAGE}" "${TAG}" "${FULL_SHA}"; then
  exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null; then
  echo "GITHUB RELEASE GATE REJECTED: ${PYTHON_BIN} is unavailable on the deployment host." >&2
  exit 1
fi
log "Verifying exact ${GITHUB_RELEASE_BRANCH} revision passed GitHub-hosted CI"
GITHUB_RELEASE_REVISION="${FULL_SHA}"
if [[ "${DEPLOYMENT_TARGET}" == "production" ]]; then
  if [[ ! "${TAG}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "PRODUCTION RELEASE GATE REJECTED: production requires an exact OCI digest." >&2
    exit 1
  fi
  if [[ -z "${PRODUCTION_RELEASE_EVIDENCE:-}" || ! -f "${PRODUCTION_RELEASE_EVIDENCE}" ]]; then
    echo "PRODUCTION RELEASE GATE REJECTED: typed production authorization is required." >&2
    exit 1
  fi
  if ! GITHUB_RELEASE_REVISION="$(
    PYTHONPATH="${REPO_DIR}" "${PYTHON_BIN}" -m scripts.release_candidate_evidence \
      verify-production \
      --path "${PRODUCTION_RELEASE_EVIDENCE}" \
      --expected-source-revision "${FULL_SHA}" \
      --expected-image-digest "${TAG}"
  )"; then
    echo "PRODUCTION RELEASE GATE REJECTED: authorization evidence did not match the image." >&2
    exit 1
  fi
fi
"${PYTHON_BIN}" "${REPO_DIR}/scripts/verify_github_release.py" \
  --repository "${GITHUB_RELEASE_REPOSITORY}" \
  --revision "${GITHUB_RELEASE_REVISION}" \
  --branch "${GITHUB_RELEASE_BRANCH}"

# If this script is killed mid-backup -- an SSH session dropping is enough --
# the pg_dump it started does NOT die with it. It keeps running, and the next
# deploy attempt piles another one on top. Take the whole child tree down.
cleanup_children() {
  local rc=$?
  if [[ -n "${BACKUP_PID:-}" ]] && kill -0 "${BACKUP_PID}" 2>/dev/null; then
    kill -TERM "${BACKUP_PID}" 2>/dev/null || true
  fi
  # pg_dump runs inside the DB container via `docker exec`; it survives the
  # exec client, so signal it there too.
  docker exec "${DB_CONTAINER}" sh -c "pkill -f pg_dump" >/dev/null 2>&1 || true
  return $rc
}
trap 'cleanup_children; echo "Deploy interrupted -- backup child terminated, nothing pinned or migrated" >&2; exit 130' INT TERM HUP

BACKUP_MODE="required"
if [[ "${DEPLOYMENT_TARGET}" == "staging" ]]; then
  if [[ "${DEPLOY_BACKUP_MODE:-}" != "skip_staging" ]]; then
    echo "BACKUP POLICY REJECTED: staging deployments must use scripts/deploy_staging.sh." >&2
    exit 1
  fi
  BACKUP_MODE="skip_staging"
elif [[ -n "${PRODUCTION_BACKUP_DECISION_FILE:-}" ]]; then
  if ! BACKUP_MODE="$(
    PYTHONPATH="${REPO_DIR}" "${PYTHON_BIN}" -m scripts.release_backup_policy \
      verify-production-decision --path "${PRODUCTION_BACKUP_DECISION_FILE}"
  )"; then
    echo "BACKUP POLICY REJECTED: production hotfix evidence is invalid." >&2
    exit 1
  fi
fi

if [[ "${BACKUP_MODE}" == "required" ]]; then
  log "Backing up database before migrations"
  # db_backup.sh reads .env from ROOT_DIR, defaulting to its own parent — the
  # code checkout. That is only correct where the deploy dir and the repo are
  # the same directory. On the runner-based production path they are not:
  # REPO_DIR is the ephemeral Actions workspace and DEPLOY_DIR is the pinned
  # host directory that actually holds .env, so the backup aborted with
  # "Missing <workspace>/.env" and no production deploy ever completed.
  ROOT_DIR="${DEPLOY_DIR}" bash "${REPO_DIR}/scripts/db_backup.sh" &
  BACKUP_PID=$!
  wait "${BACKUP_PID}"
  BACKUP_PID=""
elif [[ "${BACKUP_MODE}" == "skip_staging" ]]; then
  log "Skipping staging database backup under the verified staging host policy"
elif [[ "${BACKUP_MODE}" == "skip_production_hotfix" ]]; then
  log "Skipping production backup under verified no-migration hotfix evidence"
else
  echo "BACKUP POLICY REJECTED: unrecognized backup mode ${BACKUP_MODE}." >&2
  exit 1
fi

repin_prev() {
  restore_env_value APP_IMAGE "${PREV_IMAGE_PRESENT}" "${PREV_IMAGE}"
  restore_env_value GIT_SHA "${PREV_GIT_SHA_PRESENT}" "${PREV_GIT_SHA}"
}
# A failure on or after `up -d` (below) may already have replaced/stopped the
# previous containers — repinning .env alone does not bring anything back up.
# Recreate on the restored pin too, same as the health-gate rollback further
# down. `|| true`: this trap must not itself fail partway and lose the
# repin/log that already ran; `set -e`'s normal exit-code propagation still
# reports the ORIGINAL failure once the trap returns.
restore_prev() {
  repin_prev
  if [[ "${PRIMARY_REPLACED}" == "1" && -n "${PREV_IMAGE}" ]]; then
    "${COMPOSE[@]}" up -d "${APP_SERVICES[@]}" || true
    if [[ "${CANDIDATE_STARTED}" == "1" ]]; then
      wait_for_health "${HEALTH_URL}" "Rolled-back primary" || true
    fi
  fi
  cleanup_candidate
}
trap 'restore_prev; echo "Deploy FAILED — APP_IMAGE/GIT_SHA restored to the previous release; previous image brought back up where possible" >&2' ERR

# From here on APP_IMAGE may already be pinned, so an interrupt must restore it
# too -- not just terminate the backup child. (Migrations are NOT reverted; new
# revisions must stay backward-compatible with the previous release.)
trap 'cleanup_children; restore_prev; echo "Deploy interrupted — previous release restored" >&2; exit 130' INT TERM HUP

log "Verifying pre-migration service-extension identity state"
APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
  "${COMPOSE[@]}" run --rm --no-deps app \
  python -m scripts.migration.reconcile_service_extension_duplicates --check

log "Verifying five native service-team pointers and legacy-source retirement"
APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
  "${COMPOSE[@]}" run --rm --no-deps app \
  python -m scripts.migration.retire_legacy_service_team_sources --check

log "Pinning APP_IMAGE=${IMAGE} and GIT_SHA=${FULL_SHA}"
set_env_value APP_IMAGE "${IMAGE}"
set_env_value GIT_SHA "${FULL_SHA}"

# Multi-head safe: sub has hit multi-head states (e.g. the bundles migration that
# merged heads), so use `heads` (plural), never `head`.
log "Applying migrations (alembic upgrade heads)"
run_migrations

log "Verifying database schema contracts"
"${COMPOSE[@]}" run --rm --no-deps app \
  python -m scripts.migration.verify_schema_contracts

log "Verifying enabled integration manifest pins"
"${COMPOSE[@]}" run --rm --no-deps app \
  python -m scripts.integrations.verify_manifest_pins

log "Verifying CRM ticket capability readiness"
"${COMPOSE[@]}" run --rm --no-deps app \
  python -m scripts.integrations.verify_crm_ticket_readiness

log "Starting warm candidate on 127.0.0.1:${CANDIDATE_PORT}"
docker rm -f "${CANDIDATE_CONTAINER}" >/dev/null 2>&1 || true
"${COMPOSE[@]}" run --rm --no-deps -d \
  --name "${CANDIDATE_CONTAINER}" \
  -p "127.0.0.1:${CANDIDATE_PORT}:8001" \
  app >/dev/null
CANDIDATE_STARTED=1
assert_no_source_mount "${CANDIDATE_CONTAINER}"
wait_for_health "${CANDIDATE_HEALTH_URL}" "Warm candidate"

log "Recreating services: ${APP_SERVICES[*]}"
PRIMARY_REPLACED=1
"${COMPOSE[@]}" up -d "${APP_SERVICES[@]}"

log "Verifying deploy integrity (no host source bind-mount shadowing the image)"
if ! assert_no_source_mount; then
  trap - ERR
  exit 1
fi

# Nginx serves the healthy candidate while Compose replaces the primary.
log "Waiting for app health at ${HEALTH_URL} (timeout ${HEALTH_TIMEOUT_SECONDS}s)"
if ! wait_for_health "${HEALTH_URL}" "Primary app"; then
  trap - ERR
  log "Health gate FAILED (${HEALTH_URL} never became healthy) — rolling back to ${PREV_IMAGE:-none}"
  if [[ -n "${PREV_IMAGE}" ]]; then
    restore_prev
    log "Rolled back to ${PREV_IMAGE}. NOTE: migrations from ${TAG} were NOT reverted."
  else
    cleanup_candidate
    log "No previous image recorded — cannot auto-roll-back. Investigate the app container."
  fi
  exit 1
fi

log "Verifying Celery workers and Beat (timeout ${BACKGROUND_RUNTIME_TIMEOUT_SECONDS}s)"
if ! wait_for_background_runtime; then
  trap - ERR
  log "Background runtime health gate FAILED — rolling back to ${PREV_IMAGE:-none}"
  if [[ -n "${PREV_IMAGE}" ]]; then
    restore_prev
    log "Rolled back to ${PREV_IMAGE}. NOTE: migrations from ${TAG} were NOT reverted."
  else
    cleanup_candidate
    log "No previous image recorded — cannot auto-roll-back. Investigate the Celery containers."
  fi
  exit 1
fi

log "Primary is healthy; draining warm candidate"
stop_candidate_gracefully

trap - ERR
log "Deployed ${TAG} successfully (was ${PREV_IMAGE:-none})"

log "Pruning old ${IMAGE_REPO} images (keeping ${IMAGE_RETAIN_COUNT} rollback images)"
IMAGE_REPO="${IMAGE_REPO}" RETAIN_IMAGES="${IMAGE_RETAIN_COUNT}" \
  bash "${REPO_DIR}/scripts/docker_image_retention.sh" || \
  log "Image retention failed; deploy is healthy, but old image cleanup needs attention"
