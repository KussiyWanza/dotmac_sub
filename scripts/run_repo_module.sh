#!/usr/bin/env bash
# Execute a Python module from the explicitly selected repository checkout.
#
# PYTHONPATH does not provide this guarantee when the caller is standing in a
# different checkout: Python puts the current directory before PYTHONPATH. A
# stale deploy tree can therefore shadow release-control modules from the
# authorized Actions checkout. Changing directory makes REPO_DIR the import
# root before the interpreter starts.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if (($# == 0)); then
  echo "usage: run_repo_module.sh <module> [args...]" >&2
  exit 2
fi
if [[ ! -d "${REPO_DIR}" ]]; then
  echo "Repository module root does not exist: ${REPO_DIR}" >&2
  exit 2
fi

cd "${REPO_DIR}"
exec "${PYTHON_BIN}" -m "$@"
