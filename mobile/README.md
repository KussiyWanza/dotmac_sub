# DotMac Self-Care (Flutter)

Customer-facing mobile app for the DotMac subscriber platform. It talks to the
FastAPI backend in this repo (`app/`, served at `/api/v1`).

## Features

| Area | Screens | Backend |
| --- | --- | --- |
| **Auth** | Login (local / RADIUS), MFA (TOTP), profile, change password | `POST /auth/login`, `/auth/mfa/verify`, `/auth/refresh`, `/auth/logout`, `GET·PATCH /auth/me`, `POST /auth/me/password` |
| **Billing** | Invoice/payment/activity lists and details, PDF invoice and receipt downloads | Self-scoped `GET /me/invoices`, `/me/payments`, `/me/ledger` detail and PDF routes |
| **Usage** | Data quota buckets with progress, per-period stats | `GET /quota-buckets`, `/radius-accounting-sessions` |
| **Subscriptions** | Service list on the dashboard | `GET /subscriptions` |
| **Support** | Ticket list, detail + replies, create ticket | `GET·POST /support/tickets`, `/support/tickets/{id}`, `/support/tickets/{id}/comments` |

## Tech stack

- **State / DI:** `flutter_riverpod`
- **HTTP:** `dio` with a token-refresh interceptor
- **Routing:** `go_router` with an auth-gated redirect
- **Secure storage:** `flutter_secure_storage` (Keychain / EncryptedSharedPreferences)
- No code generation — models use hand-written `fromJson`, so there is **no
  `build_runner` step**.

## Project layout

```
lib/
  main.dart                     # ProviderScope + app entry
  src/
    app.dart                    # MaterialApp.router + theme
    config/env.dart             # API base URL (compile-time --dart-define)
    core/                       # api_client, http guard, token storage,
                                #   exceptions, formatters
    models/                     # plain Dart models mirroring app/schemas/*
    repositories/               # one class per backend domain
    providers/                  # Riverpod: auth_controller + data_providers
    router/app_router.dart      # routes + redirect
    features/                   # auth / home / billing / usage / support
    widgets/                    # shared AsyncValueView, StatusChip, etc.
```

## Getting started

`android/` **is** checked in (bundle id `io.dotmac.selfcare`, app name
"DotMac Self-Care", `INTERNET` permission, debug cleartext). `ios/` and the
other desktop/web folders are not — generate them with `flutter create .`.

```bash
cd mobile
flutter pub get

# In restricted/root environments, use the repo-local wrapper after setup:
./flutterw pub get

# Android — debug build / install / run on a connected physical device
flutter run

# Local backend on an Android emulator
flutter run --dart-define=API_BASE_URL=http://10.0.2.2:8001

# iOS (generate the platform folder first)
flutter create --platforms=ios .
flutter run --dart-define=API_BASE_URL=http://localhost:8001
```

Debug builds allow plain-HTTP (for local/LAN backends); **release builds are
HTTPS-only**, so point them at `https://selfcare.dotmac.io`.

### Choosing `API_BASE_URL`

| Target | Value |
| --- | --- |
| Android emulator | `http://10.0.2.2:8001` |
| iOS simulator | `http://localhost:8001` |
| Physical device | `http://<your-machine-LAN-IP>:8001` |
| Staging / prod | `https://selfcare.dotmac.io` |

### Android toolchain

Building the APK needs a JDK 17 + the Android SDK (platform-tools, `platforms;android-36`,
`build-tools;36.0.0`) — `flutter doctor` will confirm `[✓] Android toolchain`.
Release builds additionally need a signing config (`android/key.properties` +
keystore — both gitignored). Running an emulator needs KVM/hardware
virtualization.

## Release builds

A signed store build pulls config from the repo-root `brand.json`
(`API_BASE_URL` now points at the production host) and is signed from a
keystore referenced by `android/key.properties`.

1. **One-time keystore** (keep it safe — losing it blocks all future updates):
   ```
   keytool -genkey -v -keystore ~/dotmac-release.jks \
     -keyalg RSA -keysize 2048 -validity 10000 -alias dotmac
   ```
   Copy `android/key.properties.example` → `android/key.properties` and fill in.

2. **Build** (the GlitchTip DSN is injected separately so it stays out of git):
   ```
   flutter build appbundle --release \
     --dart-define-from-file=../brand.json \
     --dart-define=GLITCHTIP_DSN=<your-mobile-glitchtip-dsn>
   ```
   With `key.properties` present the bundle is signed with the release key;
   without it, the build falls back to the debug key (installable locally, **not
   shippable**).

3. **CI**: `.github/workflows/mobile-release.yml` produces the signed appbundle
   on a `mobile-v*` tag or manual dispatch, using these repo secrets:
   `ANDROID_KEYSTORE_BASE64` (`base64 -w0 dotmac-release.jks`),
   `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD`,
   `MOBILE_GLITCHTIP_DSN`. iOS release archiving still needs Apple signing
   credentials wired in (job is stubbed).

