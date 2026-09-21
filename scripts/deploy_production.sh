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
  echo "usage: deploy_production.sh <sha256:digest> <authorization.json> [--hotfix-no-migrations --change-reference REF --reason TEXT] [--resume-after-migration --failed-run-id RUN_ID --backup-path PATH] [--rollback-authorization PATH] [--bootstrap-authorization PATH]" >&2
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
ROLLBACK_AUTHORIZATION=""
BOOTSTRAP_AUTHORIZATION=""
CHANGE_REFERENCE=""
REASON=""
RESUME_AFTER_MIGRATION=0
FAILED_RUN_ID=""
BACKUP_PATH=""
while (($#)); do
  case "$1" in
    --rollback-authorization) ROLLBACK_AUTHORIZATION="${2:-}"; shift 2 ;;
    --bootstrap-authorization)
      (($# >= 2)) || usage
      BOOTSTRAP_AUTHORIZATION="$2"
      shift 2
      ;;
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
    --resume-after-migration) RESUME_AFTER_MIGRATION=1; shift ;;
    --failed-run-id)
      (($# >= 2)) || usage
      FAILED_RUN_ID="$2"
      shift 2
      ;;
    --backup-path)
      (($# >= 2)) || usage
      BACKUP_PATH="$2"
      shift 2
      ;;
    *) usage ;;
  esac
done

if [[ -n "${ROLLBACK_AUTHORIZATION}" && -n "${BOOTSTRAP_AUTHORIZATION}" ]]; then
  die "rollback and first-deployment bootstrap authorizations are mutually exclusive"
fi
if [[ -n "${BOOTSTRAP_AUTHORIZATION}" && ( "${HOTFIX}" == "1" || "${RESUME_AFTER_MIGRATION}" == "1" ) ]]; then
  die "first-deployment bootstrap cannot be combined with hotfix or resume modes"
fi

if [[ "${HOTFIX}" != "1" && ( -n "${CHANGE_REFERENCE}" || -n "${REASON}" ) ]]; then
  die "hotfix attribution requires --hotfix-no-migrations"
fi
if [[ "${HOTFIX}" == "1" && ( -z "${CHANGE_REFERENCE}" || -z "${REASON}" ) ]]; then
  die "hotfix backup exception requires a change reference and reason"
fi
if [[ "${RESUME_AFTER_MIGRATION}" == "1" && "${HOTFIX}" == "1" ]]; then
  die "post-migration resume cannot be combined with a no-migration hotfix exception"
fi
if [[ "${RESUME_AFTER_MIGRATION}" != "1" && ( -n "${FAILED_RUN_ID}" || -n "${BACKUP_PATH}" ) ]]; then
  die "resume evidence requires --resume-after-migration"
fi
if [[ "${RESUME_AFTER_MIGRATION}" == "1" && ( -z "${FAILED_RUN_ID}" || -z "${BACKUP_PATH}" ) ]]; then
  die "post-migration resume requires failed run ID and backup path"
fi
if [[ -n "${SKIP_BACKUP:-}" ]]; then
  die "SKIP_BACKUP is not accepted for production"
fi

export PRODUCTION_RELEASE_EVIDENCE="${AUTHORIZATION_FILE}"
unset SKIP_BACKUP
unset PRODUCTION_BACKUP_DECISION_FILE
if [[ "${RESUME_AFTER_MIGRATION}" == "1" ]]; then
  [[ "${FAILED_RUN_ID}" =~ ^[0-9]+$ && "${FAILED_RUN_ID}" -gt 0 ]] || die "failed run ID must be a positive integer"
  [[ -n "${AUTHORIZATION_RUN_ID:-}" ]] || die "AUTHORIZATION_RUN_ID is required for post-migration resume"
  export PRODUCTION_DEPLOY_RESUME_AFTER_MIGRATION=1
  export PRODUCTION_DEPLOY_RESUME_FAILED_RUN_ID="${FAILED_RUN_ID}"
  export PRODUCTION_DEPLOY_RESUME_BACKUP_PATH="${BACKUP_PATH}"
  export PRODUCTION_DEPLOY_RESUME_AUTHORIZATION_RUN_ID="${AUTHORIZATION_RUN_ID}"
else
  unset PRODUCTION_DEPLOY_RESUME_AFTER_MIGRATION
  unset PRODUCTION_DEPLOY_RESUME_FAILED_RUN_ID
  unset PRODUCTION_DEPLOY_RESUME_BACKUP_PATH
  unset PRODUCTION_DEPLOY_RESUME_AUTHORIZATION_RUN_ID
fi

# --- Anti-rollback gate -------------------------------------------------------
# Runs BEFORE anything that touches the host: before the hotfix migration
# evidence collection (which pulls images and creates throwaway containers) and
# before deploy.sh, which owns the database backup and `alembic upgrade`. A
# refusal must leave production exactly as it found it, so the gate is the
# first step after argument validation.
# Deploying a revision that is not a descendant of the one already running
# silently re-introduces every defect fixed in between, and once migrations
# have been applied it puts older code against a newer schema. Forward
# progress is proven from the running container's own OCI revision label --
# what is actually running -- not from any file the deploy was handed.
APP_CONTAINER="${APP_CONTAINER:-dotmac_sub_app}"
TARGET_REVISION=""
REVISION_OUTPUTS="$(mktemp)"
# Bind this read to what the deploy was actually handed: the exact digest
# being deployed, and -- when present -- the exact authorization run id
# already threaded through this script (AUTHORIZATION_RUN_ID, the same
# environment variable required above for post-migration resume and passed
# in by production-deploy.yml on every invocation). Without this, the
# anti-rollback gate would accept any authorization document naming a
# forward-looking TARGET_REVISION, even one that does not match the digest
# this invocation is actually deploying.
VERIFY_PRODUCTION_ARGS=(
  verify-production
  --path "${AUTHORIZATION_FILE}"
  --github-output "${REVISION_OUTPUTS}"
  --expected-image-digest "${DIGEST}"
)
if [[ -n "${AUTHORIZATION_RUN_ID:-}" ]]; then
  VERIFY_PRODUCTION_ARGS+=(--expected-authorization-run-id "${AUTHORIZATION_RUN_ID}")
fi
if run_repo_module scripts.release_candidate_evidence "${VERIFY_PRODUCTION_ARGS[@]}" >/dev/null; then
  TARGET_REVISION="$(sed -n 's/^release_revision=//p' "${REVISION_OUTPUTS}")"
else
  rm -f "${REVISION_OUTPUTS}"
  die "could not verify the authorized release revision; refusing to deploy without proving forward progress"
fi
rm -f "${REVISION_OUTPUTS}"
[[ "${TARGET_REVISION}" =~ ^[0-9a-f]{40}$ ]] \
  || die "authorized release revision is missing or malformed"

# A failed `docker inspect` cannot distinguish an empty host from a denied or
# unavailable Docker daemon. Prove daemon access first, then inventory the exact
# production container. Only a confirmed absence may enter the separately
# typed, exact-target first-deployment path.
docker info >/dev/null 2>&1 \
  || die "Docker runtime is unreadable; cannot prove the running production revision"
if ! CONTAINER_NAMES="$(
  docker container ls -a \
    --filter "name=^/${APP_CONTAINER}$" \
    --format '{{.Names}}'
)"; then
  die "production container inventory is unreadable; cannot prove first-deployment or running state"
fi

if [[ -z "${CONTAINER_NAMES}" ]]; then
  [[ -n "${BOOTSTRAP_AUTHORIZATION}" ]] || die \
    "${APP_CONTAINER} is confirmed absent; first deployment requires --bootstrap-authorization bound to this server and revision"
  [[ -f "${BOOTSTRAP_AUTHORIZATION}" ]] \
    || die "bootstrap authorization file not found"
  run_repo_module scripts.release_candidate_evidence verify-bootstrap-authorization \
    --path "${BOOTSTRAP_AUTHORIZATION}" \
    --target-revision "${TARGET_REVISION}" \
    --target-server "dotmac-sub-prod" \
    || die "bootstrap authorization does not authorize first deployment of ${TARGET_REVISION} to dotmac-sub-prod"
  echo "Authorized first deployment of ${TARGET_REVISION} to dotmac-sub-prod."
else
  [[ "${CONTAINER_NAMES}" == "${APP_CONTAINER}" ]] || die \
    "production container inventory is ambiguous for ${APP_CONTAINER}"
  [[ -z "${BOOTSTRAP_AUTHORIZATION}" ]] || die \
    "bootstrap authorization is accepted only when ${APP_CONTAINER} is confirmed absent"
  if ! RUNNING_REVISION="$(
    docker inspect "${APP_CONTAINER}" \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
  )"; then
    die "could not inspect ${APP_CONTAINER}; refusing to deploy without its running revision"
  fi
  if [[ -z "${RUNNING_REVISION}" || "${RUNNING_REVISION}" == "<no value>" ]]; then
    die "${APP_CONTAINER} has no org.opencontainers.image.revision label; running history is unprovable"
  fi
  [[ "${RUNNING_REVISION}" =~ ^[0-9a-f]{40}$ ]] || die \
    "${APP_CONTAINER} has a malformed org.opencontainers.image.revision label; running history is unprovable"

  if [[ "${RUNNING_REVISION}" == "${TARGET_REVISION}" ]]; then
    echo "Redeploying the running revision ${TARGET_REVISION}."
  else
    git -C "${REPO_DIR}" fetch --no-tags --quiet origin main || true
    if ! git -C "${REPO_DIR}" cat-file -e "${RUNNING_REVISION}^{commit}" 2>/dev/null; then
      DIRECTION="unknown"
    elif git -C "${REPO_DIR}" merge-base --is-ancestor \
      "${RUNNING_REVISION}" "${TARGET_REVISION}" 2>/dev/null; then
      DIRECTION="forward"
    elif git -C "${REPO_DIR}" merge-base --is-ancestor \
      "${TARGET_REVISION}" "${RUNNING_REVISION}" 2>/dev/null; then
      DIRECTION="backward"
    else
      DIRECTION="divergent"
    fi

    if [[ "${DIRECTION}" == "forward" ]]; then
      echo "Forward deploy: ${RUNNING_REVISION} -> ${TARGET_REVISION}."
    else
      # Every non-forward case needs the SAME typed, transition-bound
      # authorization. A divergent or unprovable history is not safer than a
      # known rollback, so it must not be easier to push through.
      [[ -n "${ROLLBACK_AUTHORIZATION}" ]] || die \
        "refusing ${DIRECTION} production deploy ${RUNNING_REVISION} -> ${TARGET_REVISION}: supply --rollback-authorization with a typed authorization naming this exact transition"
      [[ -f "${ROLLBACK_AUTHORIZATION}" ]] || die "rollback authorization file not found"
      run_repo_module scripts.release_candidate_evidence verify-rollback-authorization \
        --path "${ROLLBACK_AUTHORIZATION}" \
        --running-revision "${RUNNING_REVISION}" \
        --target-revision "${TARGET_REVISION}" \
        || die "rollback authorization does not authorize ${RUNNING_REVISION} -> ${TARGET_REVISION}"
      echo "Authorized ${DIRECTION} production deploy ${RUNNING_REVISION} -> ${TARGET_REVISION}."
    fi
  fi
fi

# --- End anti-rollback gate --------------------------------------------------

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
