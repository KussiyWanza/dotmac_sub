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

run_repo_module() {
  REPO_DIR="${REPO_DIR}" PYTHON_BIN="${PYTHON_BIN}" \
    bash "${REPO_DIR}/scripts/run_repo_module.sh" "$@"
}
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
CANDIDATE_PORT="${CANDIDATE_PORT:-18002}"
CANDIDATE_HEALTH_URL="${CANDIDATE_HEALTH_URL:-http://127.0.0.1:${CANDIDATE_PORT}/health}"
CANDIDATE_DRAIN_SECONDS="${CANDIDATE_DRAIN_SECONDS:-2}"
BACKGROUND_RUNTIME_TIMEOUT_SECONDS="${BACKGROUND_RUNTIME_TIMEOUT_SECONDS:-90}"
BACKGROUND_STABILITY_SECONDS="${BACKGROUND_STABILITY_SECONDS:-15}"
CELERY_INSPECT_TIMEOUT_SECONDS="${CELERY_INSPECT_TIMEOUT_SECONDS:-5}"
# Every service that runs the app image and must be recreated on a new build.
#
# Note what is NOT here, because it is load-bearing and easy to miss:
# postgres-local, redis-local, nominatim, freeradius, genieacs and the metrics
# services are absent. A deploy never recreates them. So a change to one of
# those service definitions -- a published port, a volume, a command flag --
# does NOT take effect by deploying. The repository looking fixed is not the
# same as the host being fixed.
#
# For PUBLISHED PORTS there is now a managed path for exactly this, and it is
# the one to use -- not a hand `up -d` on the box:
#
#   scripts/reconcile_published_ports.sh --service <name> --environment <env>
#
# It applies deploy/published_ports.toml, proves the environment value is what
# compose actually resolves BEFORE recreating, and re-reads the host's actual
# listeners afterwards. See docs/runbooks/PUBLISHED_PORT_RECONCILE.md and
# ADR-0014. Everything else about these services still needs a deliberate,
# separately scheduled recreate.
APP_SERVICES=(app celery-worker celery-worker-bandwidth celery-worker-ingestion \
  celery-worker-monitoring celery-worker-notifications-immediate \
  celery-worker-notifications celery-worker-billing \
  celery-worker-tr069 celery-beat \
  bandwidth-poller syslog-listener)
CELERY_WORKER_SERVICES=(celery-worker celery-worker-bandwidth \
  celery-worker-ingestion celery-worker-monitoring \
  celery-worker-notifications-immediate celery-worker-notifications \
  celery-worker-billing celery-worker-tr069)
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
  local watched_container="${3:-}"
  local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
  local state
  while true; do
    if curl -fsS --connect-timeout "${HEALTH_CURL_TIMEOUT}" \
      --max-time "${HEALTH_CURL_TIMEOUT}" -o /dev/null "${url}" 2>/dev/null; then
      return 0
    fi
    if [[ -n "${watched_container}" ]]; then
      state="$(docker inspect "${watched_container}" \
        --format '{{.State.Status}}' 2>/dev/null || true)"
      if [[ "${state}" != "running" ]]; then
        echo "${label} stopped before becoming healthy: ${state:-unavailable}" >&2
        return 1
      fi
    fi
    if ((SECONDS >= deadline)); then
      echo "${label} health gate failed: ${url}" >&2
      return 1
    fi
    sleep 5
  done
}

report_candidate_failure() {
  echo "Warm candidate container state:" >&2
  if ! docker inspect "${CANDIDATE_CONTAINER}" \
    --format 'status={{.State.Status}} exit_code={{.State.ExitCode}} oom_killed={{.State.OOMKilled}} restarting={{.State.Restarting}} error={{json .State.Error}}' \
    >&2; then
    echo "  unavailable (container may have exited before inspection)" >&2
  fi
  echo "Warm candidate logs (last 200 lines):" >&2
  if ! docker logs --tail 200 "${CANDIDATE_CONTAINER}" >&2; then
    echo "  unavailable (container produced no readable logs)" >&2
  fi
}

