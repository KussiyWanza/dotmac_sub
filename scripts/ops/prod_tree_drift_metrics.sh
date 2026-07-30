#!/usr/bin/env bash
# Export git working-tree drift gauges for a deploy host to VictoriaMetrics.
#
# Why this exists: the production checkout doubles as the compose/config source
# (code deploys are immutable GHCR images via scripts/deploy.sh, but
# docker-compose.yml, config/, and nginx/ are read from the tree). Parallel
# workstreams have repeatedly left the tree on a stale feature branch or with
# hand-applied edits, and nothing paged — the drift was only discovered when
# the next deploy tripped over it. These gauges make that state alertable
# instead of archaeological.
#
# Cron it on the deploy host (the tree it measures), e.g.:
#   */15 * * * * /root/dotmac_sub/scripts/ops/prod_tree_drift_metrics.sh >> /var/log/dotmac_tree_drift.log 2>&1
#
# Series (all gauges, pushed via /api/v1/import/prometheus, VM stamps time):
#   deploy_tree_fetch_ok                1 = `git fetch origin main` succeeded this run
#   deploy_tree_on_main                 1 = checked-out branch is main
#   deploy_tree_clean                   1 = `git status --porcelain` is empty
#   deploy_tree_matches_origin_main     1 = HEAD commit == origin/main
#   deploy_tree_dirty_files             count of modified/untracked paths
#   deploy_tree_behind_commits          commits on origin/main not on HEAD
#
# Read-only against the repo; the only write is the metrics POST. Bounded by
# design: two git plumbing reads and one status walk per run, no network calls
# other than the fetch and the push.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
VM_URL="${VICTORIAMETRICS_URL:-http://127.0.0.1:8428}"

cd "${REPO_DIR}"

fetch_ok=1
git fetch origin main --quiet 2>/dev/null || fetch_ok=0

branch="$(git rev-parse --abbrev-ref HEAD)"
on_main=0
[[ "${branch}" == "main" ]] && on_main=1

dirty_files="$(git status --porcelain | wc -l | tr -d ' ')"
clean=0
[[ "${dirty_files}" == "0" ]] && clean=1

matches=0
behind=0
if git rev-parse --verify --quiet origin/main >/dev/null; then
  [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] && matches=1
  behind="$(git rev-list --count HEAD..origin/main)"
fi

payload="$(cat <<METRICS
deploy_tree_fetch_ok ${fetch_ok}
deploy_tree_on_main ${on_main}
deploy_tree_clean ${clean}
deploy_tree_matches_origin_main ${matches}
deploy_tree_dirty_files ${dirty_files}
deploy_tree_behind_commits ${behind}
METRICS
)"

if ! curl -fsS --max-time 10 -X POST \
  --data-binary "${payload}" \
  "${VM_URL}/api/v1/import/prometheus" >/dev/null; then
  echo "$(date -u +%FT%TZ) push to ${VM_URL} failed" >&2
  exit 1
fi
