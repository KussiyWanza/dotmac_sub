#!/bin/sh

# Xcode Cloud post-clone hook for the dotmac_field technician app.
#
# Xcode Cloud clones the repo and runs `xcodebuild` on Runner.xcworkspace, but it
# has no knowledge of Flutter — the Flutter SDK, Generated.xcconfig, the Flutter
# framework, generated Dart (drift), and CocoaPods/SwiftPM deps are all absent, so
# the build fails. This script installs Flutter and generates those artifacts
# before Xcode Cloud's build step runs.
#
# Lives at mobile/ios/ci_scripts/ci_post_clone.sh — Xcode Cloud auto-runs any
# ci_scripts/ci_post_clone.sh adjacent to the Xcode project.

set -e

# Xcode Cloud clones to $CI_PRIMARY_REPOSITORY_PATH; the Flutter project is mobile/.
REPO="${CI_PRIMARY_REPOSITORY_PATH:-$(cd "$(dirname "$0")/../../.." && pwd)}"
MOBILE="$REPO/mobile"

# Pin Flutter to the exact revision recorded in mobile/.metadata (version
# controlled, authoritative). Xcode Cloud disables automatic SwiftPM resolution,
# so the regenerated plugin package graph must match the committed
# Package.resolved — a floating "stable" can pull newer plugins and break
# resolution. Keeping this in lockstep with .metadata avoids a hardcoded version.
FLUTTER_REV=$(grep -m1 '  revision:' "$MOBILE/.metadata" | sed -E 's/.*"([0-9a-f]+)".*/\1/')
echo "=== Installing Flutter (revision ${FLUTTER_REV:-unknown} from .metadata) ==="
git clone https://github.com/flutter/flutter.git "$HOME/flutter"
if [ -n "$FLUTTER_REV" ]; then
  (cd "$HOME/flutter" && git checkout -q "$FLUTTER_REV")
fi
export PATH="$HOME/flutter/bin:$PATH"
flutter --version

echo "=== Preparing the Flutter iOS build ==="
cd "$MOBILE"
flutter precache --ios
flutter pub get

# The app uses drift; generated *.g.dart must exist before the iOS build.
echo "=== Drift codegen ==="
dart run build_runner build --delete-conflicting-outputs

# Generates ios/Flutter/Generated.xcconfig, the App/Flutter frameworks, the
# plugin registrant, and resolves pods/SwiftPM — everything Xcode Cloud's
# xcodebuild needs. API_BASE_URL defaults to selfcare production so TestFlight builds
# point at the live backend; override via an Xcode Cloud environment variable.
flutter build ios --release --no-codesign \
  --dart-define=API_BASE_URL="${API_BASE_URL:-https://selfcare.dotmac.io}" \
  --dart-define=SENTRY_DSN="${SENTRY_DSN:-}"

# --- FCM push (operator-gated) -------------------------------------------------
# GoogleService-Info.plist and the push capability are per-deployment and kept
# out of the repo. When the operator supplies the plist as a base64 Xcode Cloud
# secret, materialize it, switch the entitlement to the production APNs
# environment (this is a distribution/TestFlight build), and wire it into the
# Runner target. Without the secret the app builds normally with push disabled
# (FcmPushSource.tryCreate() returns null → NoopPushSource). Runs AFTER
# `flutter build` so Flutter's project regeneration can't clobber it.
if [ -n "$GOOGLE_SERVICE_INFO_PLIST_B64" ]; then
  echo "=== Wiring FCM push (GoogleService-Info.plist provided) ==="
  echo "$GOOGLE_SERVICE_INFO_PLIST_B64" | base64 --decode > ios/Runner/GoogleService-Info.plist
  /usr/libexec/PlistBuddy -c "Set :aps-environment production" ios/Runner/Runner.entitlements
  export GEM_HOME="$HOME/.gem"
  export PATH="$GEM_HOME/bin:$PATH"
  gem install xcodeproj --no-document
  ruby ios/ci_scripts/wire_firebase.rb
else
  echo "=== GOOGLE_SERVICE_INFO_PLIST_B64 not set — building with push disabled ==="
fi

echo "=== Flutter setup complete ==="
exit 0