require_candidate_health() {
  if wait_for_health \
    "${CANDIDATE_HEALTH_URL}" "Warm candidate" "${CANDIDATE_CONTAINER}"; then
    return 0
  fi
  # Capture bounded diagnostics before the ERR trap removes the failed
  # candidate and restores the previous release. Application logging rules
  # prohibit secret material, so this emits runtime logs rather than env/config.
  report_candidate_failure
  return 1
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
  local -a queue_requirements=()
  local required_queue=""

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
    case "${service}" in
      celery-worker-notifications-immediate)
        required_queue="notifications_immediate"
        ;;
      celery-worker-notifications)
        required_queue="notifications"
        ;;
      *)
        required_queue=""
        ;;
    esac
    if [[ -n "${required_queue}" ]]; then
      queue_requirements+=(--require-queue "celery@${hostname}=${required_queue}")
    fi
  done

  if ! docker exec "${probe_container}" \
    python -m scripts.verify_celery_workers \
    --timeout "${CELERY_INSPECT_TIMEOUT_SECONDS}" \
    "${queue_requirements[@]}" \
    "${expected_nodes[@]}" >/dev/null 2>&1; then
    echo "BACKGROUND RUNTIME FAILURE: a Celery worker did not answer ping or consume its required queue." >&2
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

assert_candidate_port_available() {
  local port_owner

  docker rm -f "${CANDIDATE_CONTAINER}" >/dev/null 2>&1 || true
  port_owner="$(
    docker ps --format '{{.Names}} {{.Ports}}' \
      | grep -E "(^|[ ,])127\\.0\\.0\\.1:${CANDIDATE_PORT}->|(^|[ ,])0\\.0\\.0\\.0:${CANDIDATE_PORT}->|\\[::\\]:${CANDIDATE_PORT}->" \
      || true
  )"
  if [[ -n "${port_owner}" ]]; then
    echo "DEPLOY AVAILABILITY FAILURE: candidate port 127.0.0.1:${CANDIDATE_PORT} is already allocated." >&2
    printf '%s\n' "${port_owner}" | sed 's/^/  owner: /' >&2
    echo "Set CANDIDATE_PORT to a free warm-candidate port before retrying." >&2
    return 1
  fi
  if command -v ss >/dev/null \
    && ss -H -ltn | awk '{print $4}' | grep -Eq "(^|:|\\])${CANDIDATE_PORT}$"; then
    echo "DEPLOY AVAILABILITY FAILURE: candidate port 127.0.0.1:${CANDIDATE_PORT} is already in use by a host process." >&2
    echo "Set CANDIDATE_PORT to a free warm-candidate port before retrying." >&2
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

# --- Module database prerequisites ----------------------------------------
#
# Schemas and cluster roles are privileged DEPLOYMENT prerequisites, not
# Alembic effects: the restricted migration role deliberately never holds
# database-level CREATE (ADR-0011). Until 2026-08-31 the repair leg below
# short-circuited whenever no elevated credential was configured, and the
# deploy carried on to a verification it could not possibly satisfy:
# "nothing to do" and "nothing can be done" both returned 0.
#
# What that cost: 20e1fb9a6 (#2819) composed the dotmac-inbox lineage, which
# added mod_inbox to the derived contract. No environment had it. Two staging
# deploys then failed at the verification gate. Production was never attempted
# - the gate stopped the pipeline first, which is the gate working - and it
# remains unprovisioned to this day.
#
# The leg now reports one of three outcomes and never conflates them:
#   already_satisfied - the contract holds; nothing was written
#   repaired          - the managed credential brought it to contract
#   blocked           - repair is required and cannot proceed; the deploy stops
PREREQUISITE_OUTCOME="unknown"

