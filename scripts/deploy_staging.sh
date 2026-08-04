#!/usr/bin/env bash
# Staging-only deployment adapter.
#
# The staging database is non-authoritative and shares its host disk with the
# application stack. A full local pg_dump before every staging deployment can
# therefore starve the running services of I/O without protecting production.
# This adapter proves the exact staging host contract before opting out of that
# local dump. Seabone's cold application imports can exceed the production
# health budget under measured disk/swap pressure, so this adapter also owns a
# staging-only ten-minute health window. Production and every other environment
# continue to call scripts/deploy.sh directly, where backups remain enabled and
# the shorter default health budget remains unchanged.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"

die() {
  echo "Staging deploy refused: $*" >&2
  exit 1
}

require_exact_env_line() {
  local expected="$1"
  grep -Fqx "${expected}" "${ENV_FILE}" || die "${ENV_FILE} must contain ${expected}"
}

[[ -f "${ENV_FILE}" ]] || die "missing ${ENV_FILE}"
require_exact_env_line "APP_ENV=staging"
require_exact_env_line "SERVER_NAME=dotmac-sub-staging"
require_exact_env_line "HEALTH_URL=http://10.120.121.20:8001/health"

cd "${ROOT_DIR}"
export SKIP_BACKUP=1
export REQUIRE_PROXY_HANDOFF=0
export HEALTH_TIMEOUT_SECONDS=600
exec bash "${ROOT_DIR}/scripts/deploy.sh" "$@"
