#!/usr/bin/env bash
# Provision the isolated PostgreSQL audit restore that the prepaid funding
# exporter requires, run the export against it, and destroy it again.
#
#   scripts/one_off/export_prepaid_funding_snapshot.py
#
# refuses to run unless BILLING_AUDIT_EPHEMERAL=1 AND current_database() ends
# in `_audit`. That guard is deliberate: the replay reads every financial fact
# in the system, and it must never do so against the live database. There was
# no tooling to build that restore, which made the whole funding-baseline
# repair path unreachable. This is that tooling.
#
# WHAT THIS DOES NOT TOUCH
#   The production database. This script reads a *dump file* and nothing else.
#   It never connects to the live DB container, and it refuses to name it.
#
# The audit stack is fully isolated:
#   * its own --internal docker network (no route off the host, no route to the
#     application stack),
#   * no published ports,
#   * its own volume, destroyed on teardown,
#   * trust auth, which is safe precisely because of the two lines above and
#     keeps us from minting a credential for a database that lives for minutes.
#
# Usage:
#   prepaid_funding_audit_restore.sh provision [--dump PATH] [--recreate]
#   prepaid_funding_audit_restore.sh export --snapshot-at ISO8601 --source TEXT
#   prepaid_funding_audit_restore.sh status
#   prepaid_funding_audit_restore.sh destroy
#
# See docs/runbooks/PREPAID_FUNDING_AUDIT_RESTORE.md.
set -euo pipefail

AUDIT_DB="${AUDIT_DB:-dotmac_sub_audit}"
AUDIT_CONTAINER="${AUDIT_CONTAINER:-dotmac_sub_funding_audit}"
AUDIT_VOLUME="${AUDIT_VOLUME:-dotmac_sub_funding_audit_data}"
AUDIT_NETWORK="${AUDIT_NETWORK:-dotmac_sub_funding_audit_net}"
AUDIT_IMAGE="${AUDIT_IMAGE:-postgis/postgis:16-3.4-alpine}"
APP_CONTAINER="${APP_CONTAINER:-dotmac_sub_app}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dotmac_sub}"
OUT_DIR="${OUT_DIR:-/var/backups/dotmac_sub/funding_audit}"
ENV_FILE="${ENV_FILE:-/root/dotmac_sub/.env}"
# A 2.4G gzip dump expands to roughly 25G of heap plus indexes. Refuse to start
# a restore that would fill the disk halfway through and leave a wedged volume.
MIN_FREE_GB="${MIN_FREE_GB:-60}"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- guards ----------------------------------------------------------------

# The exporter checks this too, but failing here costs seconds instead of an
# hour-long restore into a database it will then refuse.
[[ "${AUDIT_DB}" == *_audit ]] ||
  die "AUDIT_DB must end in '_audit' (got '${AUDIT_DB}') - the exporter refuses anything else"

# Defence in depth against a copy-paste that points this at the live stack.
for reserved in dotmac_pg_local dotmac_sub_db postgres-local; do
  [[ "${AUDIT_CONTAINER}" == "${reserved}" ]] &&
    die "AUDIT_CONTAINER must not be the live database container (${reserved})"
done

command -v docker >/dev/null 2>&1 || die "docker not found"

# --- helpers ---------------------------------------------------------------

container_exists() { docker inspect "$1" >/dev/null 2>&1; }

container_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == "true" ]]
}

postgres_initialization_complete() {
  docker logs "$1" 2>&1 |
    grep -Eq 'PostgreSQL init process complete|Skipping initialization'
}

latest_dump() {
  # Newest deploy-time backup. These are plain SQL + gzip, --no-owner
  # --no-privileges (see scripts/db_backup.sh), which restores cleanly as any
  # superuser into a fresh database.
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'dotmac_sub_*.sql.gz' \
    -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-
}

require_audit_up() {
  container_running "${AUDIT_CONTAINER}" ||
    die "audit database is not running - run 'provision' first"
}

# --- subcommands -----------------------------------------------------------