# Where the deployment adapter finds the dedicated schema-creation credential.
# Root-owned, 0400, never readable by the app, the runner or dotmac_app. The
# password is only ever read by libpq from this file - never the URL, argv,
# environment or log.
SCHEMA_BOOTSTRAP_PGPASS="${SCHEMA_BOOTSTRAP_PGPASS:-$(env_value SCHEMA_BOOTSTRAP_PGPASS)}"
SCHEMA_BOOTSTRAP_PGPASS="${SCHEMA_BOOTSTRAP_PGPASS:-/etc/dotmac/sub/schema-bootstrap.pgpass}"
SCHEMA_BOOTSTRAP_PGPASS_MOUNT="${SCHEMA_BOOTSTRAP_PGPASS_MOUNT:-/run/secrets/schema-bootstrap.pgpass}"
SCHEMA_BOOTSTRAP_URL="${SCHEMA_BOOTSTRAP_URL:-$(env_value SCHEMA_BOOTSTRAP_URL)}"
# The fixed user that owns the credential and runs the deployment adapter.
# Overridable because a host may run the adapter as a dedicated non-root
# account; root is the default and what production uses.
SCHEMA_BOOTSTRAP_OWNER="${SCHEMA_BOOTSTRAP_OWNER:-root}"

module_prerequisites_satisfied() {
  APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
    "${COMPOSE[@]}" run --rm --no-deps app sh -c \
    'MIGRATION_DATABASE_URL="$DATABASE_URL" python scripts/bootstrap_commercial_module_prereqs.py --verify-only' \
    >/dev/null 2>&1
}

# Every refusal below names the exact thing that was wrong. A bare `test` that
# exits 1 with no output is how the 2026-08-31 production repair became
# undiagnosable from its own log.
schema_bootstrap_credential_available() {
  local ok=0
  local authority userinfo

  if [[ -z "${SCHEMA_BOOTSTRAP_URL}" ]]; then
    echo "PREREQUISITE PREFLIGHT: SCHEMA_BOOTSTRAP_URL is not set (expected a passwordless postgresql:// URL for the dedicated bootstrap role)." >&2
    ok=1
  else
    # Inspect ONLY the userinfo. A naive `*:*@*` test also matches the scheme's
    # own colon in `postgresql://`, which would reject every correct URL.
    authority="${SCHEMA_BOOTSTRAP_URL#*://}"
    if [[ "${authority}" == *@* ]]; then
      userinfo="${authority%%@*}"
      if [[ "${userinfo}" == *:* ]]; then
        echo "PREREQUISITE PREFLIGHT: SCHEMA_BOOTSTRAP_URL carries an inline password; the credential must come from the pgpass file instead." >&2
        ok=1
      fi
    fi
  fi

  if [[ ! -e "${SCHEMA_BOOTSTRAP_PGPASS}" ]]; then
    echo "PREREQUISITE PREFLIGHT: credential file ${SCHEMA_BOOTSTRAP_PGPASS} does not exist." >&2
    ok=1
  elif [[ ! -f "${SCHEMA_BOOTSTRAP_PGPASS}" ]]; then
    echo "PREREQUISITE PREFLIGHT: ${SCHEMA_BOOTSTRAP_PGPASS} is not a regular file." >&2
    ok=1
  else
    local mode owner
    mode="$(stat -c '%a' "${SCHEMA_BOOTSTRAP_PGPASS}" 2>/dev/null || echo '?')"
    owner="$(stat -c '%U' "${SCHEMA_BOOTSTRAP_PGPASS}" 2>/dev/null || echo '?')"
    if [[ "${mode}" != "400" ]]; then
      echo "PREREQUISITE PREFLIGHT: ${SCHEMA_BOOTSTRAP_PGPASS} has mode ${mode}; expected 400 (libpq refuses a group- or world-readable pgpass)." >&2
      ok=1
    fi
    if [[ "${owner}" != "${SCHEMA_BOOTSTRAP_OWNER}" ]]; then
      echo "PREREQUISITE PREFLIGHT: ${SCHEMA_BOOTSTRAP_PGPASS} is owned by ${owner}; expected ${SCHEMA_BOOTSTRAP_OWNER}." >&2
      ok=1
    fi
    if [[ ! -s "${SCHEMA_BOOTSTRAP_PGPASS}" ]]; then
      echo "PREREQUISITE PREFLIGHT: ${SCHEMA_BOOTSTRAP_PGPASS} is empty." >&2
      ok=1
    fi
  fi

  return "${ok}"
}

