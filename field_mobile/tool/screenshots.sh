#!/usr/bin/env bash
#
# Capture App Store / Play Store screenshots for the field app.
#
# Drives a real login and captures each primary tab (see
# integration_test/screenshots_test.dart). PNGs land in build/screenshots/.
#
# Prerequisites:
#   - A booted target device/simulator. App Store needs a 6.9" iPhone
#     (1320x2868) and a 13" iPad; Play needs a phone + tablet. Run once per
#     device and pass -d <device-id> (see `flutter devices`).
#   - A working technician demo account (App Review requires it too).
#
# Usage:
#   DEMO_USERNAME=tech@example.com DEMO_PASSWORD=secret tool/screenshots.sh -d <device-id>
#
# Optional:
#   API_BASE_URL=https://selfcare.dotmac.io   # defaults to this production host
set -euo pipefail
cd "$(dirname "$0")/.."

: "${DEMO_USERNAME:?set DEMO_USERNAME (technician demo login)}"
: "${DEMO_PASSWORD:?set DEMO_PASSWORD}"
API_BASE_URL="${API_BASE_URL:-https://selfcare.dotmac.io}"

flutter drive \
  --driver=test_driver/screenshot_driver.dart \
  --target=integration_test/screenshots_test.dart \
  --dart-define=DEMO_USERNAME="$DEMO_USERNAME" \
  --dart-define=DEMO_PASSWORD="$DEMO_PASSWORD" \
  --dart-define=API_BASE_URL="$API_BASE_URL" \
  "$@"

echo "Screenshots written to build/screenshots/"