cmd_provision() {
  local dump="" recreate=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dump) dump="${2:-}"; shift 2 ;;
      --recreate) recreate=1; shift ;;
      *) die "unknown provision option: $1" ;;
    esac
  done

  if container_exists "${AUDIT_CONTAINER}"; then
    if [[ "${recreate}" -eq 1 ]]; then
      log "Existing audit stack found - destroying first (--recreate)"
      cmd_destroy
    else
      die "audit stack already exists - use --recreate to rebuild, or 'destroy'"
    fi
  fi

  [[ -n "${dump}" ]] || dump="$(latest_dump)"
  [[ -n "${dump}" ]] || die "no dump found in ${BACKUP_DIR} (pass --dump PATH)"
  [[ -f "${dump}" ]] || die "dump not found: ${dump}"
  [[ -s "${dump}" ]] || die "dump is empty: ${dump}"

  local free_gb
  free_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
  [[ "${free_gb}" -ge "${MIN_FREE_GB}" ]] ||
    die "only ${free_gb}G free on / - need >= ${MIN_FREE_GB}G to restore ${dump}"

  log "Dump:      ${dump} ($(du -h "${dump}" | cut -f1))"
  log "Free disk: ${free_gb}G"

  log "Creating isolated network ${AUDIT_NETWORK} (internal: no route off host)"
  docker network create --internal "${AUDIT_NETWORK}" >/dev/null

  log "Starting ${AUDIT_CONTAINER} (${AUDIT_IMAGE}), no published ports"
  docker run -d \
    --name "${AUDIT_CONTAINER}" \
    --network "${AUDIT_NETWORK}" \
    --volume "${AUDIT_VOLUME}:/var/lib/postgresql/data" \
    --env POSTGRES_DB="${AUDIT_DB}" \
    --env POSTGRES_HOST_AUTH_METHOD=trust \
    --label dotmac.ephemeral=funding-audit \
    "${AUDIT_IMAGE}" >/dev/null

  # The official image starts a temporary PostgreSQL server while it runs its
  # initialization scripts. pg_isready succeeds against that temporary server,
  # then the image stops it and starts the final server. Do not begin a restore
  # in that shutdown gap.
  log "Waiting for PostgreSQL initialization to complete"
  local attempt=0
  until postgres_initialization_complete "${AUDIT_CONTAINER}"; do
    attempt=$((attempt + 1))
    [[ "${attempt}" -lt 60 ]] || die "audit database initialization did not complete"
    sleep 2
  done
  log "Waiting for the final PostgreSQL server to accept connections"
  attempt=0
  until docker exec "${AUDIT_CONTAINER}" pg_isready -U postgres -q 2>/dev/null; do
    attempt=$((attempt + 1))
    [[ "${attempt}" -lt 60 ]] || die "final audit database did not become ready"
    sleep 2
  done

  log "Restoring - this takes a while for a multi-GB dump"
  # ON_ERROR_STOP is deliberately NOT set: a prod dump reliably emits benign
  # errors for extensions and roles that do not exist in a bare image. A real
  # failure is caught by the row-count verification below, which is a far
  # better signal than psql's exit status on a plain-format restore.
  gunzip -c "${dump}" |
    docker exec -i "${AUDIT_CONTAINER}" psql -U postgres -d "${AUDIT_DB}" \
      --quiet --output=/dev/null 2>&1 |
    grep -viE 'already exists|does not exist|skipping|^$' | head -40 || true

  local subscribers
  subscribers=$(docker exec "${AUDIT_CONTAINER}" psql -U postgres -d "${AUDIT_DB}" \
    -tAc 'SELECT count(*) FROM subscribers' 2>/dev/null || echo 0)
  [[ "${subscribers}" -gt 0 ]] ||
    die "restore produced no subscriber rows - the audit database is not usable"

  log "Restore verified: ${subscribers} subscribers in ${AUDIT_DB}"
  log "Next: $0 export --snapshot-at <ISO8601> --source <label>"
}