run_database_prerequisite_bootstrap() {
  local bootstrap_url persisted_url

  # An elevated DSN persisted in the deploy directory's .env would arm
  # auto-repair on EVERY deployment, silently and permanently: `env_value`
  # greps that file, so the old
  #   "${BOOTSTRAP_DATABASE_URL:-$(env_value BOOTSTRAP_DATABASE_URL)}"
  # made "nobody has typed it into .env" the entire safety property. Refuse it
  # instead of depending on that. A one-off operator provisioning run passes
  # the DSN in the PROCESS environment, where it lasts exactly one invocation.
  persisted_url="$(env_value BOOTSTRAP_DATABASE_URL)"
  if [[ -n "${persisted_url}" ]]; then
    PREREQUISITE_OUTCOME="blocked"
    log "PREREQUISITE OUTCOME: blocked"
    cat >&2 <<'REFUSAL'
DEPLOY REFUSED: BOOTSTRAP_DATABASE_URL is set in the deploy directory's .env.

A persisted elevated DSN turns every deployment into an auto-repairing one,
with superuser-shaped rights, for as long as the line stays in the file. The
managed repair leg uses the least-privilege dotmac_schema_bootstrap credential
from a root-owned 0400 pgpass file instead, and that credential carries no
password in the URL at all.

Remove the line from .env. For a one-off elevated provisioning run, pass
BOOTSTRAP_DATABASE_URL in the environment of that single invocation.
REFUSAL
    exit 1
  fi

  # Operator provisioning path: an explicitly supplied elevated connection may
  # create cluster ROLES as well as schemas. Environment-only, so it is one
  # deliberate invocation rather than a standing configuration.
  bootstrap_url="${BOOTSTRAP_DATABASE_URL:-}"
  if [[ -n "${bootstrap_url}" ]]; then
    log "Repairing commercial module database prerequisites with the elevated bootstrap connection"
    BOOTSTRAP_DATABASE_URL="${bootstrap_url}" APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
      "${COMPOSE[@]}" run --rm --no-deps -e BOOTSTRAP_DATABASE_URL app \
      python scripts/bootstrap_commercial_module_prereqs.py --repair

    log "Repairing outbox dispatcher database prerequisites with the elevated bootstrap connection"
    BOOTSTRAP_DATABASE_URL="${bootstrap_url}" APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
      "${COMPOSE[@]}" run --rm --no-deps -e BOOTSTRAP_DATABASE_URL app \
      python scripts/bootstrap_outbox_dispatcher_roles.py --repair

    PREREQUISITE_OUTCOME="repaired"
    log "PREREQUISITE OUTCOME: repaired (elevated operator connection)"
    return 0
  fi

  log "Checking module database prerequisites with the restricted migration connection"
  if module_prerequisites_satisfied; then
    PREREQUISITE_OUTCOME="already_satisfied"
    log "PREREQUISITE OUTCOME: already_satisfied (contract holds; nothing written)"
    return 0
  fi

  log "Module database prerequisites are NOT satisfied; the managed repair leg is required"
  if ! schema_bootstrap_credential_available; then
    PREREQUISITE_OUTCOME="blocked"
    log "PREREQUISITE OUTCOME: blocked"
    cat >&2 <<'REFUSAL'
DEPLOY REFUSED: module database prerequisites need repair, but the dedicated
schema-bootstrap credential is unavailable (see PREREQUISITE PREFLIGHT lines
above for exactly which check failed).

This deploy will NOT continue to migrations. Refusing here is the point: the
previous behaviour was to carry on and fail the verification step with no
statement of what was missing or why nothing had been attempted.

To resolve, provision the dedicated role and its held credential on this host:
  - role dotmac_schema_bootstrap (NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS NOINHERIT), CONNECT + CREATE on this database
    only, member of dotmac_app without admin option
  - credential materialised root-owned 0400 at the pgpass path above
See docs/runbooks/PRODUCTION_DEPLOYMENT.md, "Module database prerequisites".
REFUSAL
    exit 1
  fi

  log "Repairing module schemas with the dedicated schema-bootstrap credential"
  APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
    "${COMPOSE[@]}" run --rm --no-deps --user 0:0 \
    -v "${SCHEMA_BOOTSTRAP_PGPASS}:${SCHEMA_BOOTSTRAP_PGPASS_MOUNT}:ro" \
    -e PGPASSFILE="${SCHEMA_BOOTSTRAP_PGPASS_MOUNT}" \
    -e BOOTSTRAP_DATABASE_URL="${SCHEMA_BOOTSTRAP_URL}" \
    app python scripts/bootstrap_commercial_module_prereqs.py --repair-schemas

  PREREQUISITE_OUTCOME="repaired"
  log "PREREQUISITE OUTCOME: repaired (managed schema-bootstrap credential)"
}

