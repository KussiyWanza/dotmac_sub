#!/usr/bin/env bash
# Production-only adapter for a digest authorized by the release control plane.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEPLOY_DIR="${DEPLOY_DIR:-${REPO_DIR}}"
ENV_FILE="${DEPLOY_DIR}/.env"
IMAGE_REPO="ghcr.io/michaelayoade/dotmac_sub"
PYTHON_BIN="${PYTHON_BIN:-python3}"

run_repo_module() {
  REPO_DIR="${REPO_DIR}" PYTHON_BIN="${PYTHON_BIN}" \
    bash "${REPO_DIR}/scripts/run_repo_module.sh" "$@"
}

die() {
  echo "Production deploy refused: $*" >&2
  exit 1
}

env_value() {
  local key="$1"
  grep -E "^${key}=" "${ENV_FILE}" 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

require_exact_env_line() {
  local expected="$1"
  grep -Fqx "${expected}" "${ENV_FILE}" || die "${ENV_FILE} must contain ${expected}"
}

usage() {
  echo "usage: deploy_production.sh <sha256:digest> <authorization.json> [--hotfix-no-migrations --change-reference REF --reason TEXT]" >&2
  exit 2
}

(($# >= 2)) || usage
DIGEST="$1"
AUTHORIZATION_FILE="$2"
shift 2
[[ "${DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]] || usage
[[ -f "${AUTHORIZATION_FILE}" ]] || die "missing production authorization ${AUTHORIZATION_FILE}"
[[ -f "${ENV_FILE}" ]] || die "missing ${ENV_FILE}"
require_exact_env_line "APP_ENV=production"
require_exact_env_line "SERVER_NAME=dotmac-sub-prod"

HOTFIX=0
CHANGE_REFERENCE=""
REASON=""
while (($#)); do
  case "$1" in
    --hotfix-no-migrations) HOTFIX=1; shift ;;
    --change-reference)
      (($# >= 2)) || usage
      CHANGE_REFERENCE="$2"
      shift 2
      ;;
    --reason)
      (($# >= 2)) || usage
      REASON="$2"
      shift 2
      ;;
    *) usage ;;
  esac
done

if [[ "${HOTFIX}" != "1" && ( -n "${CHANGE_REFERENCE}" || -n "${REASON}" ) ]]; then
  die "hotfix attribution requires --hotfix-no-migrations"
fi
if [[ "${HOTFIX}" == "1" && ( -z "${CHANGE_REFERENCE}" || -z "${REASON}" ) ]]; then
  die "hotfix backup exception requires a change reference and reason"
fi
if [[ -n "${SKIP_BACKUP:-}" ]]; then
  die "SKIP_BACKUP is not accepted for production"
fi

export PRODUCTION_RELEASE_EVIDENCE="${AUTHORIZATION_FILE}"
unset SKIP_BACKUP
unset PRODUCTION_BACKUP_DECISION_FILE

if [[ "${HOTFIX}" == "1" ]]; then
  PREVIOUS_IMAGE="$(env_value APP_IMAGE)"
  [[ -n "${PREVIOUS_IMAGE}" ]] || die "APP_IMAGE is required to prove hotfix migration state"
  CANDIDATE_IMAGE="${IMAGE_REPO}@${DIGEST}"
  TMP_DIR="$(mktemp -d)"
  CONTAINERS=()
  cleanup() {
    local container
    for container in "${CONTAINERS[@]}"; do
      docker rm -f "${container}" >/dev/null 2>&1 || true
    done
    rm -rf "${TMP_DIR}"
  }
  trap cleanup EXIT

  describe_image() {
    local image="$1"
    local name="$2"
    local container
    mkdir -p "${TMP_DIR}/${name}"
    docker pull "${image}" >/dev/null || return 1
    container="$(docker create "${image}")" || return 1
    CONTAINERS+=("${container}")
    docker cp "${container}:/app/alembic/versions" "${TMP_DIR}/${name}/versions" || return 1
    run_repo_module scripts.release_backup_policy describe-tree \
      --versions-dir "${TMP_DIR}/${name}/versions" \
      --output "${TMP_DIR}/${name}.json" || return 1
  }

  if ! describe_image "${PREVIOUS_IMAGE}" running \
    || ! describe_image "${CANDIDATE_IMAGE}" candidate; then
    echo "Hotfix migration evidence could not be collected; production backup remains required." >&2
    HOTFIX=0
  fi

  if [[ "${HOTFIX}" == "1" ]]; then
    DB_CONTAINER="${DB_CONTAINER:-$(env_value DB_CONTAINER)}"
    DB_CONTAINER="${DB_CONTAINER:-dotmac_pg_local}"
    BACKUP_DB_USER="${DB_BACKUP_DB_USER:-$(env_value DB_BACKUP_DB_USER)}"
    BACKUP_DB_USER="${BACKUP_DB_USER:-postgres}"
    BACKUP_DB_NAME="${DB_BACKUP_DB_NAME:-$(env_value DB_BACKUP_DB_NAME)}"
    if [[ -z "${BACKUP_DB_NAME}" ]]; then
      DATABASE_URL="$(env_value DATABASE_URL)"
      BACKUP_DB_NAME="${DATABASE_URL##*/}"
      BACKUP_DB_NAME="${BACKUP_DB_NAME%%\?*}"
    fi
    DATABASE_OUTPUT=""
    if [[ -n "${BACKUP_DB_NAME}" ]]; then
      DATABASE_OUTPUT="$(
        docker exec "${DB_CONTAINER}" psql -X -A -t -U "${BACKUP_DB_USER}" \
          -d "${BACKUP_DB_NAME}" \
          -c 'SELECT version_num FROM alembic_version ORDER BY version_num'
      )" || true
    fi
    mapfile -t DATABASE_HEADS <<<"${DATABASE_OUTPUT}"
    HEAD_ARGS=()
    for head in "${DATABASE_HEADS[@]}"; do
      if [[ ! "${head}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
        HEAD_ARGS=()
        break
      fi
      HEAD_ARGS+=(--database-head "${head}")
    done
    DECISION_FILE="${TMP_DIR}/production-backup-decision.json"
    if ((${#HEAD_ARGS[@]} > 0)); then
      BACKUP_MODE="$(
        run_repo_module scripts.release_backup_policy write-production-decision \
          --running-image "${TMP_DIR}/running.json" \
          --candidate-image "${TMP_DIR}/candidate.json" \
          "${HEAD_ARGS[@]}" \
          --change-reference "${CHANGE_REFERENCE}" \
          --reason "${REASON}" \
          --output "${DECISION_FILE}"
      )" || BACKUP_MODE="required"
      if [[ "${BACKUP_MODE}" == "skip_production_hotfix" ]]; then
        export PRODUCTION_BACKUP_DECISION_FILE="${DECISION_FILE}"
        echo "Verified no-migration hotfix backup exception for ${CHANGE_REFERENCE}."
      else
        echo "Hotfix backup exception was not proven; production backup remains required." >&2
      fi
    else
      echo "Database migration heads could not be proven; production backup remains required." >&2
    fi
  fi
fi

export REPO_DIR DEPLOY_DIR
bash "${REPO_DIR}/scripts/deploy.sh" "${DIGEST}"