cmd_export() {
  require_audit_up

  local snapshot_at="" source_label="" extra=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --snapshot-at) snapshot_at="${2:-}"; shift 2 ;;
      --source) source_label="${2:-}"; shift 2 ;;
      *) extra+=("$1"); shift ;;
    esac
  done
  [[ -n "${snapshot_at}" ]] || die "--snapshot-at is required (ISO-8601 with offset)"
  [[ -n "${source_label}" ]] || die "--source is required (a traceable label)"

  container_exists "${APP_CONTAINER}" ||
    die "app container ${APP_CONTAINER} not found - needed for its image"
  local image
  image=$(docker inspect -f '{{.Config.Image}}' "${APP_CONTAINER}")

  mkdir -p "${OUT_DIR}"
  chmod 700 "${OUT_DIR}"
  local stamp
  stamp=$(date +"%F_%H%M%S")

  log "Exporter image: ${image}"
  log "Output:         ${OUT_DIR} (blockers_${stamp}.json)"

  # No --signing-key-ref: without one the exporter reaches the signing step
  # only if the cohort is fully reconstructable, and refuses there rather than
  # writing anything. That is what we want for a survey run - the blockers
  # file is written BEFORE that point. Exit 2 means "blocked, nothing sealed",
  # which is the expected outcome while accounts remain quarantined.
  #
  # --allow-primary is required and correct here: the audit restore is its own
  # primary. That flag never authorises the production primary, because
  # _require_ephemeral_postgres has already rejected any non-_audit database.
  set +e
  docker run --rm \
    --network "${AUDIT_NETWORK}" \
    --env-file "${ENV_FILE}" \
    --env BILLING_AUDIT_EPHEMERAL=1 \
    --env REPO_DIR=/app \
    --env PYTHON_BIN=python \
    --env "DATABASE_URL=postgresql+psycopg://postgres@${AUDIT_CONTAINER}:5432/${AUDIT_DB}" \
    --volume "${OUT_DIR}:/out" \
    --workdir /app \
    "${image}" \
    bash /app/scripts/run_repo_module.sh \
      scripts.one_off.export_prepaid_funding_snapshot \
      --snapshot-at "${snapshot_at}" \
      --source "${source_label}" \
      --out "/out/manifest_${stamp}.json" \
      --blockers-out "/out/blockers_${stamp}.json" \
      --allow-primary \
      "${extra[@]}"
  local rc=$?
  set -e

  case "${rc}" in
    0) log "Sealed manifest written: ${OUT_DIR}/manifest_${stamp}.json" ;;
    2) log "BLOCKED (expected while accounts are quarantined) - no manifest sealed" ;;
    *) die "exporter failed with exit ${rc}" ;;
  esac

  if [[ -f "${OUT_DIR}/blockers_${stamp}.json" ]]; then
    log "Blocker reason codes: ${OUT_DIR}/blockers_${stamp}.json"
  else
    log "WARNING: no blockers file was written"
  fi
  return 0
}

cmd_status() {
  if container_running "${AUDIT_CONTAINER}"; then
    local rows
    rows=$(docker exec "${AUDIT_CONTAINER}" psql -U postgres -d "${AUDIT_DB}" \
      -tAc 'SELECT count(*) FROM subscribers' 2>/dev/null || echo '?')
    log "audit stack RUNNING - ${AUDIT_DB}, ${rows} subscribers"
    docker ps --filter "name=${AUDIT_CONTAINER}" \
      --format '  {{.Names}}\t{{.Image}}\t{{.Status}}'
  elif container_exists "${AUDIT_CONTAINER}"; then
    log "audit stack exists but is STOPPED"
  else
    log "audit stack not present"
  fi
}

cmd_destroy() {
  # This is why the stack carries dedicated names: teardown can be
  # unconditional without ever risking an application container or volume.
  log "Removing ${AUDIT_CONTAINER}"
  docker rm -f "${AUDIT_CONTAINER}" >/dev/null 2>&1 || true
  log "Removing volume ${AUDIT_VOLUME} (destroys the restored copy)"
  docker volume rm "${AUDIT_VOLUME}" >/dev/null 2>&1 || true
  log "Removing network ${AUDIT_NETWORK}"
  docker network rm "${AUDIT_NETWORK}" >/dev/null 2>&1 || true
  log "Audit stack destroyed. Exported artifacts under ${OUT_DIR} are kept."
}

case "${1:-}" in
  provision) shift; cmd_provision "$@" ;;
  export)    shift; cmd_export "$@" ;;
  status)    shift; cmd_status "$@" ;;
  destroy)   shift; cmd_destroy "$@" ;;
  *)
    sed -n '1,31p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
