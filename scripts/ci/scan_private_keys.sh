#!/usr/bin/env bash
# Fail if any tracked file contains actual PRIVATE KEY material.
#
# Why this exists separately from pre-commit's detect-private-key hook:
#
#   On 2026-01-26 a real OpenVPN server private key was committed in
#   configs/openvpn/mikrotik.conf. The detect-private-key hook caught it — and
#   the file was added to the hook's `exclude:` list instead of the key being
#   removed. detect-secrets had `configs/` excluded too. The key then sat in a
#   PUBLIC repository until 2026-07-27.
#
#   The failure mode was not a missing guard. It was a guard with a
#   path-shaped off switch, reachable by whoever the guard inconvenienced.
#
# So this check has no path allowlist, by design. It discriminates on CONTENT:
# a PEM header with no base64 body after it is a placeholder (a form hint, a
# test fixture, a doc example) and passes; a header followed by real encoded
# key material fails. There is nothing to add a path to, so the only way to
# make it pass is to remove the key.
#
# If it fires: the key is compromised. Rotate it first — deleting the file does
# not un-publish it. Then remove it and render it at provisioning time instead.
#
# Usage: scripts/ci/scan_private_keys.sh [path ...]   (defaults to all tracked files)
set -euo pipefail

# A PEM body line: base64, and long enough that prose or a short placeholder
# ("test", "abc", "...") cannot reach it.
BODY_LINE='^[A-Za-z0-9+/=]{40,}$'
# Number of body lines before we call it real key material. The smallest key
# anyone would actually deploy is far above this; placeholders are at zero.
MIN_BODY_LINES=4

MARKER='-----BEGIN .*PRIVATE KEY-----'

# Narrow to candidates in one pass before doing per-file work. Scanning every
# tracked file individually took ~21s; this takes well under a second, and the
# expensive awk below then runs on a handful of files instead of thousands.
# Not `mapfile` — macOS ships bash 3.2 and this must run locally as well as on
# the ubuntu CI runner.
candidates=()
if [ "$#" -gt 0 ]; then
  scanned="$# given path(s)"
  while IFS= read -r line; do
    [ -n "$line" ] && candidates+=("$line")
  done < <(grep -lI -- "$MARKER" "$@" 2>/dev/null || true)
else
  scanned="$(git ls-files | wc -l | tr -d ' ') tracked files"
  while IFS= read -r line; do
    [ -n "$line" ] && candidates+=("$line")
  done < <(git grep -lI -- "$MARKER" || true)
fi

findings=0

for f in "${candidates[@]:-}"; do
  [ -n "$f" ] && [ -f "$f" ] || continue

  # Count base64 body lines that fall between BEGIN and END markers.
  body=$(awk '
    /-----BEGIN .*PRIVATE KEY-----/ { inblock=1; next }
    /-----END .*PRIVATE KEY-----/   { inblock=0; next }
    inblock                          { print }
  ' "$f" | grep -cE "$BODY_LINE" || true)

  # A key can also be inlined as one long base64 blob on a single line
  # (JSON/YAML embedding, "\n"-escaped env values).
  inline=$(grep -cE -- '-----BEGIN .*PRIVATE KEY-----([A-Za-z0-9+/=]|\\n){200,}' "$f" 2>/dev/null || true)

  if [ "$body" -ge "$MIN_BODY_LINES" ] || [ "$inline" -gt 0 ]; then
    line=$(grep -nE -- '-----BEGIN .*PRIVATE KEY-----' "$f" | head -1 | cut -d: -f1)
    # Report location and shape only — never echo the material itself.
    echo "PRIVATE KEY MATERIAL: ${f}:${line} (${body} encoded body lines)" >&2
    findings=$((findings + 1))
  fi
done

if [ "$findings" -gt 0 ]; then
  cat >&2 <<'MSG'

Committed private key material found in the files listed above.

This check has no path allowlist on purpose — see the header of this script.
Do NOT try to make it pass by excluding the file.

  1. ROTATE the key. It must be assumed compromised; on a public repo, assume
     it is. Deleting the file does not un-publish it.
  2. Remove it from the tree and add its path to .gitignore.
  3. Render it at provisioning time from OpenBao instead of committing it.
MSG
  exit 1
fi

echo "No private key material in ${scanned} (${#candidates[@]} contained a PEM header)."
