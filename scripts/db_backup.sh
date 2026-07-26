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
_ENV_DB_BACKUP_DIR="${DB_BACKUP_DIR:-}"

if [[ ! -f "${ROOT_DIR}/.env" ]]; then
  echo "Missing ${ROOT_DIR}/.env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "${ROOT_DIR}/.env"
set +a

DB_CONTAINER="${_ENV_DB_CONTAINER:-${DB_CONTAINER:-dotmac_pg_local}}"
BACKUP_DIR="${_ENV_DB_BACKUP_DIR:-${DB_BACKUP_DIR:-/var/backups/dotmac_sub}}"
BACKUP_BASENAME="${DB_BACKUP_BASENAME:-dotmac_sub}"
BACKUP_RETENTION_COUNT="${DB_BACKUP_RETENTION_COUNT:-3}"
BACKUP_DB_USER="${DB_BACKUP_DB_USER:-postgres}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL not set in ${ROOT_DIR}/.env" >&2
  exit 1
fi

if ! [[ "${BACKUP_RETENTION_COUNT}" =~ ^[0-9]+$ ]] || [[ "${BACKUP_RETENTION_COUNT}" -lt 1 ]]; then
  echo "DB_BACKUP_RETENTION_COUNT must be a positive integer" >&2
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

mapfile -t EXISTING_BACKUPS < <(
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name "${BACKUP_BASENAME}_*.sql.gz" \
    -printf '%T@ %p\n' | sort -n | cut -d' ' -f2-
)
DELETE_COUNT=$((${#EXISTING_BACKUPS[@]} - BACKUP_RETENTION_COUNT))
if [[ "${DELETE_COUNT}" -gt 0 ]]; then
  for ((i = 0; i < DELETE_COUNT; i++)); do
    echo "Pruning old backup: ${EXISTING_BACKUPS[$i]}"
    rm -f "${EXISTING_BACKUPS[$i]}"
  done
fi
