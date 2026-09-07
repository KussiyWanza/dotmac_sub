#!/usr/bin/env bash
# Pre-migration DB backup for dotmac_sub (a fast local dump for rollback).
#
# Separate from the offsite rclone backups in scripts/backup/* — this is the
# quick, on-box snapshot deploy.sh takes right before `alembic upgrade heads`,
# so a bad migration can be restored without waiting on remote storage.
#
# Dumps via DATABASE_URL from .env, run inside the DB container (which ships
# pg_dump and can reach the DB whether it's postgres-local or external).
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Capture explicit shell-env overrides BEFORE sourcing .env, so the precedence
# is: shell env > .env > default. These used to be resolved here with their
# defaults already baked in, which made a DB_CONTAINER=... line in .env dead
# config: the default had won before .env was ever read, and the deploy aborted
# at the backup step on any host whose DB container is not `dotmac_pg_local`.
_ENV_DB_CONTAINER="${DB_CONTAINER:-}"
_ENV_DB_BACKUP_ROOT="${DB_BACKUP_ROOT:-}"
_ENV_DB_BACKUP_DIR="${DB_BACKUP_DIR:-}"
_ENV_DB_BACKUP_LEGACY_DIR="${DB_BACKUP_LEGACY_DIR:-}"
_ENV_DB_BACKUP_BASENAME="${DB_BACKUP_BASENAME:-}"
_ENV_DB_BACKUP_RETENTION_PREFIX="${DB_BACKUP_RETENTION_PREFIX:-}"
_ENV_DB_BACKUP_RETENTION_COUNT="${DB_BACKUP_RETENTION_COUNT:-}"

if [[ ! -f "${ROOT_DIR}/.env" ]]; then
  echo "Missing ${ROOT_DIR}/.env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "${ROOT_DIR}/.env"
set +a

DB_CONTAINER="${_ENV_DB_CONTAINER:-${DB_CONTAINER:-dotmac_pg_local}}"
BACKUP_ROOT="${_ENV_DB_BACKUP_ROOT:-${DB_BACKUP_ROOT:-/var/backups/dotmac_sub}}"
BACKUP_DIR="${_ENV_DB_BACKUP_DIR:-${DB_BACKUP_DIR:-${BACKUP_ROOT}/deployments}}"
BACKUP_LEGACY_DIR="${_ENV_DB_BACKUP_LEGACY_DIR:-${DB_BACKUP_LEGACY_DIR:-}}"
BACKUP_BASENAME="${_ENV_DB_BACKUP_BASENAME:-${DB_BACKUP_BASENAME:-dotmac_sub}}"
BACKUP_RETENTION_PREFIX="${_ENV_DB_BACKUP_RETENTION_PREFIX:-${DB_BACKUP_RETENTION_PREFIX:-${BACKUP_BASENAME}_}}"
BACKUP_RETENTION_COUNT="${_ENV_DB_BACKUP_RETENTION_COUNT:-${DB_BACKUP_RETENTION_COUNT:-5}}"
BACKUP_DB_USER="${DB_BACKUP_DB_USER:-postgres}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL not set in ${ROOT_DIR}/.env" >&2
  exit 1
fi

if ! [[ "${BACKUP_RETENTION_COUNT}" =~ ^[0-9]+$ ]] || [[ "${BACKUP_RETENTION_COUNT}" -lt 1 ]]; then
  echo "DB_BACKUP_RETENTION_COUNT must be a positive integer" >&2
  exit 1
fi