verify_database_prerequisites() {
  log "Verifying commercial module prerequisites with the restricted migration connection"
  APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
    "${COMPOSE[@]}" run --rm --no-deps app sh -c \
    'MIGRATION_DATABASE_URL="$DATABASE_URL" python scripts/bootstrap_commercial_module_prereqs.py --verify-only'

  log "Verifying outbox dispatcher prerequisites with the restricted migration connection"
  APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
    "${COMPOSE[@]}" run --rm --no-deps app sh -c \
    'MIGRATION_DATABASE_URL="$DATABASE_URL" python scripts/bootstrap_outbox_dispatcher_roles.py --verify-only'
}

database_heads() {
  local backup_db_user
  local backup_db_name
  local database_url

  backup_db_user="${DB_BACKUP_DB_USER:-$(env_value DB_BACKUP_DB_USER)}"
  backup_db_user="${backup_db_user:-postgres}"
  backup_db_name="${DB_BACKUP_DB_NAME:-$(env_value DB_BACKUP_DB_NAME)}"
  if [[ -z "${backup_db_name}" ]]; then
    database_url="$(env_value DATABASE_URL)"
    backup_db_name="${database_url##*/}"
    backup_db_name="${backup_db_name%%\?*}"
  fi
  if [[ -z "${backup_db_name}" || "${backup_db_name}" == "$(env_value DATABASE_URL)" ]]; then
    echo "RESUME POLICY REJECTED: cannot resolve database name for alembic head verification." >&2
    return 1
  fi
  docker exec "${DB_CONTAINER}" psql -X -A -t -U "${backup_db_user}" \
    -d "${backup_db_name}" \
    -c 'SELECT version_num FROM alembic_version ORDER BY version_num'
}

candidate_heads() {
  docker run --rm --entrypoint alembic "${IMAGE}" heads \
    | sed -E 's/[[:space:]].*$//' \
    | grep -E '^[A-Za-z0-9_.-]+$'
}