The payment return scheme (`dotmacpay`) is registered in the Android manifest
and iOS Info.plist; for a white-label build, override `BRAND_PAYMENT_SCHEME` in
the Dart build *and* the matching native entries (Gradle `-PpaymentScheme=`,
iOS `CFBundleURLSchemes`).

## How auth works

1. `POST /auth/login` returns either a token pair **or** an MFA challenge
   (`mfa_required: true`, `mfa_token`). The app routes to the MFA screen and
   completes via `POST /auth/mfa/verify`.
2. Tokens are stored in the secure store. Every request carries
   `Authorization: Bearer <access_token>`.
3. On a `401`, `ApiClient` transparently calls `POST /auth/refresh` once and
   replays the request. If refresh fails, the app drops to the login screen.

## Data scoping — self-scoped `/me/*` endpoints

The staff-facing list endpoints (`/invoices`, `/subscriptions`, `/quota-buckets`)
are gated by `require_permission("billing:read" | "catalog:read" | "usage:read")`
**and** take an explicit `account_id`. A real subscriber's token carries **no
roles/scopes**, so those endpoints return `403` for customers (verified live with
a real subscriber login).

The app therefore reads data through **customer self-care endpoints** added in
`app/api/me.py`, which require only authentication and force scoping to the
caller's own `subscriber_id`:

| App call | Endpoint |
| --- | --- |
| invoices list / detail / PDF | `GET /api/v1/me/invoices`, `/me/invoices/{id}`, `/me/invoices/{id}/pdf` |
| payment history / detail / receipt PDF | `GET /api/v1/me/payments`, `/me/payments/{id}`, `/me/payments/{id}/receipt/pdf` |
| account activity list / detail | `GET /api/v1/me/ledger`, `/me/ledger/{id}` |
| services / plan | `GET /api/v1/me/subscriptions` |
| data usage (quota) | `GET /api/v1/me/quota-buckets` |
| data usage (sessions) | `GET /api/v1/me/radius-accounting-sessions` |

The Usage tab is driven primarily by RADIUS **accounting sessions** (download/
upload octets) — this ISP meters via RADIUS, not quota buckets, so quota cards
only appear when present.

`accountIdProvider` (`Me.id` = the subscriber id) is still used where the caller's
id is needed explicitly (e.g. creating a ticket). No staff scopes required.

> Verified end-to-end with a real subscriber via `test_live/live_backend_test.dart`
> (login → `/auth/me` → `/me/*`), headless on the Dart VM.

## Payments (implemented)

Online invoice payment uses a hosted-checkout flow added to the backend for
bearer-auth API clients:

1. `POST /api/v1/payments/initiate {invoice_id}` → `{provider_type,
   provider_public_key, payment_reference, customer_email, amount, currency}`.
   Self-scoped to the caller's own invoice; no `billing:*` permission required.
2. The app opens `PaymentWebViewScreen`, which runs the **Paystack** or
   **Flutterwave** inline JS keyed by that public key + reference. On success the
   provider callback redirects to a `dotmacpay://success?reference=…` sentinel
   the WebView intercepts.
3. `POST /api/v1/payments/verify {reference}` → verifies with the provider,
   records the `Payment`, allocates it to the invoice (idempotent on the
   provider's external id), and returns the result.

Requirements:
- A payment provider must be configured in backend billing settings
  (`default_payment_provider_type` + the provider public/secret keys).
- `webview_flutter` needs Android `minSdkVersion >= 19` (default is fine) and,
  for iOS, no extra setup for https content.
- The provider inline JS loads from the internet, so the device/emulator needs
  network access.

## Crash reporting (GlitchTip)

Uncaught Dart/Flutter errors are reported to your self-hosted **GlitchTip**
(Sentry-protocol). It is **off by default**: with no DSN, `Sentry.init` is never
called and nothing is sent. The pure-Dart `sentry` client is used (no native
plugin, no build impact).

Enable it by supplying the DSN at build time — never commit it (a DSN is a
client ingestion key embedded in the shipped app). Use a gitignored defines
file:

```bash
cp dart_defines.example.json dart_defines.json   # then fill in GLITCHTIP_DSN
flutter run --dart-define-from-file=dart_defines.json
flutter build apk --release --dart-define-from-file=dart_defines.json
```

Or pass it directly: `--dart-define=GLITCHTIP_DSN=https://key@host/<id>`.

Use a **separate GlitchTip project** for the mobile app so device crashes are
isolated from backend errors. Do not reuse the backend DSN in mobile builds.

## Not yet wired

- **Avatar upload**, **session management UI**, and **push notifications**.
- A pure **top-up / add-funds** flow exists in the backend
  (`create_topup_intent` / `verify_and_record_topup`) but is not yet exposed as
  a bearer-auth API endpoint or surfaced in the app.