if ! [[ "${BACKUP_BASENAME}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "DB_BACKUP_BASENAME contains unsupported characters" >&2
  exit 1
fi

if ! [[ "${BACKUP_RETENTION_PREFIX}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "DB_BACKUP_RETENTION_PREFIX contains unsupported characters" >&2
  exit 1
fi

if ! docker inspect "${DB_CONTAINER}" >/dev/null 2>&1; then
  echo "DB container not found: ${DB_CONTAINER} (set DB_CONTAINER=...)" >&2
  exit 1
fi

BACKUP_DB_NAME="${DB_BACKUP_DB_NAME:-${DATABASE_URL##*/}}"
BACKUP_DB_NAME="${BACKUP_DB_NAME%%\?*}"
if [[ -z "${BACKUP_DB_NAME}" || "${BACKUP_DB_NAME}" == "${DATABASE_URL}" ]]; then
  echo "Could not derive DB_BACKUP_DB_NAME from DATABASE_URL" >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"
STAMP=$(date +"%F_%H%M%S")
OUT_FILE="${BACKUP_DIR}/${BACKUP_BASENAME}_${STAMP}.sql.gz"

echo "Starting DB backup to ${OUT_FILE}"
# -Fp (plain) piped through gzip. Dump from inside the DB container as the
# container-local backup user so non-app schemas are included too.
set -o pipefail
docker exec "${DB_CONTAINER}" pg_dump -U "${BACKUP_DB_USER}" -d "${BACKUP_DB_NAME}" \
  --no-owner --no-privileges \
  | gzip > "${OUT_FILE}"

if [[ ! -s "${OUT_FILE}" ]]; then
  echo "Backup produced an empty file — aborting" >&2
  rm -f "${OUT_FILE}"
  exit 1
fi
echo "Backup complete: ${OUT_FILE} ($(du -h "${OUT_FILE}" | cut -f1))"

retention_inventory() {
  local directory
  local -a directories=("${BACKUP_DIR}")

  if [[ -n "${BACKUP_LEGACY_DIR}" && "${BACKUP_LEGACY_DIR}" != "${BACKUP_DIR}" ]]; then
    directories+=("${BACKUP_LEGACY_DIR}")
  fi

  for directory in "${directories[@]}"; do
    [[ -d "${directory}" ]] || continue
    find "${directory}" -maxdepth 1 -type f \
      -name "${BACKUP_RETENTION_PREFIX}*.sql.gz" -printf '%T@ %p\n'
  done | sort -n | cut -d' ' -f2-
}

mapfile -t EXISTING_BACKUPS < <(retention_inventory)
CURRENT_BACKUP_INVENTORIED=0
for existing_backup in "${EXISTING_BACKUPS[@]}"; do
  if [[ "${existing_backup}" == "${OUT_FILE}" ]]; then
    CURRENT_BACKUP_INVENTORIED=1
    break
  fi
done
if [[ "${CURRENT_BACKUP_INVENTORIED}" != "1" ]]; then
  echo "Backup retention refused: current backup is outside the configured retention family" >&2
  exit 1
fi

DISCOVERED_COUNT=${#EXISTING_BACKUPS[@]}
DELETE_COUNT=$((${#EXISTING_BACKUPS[@]} - BACKUP_RETENTION_COUNT))
REMOVED_COUNT=0
if [[ "${DELETE_COUNT}" -gt 0 ]]; then
  for ((i = 0; i < DELETE_COUNT; i++)); do
    echo "Pruning old backup: ${EXISTING_BACKUPS[$i]}"
    rm -f "${EXISTING_BACKUPS[$i]}"
    REMOVED_COUNT=$((REMOVED_COUNT + 1))
  done
fi

mapfile -t RETAINED_BACKUPS < <(retention_inventory)
EXPECTED_COUNT=${DISCOVERED_COUNT}
if [[ "${EXPECTED_COUNT}" -gt "${BACKUP_RETENTION_COUNT}" ]]; then
  EXPECTED_COUNT=${BACKUP_RETENTION_COUNT}
fi
if [[ "${#RETAINED_BACKUPS[@]}" -ne "${EXPECTED_COUNT}" ]]; then
  echo "Backup retention verification failed: expected=${EXPECTED_COUNT} actual=${#RETAINED_BACKUPS[@]}" >&2
  exit 1
fi
if [[ ! -s "${OUT_FILE}" ]]; then
  echo "Backup retention verification failed: current backup is missing" >&2
  exit 1
fi

for retained_backup in "${RETAINED_BACKUPS[@]}"; do
  echo "Retained deployment backup: ${retained_backup}"
done
echo "Backup retention verified: retained=${#RETAINED_BACKUPS[@]} removed=${REMOVED_COUNT} target=${BACKUP_RETENTION_COUNT}"