verify_post_migration_resume() {
  local prior_run_id="${PRODUCTION_DEPLOY_RESUME_FAILED_RUN_ID:-}"
  local backup_path="${PRODUCTION_DEPLOY_RESUME_BACKUP_PATH:-}"
  local authorization_run_id="${PRODUCTION_DEPLOY_RESUME_AUTHORIZATION_RUN_ID:-}"
  local current_app_image
  local -a database_head_args=()
  local -a candidate_head_args=()
  local -a database_head_values=()
  local -a candidate_head_values=()
  local head

  [[ "${PRODUCTION_DEPLOY_RESUME_AFTER_MIGRATION:-0}" == "1" ]] || return 1
  [[ "${DEPLOYMENT_TARGET}" == "production" ]] || {
    echo "RESUME POLICY REJECTED: post-migration resume is production-only." >&2
    return 1
  }
  [[ "${prior_run_id}" =~ ^[0-9]+$ && "${prior_run_id}" -gt 0 ]] || {
    echo "RESUME POLICY REJECTED: prior failed deployment run ID is required." >&2
    return 1
  }
  [[ "${authorization_run_id}" =~ ^[0-9]+$ && "${authorization_run_id}" -gt 0 ]] || {
    echo "RESUME POLICY REJECTED: authorization run ID is required." >&2
    return 1
  }
  current_app_image="$(
    docker inspect "${APP_CONTAINER}" --format '{{.Config.Image}}' 2>/dev/null || true
  )"
  mapfile -t database_head_values < <(database_heads)
  mapfile -t candidate_head_values < <(candidate_heads)
  if ((${#database_head_values[@]} == 0 || ${#candidate_head_values[@]} == 0)); then
    echo "RESUME POLICY REJECTED: database or candidate Alembic heads could not be proven." >&2
    return 1
  fi
  for head in "${database_head_values[@]}"; do
    database_head_args+=(--database-head "${head}")
  done
  for head in "${candidate_head_values[@]}"; do
    candidate_head_args+=(--candidate-head "${head}")
  done
  run_repo_module scripts.deploy_resume_policy verify-post-migration \
    --prior-failed-run-id "${prior_run_id}" \
    --authorization-run-id "${authorization_run_id}" \
    --expected-authorization-run-id "${authorization_run_id}" \
    --backup-path "${backup_path}" \
    --candidate-digest "${TAG}" \
    --authorized-digest "${TAG}" \
    "${database_head_args[@]}" \
    "${candidate_head_args[@]}" \
    --current-app-image "${current_app_image}" \
    --previous-image "${PREV_IMAGE}" \
    --candidate-image "${IMAGE}"
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
# The base Compose contract belongs to the authorized release checkout, not the
# persistent host deployment directory. Production intentionally separates
# those locations: REPO_DIR is the exact Actions checkout while DEPLOY_DIR owns
# only host state such as .env and an optional host-specific override. Reading
# the base file from DEPLOY_DIR let a stale checkout omit a newly required
# worker even while the host ran the newer image.
#
# This used to be `-f docker-compose.yml` alone, which silently diverges from
# every bare `docker compose` command on the host: Compose auto-loads
# docker-compose.override.yml, this script did not. On a host whose override
# carries real deployment configuration that divergence is not cosmetic --
# staging's override gates `celery-beat` behind a `production-scheduler`
# profile (added after a staging scheduler wrote to production) and pins
# staging-only object storage. Deploying without it resurrects both.
#
# Production DOES have an override file: /root/dotmac_sub/docker-compose.override.yml,
# 21 services, measured 2026-08-31. (An earlier version of this comment said it
# had none, which was wrong and is the sort of thing people reason from.) It
# tunes resources and `command` for postgres-local, redis-local and the workers,
# and publishes team-inbox-smtp on 127.0.0.1:18001. It declares no `ports:` for
# postgres-local, so the base file's binding is what applies there.
# Set IGNORE_COMPOSE_OVERRIDE=1 to force single-file behaviour.
RELEASE_COMPOSE_FILE="${REPO_DIR}/docker-compose.yml"
HOST_COMPOSE_OVERRIDE="${DEPLOY_DIR}/docker-compose.override.yml"
if [[ ! -f "${RELEASE_COMPOSE_FILE}" ]]; then
  echo "DEPLOY CONFIG FAILURE: authorized release has no ${RELEASE_COMPOSE_FILE}." >&2
  exit 1
fi
COMPOSE=(docker compose --project-directory "${DEPLOY_DIR}" \
  --env-file "${DEPLOY_DIR}/.env" -f "${RELEASE_COMPOSE_FILE}")
COMPOSE_FILES_DESC="${RELEASE_COMPOSE_FILE}"
if [[ "${IGNORE_COMPOSE_OVERRIDE:-0}" != "1" && -f "${HOST_COMPOSE_OVERRIDE}" ]]; then
  COMPOSE+=(-f "${HOST_COMPOSE_OVERRIDE}")
  COMPOSE_FILES_DESC+=" + ${HOST_COMPOSE_OVERRIDE}"
fi

# Restrict optional services this deploy touches to the ones the resolved
# Compose config actually declares. Naming a profile-gated service explicitly
# ACTIVATES its profile, so a hardcoded `up -d ... celery-beat` would create
# the very scheduler the staging override exists to suppress.
#
# Worker services are not optional on a production host. Dropping a missing
# queue consumer here lets beat keep publishing tasks to an unconsumed Redis
# queue while the deploy still reports green.
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

assert_required_services_declared() {
  local service
  local missing=()
  for service in "$@"; do
    if ! service_is_declared "${service}"; then
      missing+=("${service}")
    fi
  done
  if ((${#missing[@]} > 0)); then
    echo "DEPLOY CONFIG FAILURE: required Compose service(s) missing from ${COMPOSE_FILES_DESC}: ${missing[*]}" >&2
    return 1
  fi
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

image_source_tree() {
  docker image inspect "$1" \
    --format '{{index .Config.Labels "io.dotmac.release.source-tree"}}'
}

validate_release_source_tree() {
  local image="$1"
  local image_tree="$2"
  local release_tree
  if [[ ! "${image_tree}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "DEPLOY INTEGRITY FAILURE: ${image} has no full source-tree label." >&2
    return 1
  fi
  if ! release_tree="$(git -C "${REPO_DIR}" rev-parse 'HEAD^{tree}' 2>/dev/null)"; then
    echo "DEPLOY INTEGRITY FAILURE: ${REPO_DIR} is not an authorized Git checkout." >&2
    return 1
  fi
  if [[ "${release_tree}" != "${image_tree}" ]]; then
    echo "DEPLOY INTEGRITY FAILURE: release Compose tree ${release_tree} does not match image source tree ${image_tree}." >&2
    return 1
  fi
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
    GITHUB_RELEASE_BRANCH="main"
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
log "Verifying warm-candidate port availability"
assert_candidate_port_available

log "Resolving declared Compose services"
load_declared_services
assert_required_services_declared app "${CELERY_WORKER_SERVICES[@]}"
mapfile -t APP_SERVICES < <(declared_subset "${APP_SERVICES[@]}")
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
if ! IMAGE_SOURCE_TREE="$(image_source_tree "${IMAGE}")"; then
  echo "DEPLOY INTEGRITY FAILURE: could not inspect ${IMAGE} source tree." >&2
  exit 1
fi
if ! validate_release_source_tree "${IMAGE}" "${IMAGE_SOURCE_TREE}"; then
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
  PRODUCTION_VERIFY_ARGS=(
    verify-production
    --path "${PRODUCTION_RELEASE_EVIDENCE}"
    --expected-source-revision "${FULL_SHA}"
    --expected-image-digest "${TAG}"
  )
  if [[ -n "${PRODUCTION_DEPLOY_RESUME_AUTHORIZATION_RUN_ID:-}" ]]; then
    PRODUCTION_VERIFY_ARGS+=(
      --expected-authorization-run-id "${PRODUCTION_DEPLOY_RESUME_AUTHORIZATION_RUN_ID}"
    )
  fi
  if ! GITHUB_RELEASE_REVISION="$(
    run_repo_module scripts.release_candidate_evidence "${PRODUCTION_VERIFY_ARGS[@]}"
  )"; then
    echo "PRODUCTION RELEASE GATE REJECTED: authorization evidence did not match the image." >&2
    exit 1
  fi
fi
"${PYTHON_BIN}" "${REPO_DIR}/scripts/verify_github_release.py" \
  --repository "${GITHUB_RELEASE_REPOSITORY}" \
  --revision "${GITHUB_RELEASE_REVISION}" \
  --branch "${GITHUB_RELEASE_BRANCH}"

run_database_prerequisite_bootstrap
verify_database_prerequisites

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
    run_repo_module scripts.release_backup_policy \
      verify-production-decision --path "${PRODUCTION_BACKUP_DECISION_FILE}"
  )"; then
    echo "BACKUP POLICY REJECTED: production hotfix evidence is invalid." >&2
    exit 1
  fi
elif [[ "${PRODUCTION_DEPLOY_RESUME_AFTER_MIGRATION:-0}" == "1" ]]; then
  if ! BACKUP_MODE="$(verify_post_migration_resume)"; then
    echo "BACKUP POLICY REJECTED: production post-migration resume evidence is invalid." >&2
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
  run_database_backup() {
    DB_BACKUP_BASENAME="${DB_BACKUP_BASENAME:-dotmac_sub}" \
      DB_BACKUP_DIR="${DB_BACKUP_DIR:-/var/backups/dotmac_sub/deployments}" \
      DB_BACKUP_LEGACY_DIR="${DB_BACKUP_LEGACY_DIR:-/var/backups/dotmac_sub}" \
      DB_BACKUP_RETENTION_PREFIX="${DB_BACKUP_RETENTION_PREFIX:-dotmac_sub_run_}" \
      DB_BACKUP_RETENTION_COUNT="${DB_BACKUP_RETENTION_COUNT:-5}" \
      ROOT_DIR="${DEPLOY_DIR}" bash "${REPO_DIR}/scripts/db_backup.sh"
  }
  if [[ "${DEPLOYMENT_TARGET}" == "production" && -n "${DEPLOY_RUN_ID:-}" && -z "${DB_BACKUP_BASENAME:-}" ]]; then
    DB_BACKUP_BASENAME="dotmac_sub_run_${DEPLOY_RUN_ID}" run_database_backup &
  else
    run_database_backup &
  fi
  BACKUP_PID=$!
  wait "${BACKUP_PID}"
  BACKUP_PID=""
elif [[ "${BACKUP_MODE}" == "skip_staging" ]]; then
  log "Skipping staging database backup under the verified staging host policy"
elif [[ "${BACKUP_MODE}" == "skip_production_hotfix" ]]; then
  log "Skipping production backup under verified no-migration hotfix evidence"
elif [[ "${BACKUP_MODE}" == "skip_production_post_migration_resume" ]]; then
  log "Skipping production backup under verified post-migration resume evidence"
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

log "Verifying mandatory OpenBao boot secrets"
APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
  "${COMPOSE[@]}" run --rm --no-deps app \
  python -m scripts.setup.verify_openbao_boot_secrets

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
if [[ "${BACKUP_MODE}" == "skip_production_post_migration_resume" ]]; then
  log "Skipping migrations under verified post-migration resume evidence"
else
  run_migrations
fi

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
# Do not use `--rm`: an early process exit must leave its state and bounded log
# stream available to `report_candidate_failure` before rollback cleanup.
"${COMPOSE[@]}" run --no-deps -d \
  --name "${CANDIDATE_CONTAINER}" \
  -p "127.0.0.1:${CANDIDATE_PORT}:8001" \
  app >/dev/null
CANDIDATE_STARTED=1
assert_no_source_mount "${CANDIDATE_CONTAINER}"
require_candidate_health

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
# Part of the deployment receipt: a promotion should be able to show which
# of the three prerequisite outcomes this release actually took.
log "DEPLOY RECEIPT: tag=${TAG} revision=${FULL_SHA} prerequisites=${PREREQUISITE_OUTCOME}"

log "Pruning old ${IMAGE_REPO} images (keeping ${IMAGE_RETAIN_COUNT} rollback images)"
if ! IMAGE_REPO="${IMAGE_REPO}" RETAIN_IMAGES="${IMAGE_RETAIN_COUNT}" \
  bash "${REPO_DIR}/scripts/docker_image_retention.sh"; then
  log "Image retention failed; deploy is healthy, but old image cleanup needs attention"
  exit 1
fi
log "Image retention completed and verified"
