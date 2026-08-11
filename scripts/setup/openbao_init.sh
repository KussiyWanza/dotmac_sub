#!/usr/bin/env bash
# OpenBao secrets initialization for dotmac_sub.
# Seeds project secrets into KV v2 at secret/<path> using real environment
# values only. The script never falls back to baked-in secrets.
#
# Usage:
#   ./scripts/openbao_init.sh
#   ./scripts/openbao_init.sh --check
#   ./scripts/openbao_init.sh --strict
#   BAO_ADDR=http://openbao:8200 ./scripts/openbao_init.sh
#
# Modes:
#   default  Write any secret groups whose required env vars are present.
#   --check  Report what would be written/skipped without changing OpenBao.
#   --strict Fail if any required env var for any group is missing.

set -euo pipefail

CONTAINER="dotmac_sub_openbao"
BAO_ADDR="${BAO_ADDR:-http://127.0.0.1:8200}"
BAO_TOKEN="${BAO_TOKEN:-${OPENBAO_TOKEN:-}}"
CHECK_ONLY=0
STRICT=0

for arg in "$@"; do
    case "$arg" in
        --check)
            CHECK_ONLY=1
            ;;
        --strict)
            STRICT=1
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

if [ -z "$BAO_TOKEN" ]; then
    echo "BAO_TOKEN or OPENBAO_TOKEN is required; no default token is used." >&2
    exit 1
fi

if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

run_bao() {
    docker exec \
        -e BAO_ADDR="$BAO_ADDR" \
        -e BAO_TOKEN="$BAO_TOKEN" \
        "$CONTAINER" bao "$@"
}

require_vars() {
    local missing=0
    for name in "$@"; do
        if [ -z "${!name:-}" ]; then
            echo "  [MISSING] $name"
            missing=1
        fi
    done
    return $missing
}

patch_secret() {
    local path="$1"
    shift
    if [ "$CHECK_ONLY" -eq 1 ]; then
        echo "  [CHECK] secret/${path}"
        return 0
    fi
    # Preserve fields managed by a different bootstrap or rotation workflow.
    # `kv put` replaces the complete payload, while `kv patch` only updates the
    # fields supplied here.
    if run_bao kv get "secret/${path}" >/dev/null 2>&1; then
        run_bao kv patch "secret/${path}" "$@" >/dev/null
    else
        run_bao kv put "secret/${path}" "$@" >/dev/null
    fi
    echo "  [OK] secret/${path}"
}

seed_group() {
    local path="$1"
    shift
    local required_csv="$1"
    shift
    local required=()
    local missing=0
    IFS=',' read -r -a required <<<"$required_csv"

    echo "==> secret/${path}"
    if ! require_vars "${required[@]}"; then
        missing=1
    fi

    if [ "$missing" -eq 1 ]; then
        if [ "$STRICT" -eq 1 ]; then
            echo "  [FAIL] secret/${path} skipped because required env vars are missing" >&2
            return 1
        fi
        echo "  [SKIP] secret/${path} skipped because required env vars are missing"
        return 0
    fi

    patch_secret "$path" "$@"
}

seed_optional_group() {
    # Material for ONE feature, mirroring `OPTIONAL_SECRET_REFS` in
    # app/services/kernel_secret_source.py and the settings keyring in
    # app/services/kernel_key_provider.py.
    #
    # Skips even under --strict, which is the whole difference from
    # `seed_group`. A deployment that does not use prepaid reconstruction has
    # no attestation anchor to provision, and one that has not started
    # encrypting secret settings has no keyring; failing a bootstrap over
    # either would make --strict useless for the deployments that need it.
    #
    # The application makes the same distinction at boot, and makes it the
    # strict way: a missing PATH is "not provisioned", while an unreachable
    # store still raises.
    local path="$1"
    shift
    local required_csv="$1"
    shift
    local required=()
    IFS=',' read -r -a required <<<"$required_csv"

    echo "==> secret/${path} (optional)"
    for name in "${required[@]}"; do
        if [ -z "${!name:-}" ]; then
            echo "  [SKIP] ${name} is unset; this material is not provisioned"
            return 0
        fi
    done

    patch_secret "$path" "$@"
}

seed_credential_keyring() {
    local path="settings/auth"
    local seed="${CREDENTIAL_ENCRYPTION_KEY_SEED:-}"
    local active="${CREDENTIAL_ENCRYPTION_KEY:-}"
    local existing=""

    echo "==> secret/${path}"
    if [ -z "$seed" ]; then
        if [ "$STRICT" -eq 1 ]; then
            echo "  [FAIL] CREDENTIAL_ENCRYPTION_KEY_SEED is required" >&2
            return 1
        fi
        echo "  [SKIP] CREDENTIAL_ENCRYPTION_KEY_SEED is missing"
        return 0
    fi
    if [ -z "$active" ]; then
        echo "  [FAIL] CREDENTIAL_ENCRYPTION_KEY must be set for seed verification" >&2
        return 1
    fi
    case "$active" in
        bao://*|openbao://*|vault://*)
            echo "  [FAIL] remove CREDENTIAL_ENCRYPTION_KEY_SEED after switching to an OpenBao reference" >&2
            return 1
            ;;
    esac
    if [ "$seed" != "$active" ]; then
        echo "  [FAIL] seed differs from the active credential key; refusing destructive replacement" >&2
        return 1
    fi

    existing="$(run_bao kv get -field=credential_encryption_key "secret/${path}" 2>/dev/null || true)"
    if [ -n "$existing" ]; then
        if [ "$existing" != "$seed" ]; then
            echo "  [FAIL] secret/${path} already contains a different credential key" >&2
            return 1
        fi
        echo "  [OK] secret/${path} already contains the verified active key"
        return 0
    fi
    if [ "$CHECK_ONLY" -eq 1 ]; then
        echo "  [CHECK] secret/${path} will receive the verified active key"
        return 0
    fi

    # Patch an existing payload so rotation metadata is never discarded. A new
    # install has no payload yet, so create it with the one verified field.
    if run_bao kv get "secret/${path}" >/dev/null 2>&1; then
        run_bao kv patch "secret/${path}" "credential_encryption_key=${seed}" >/dev/null
    else
        run_bao kv put "secret/${path}" "credential_encryption_key=${seed}" >/dev/null
    fi
    existing="$(run_bao kv get -field=credential_encryption_key "secret/${path}" 2>/dev/null || true)"
    if [ "$existing" != "$seed" ]; then
        echo "  [FAIL] secret/${path} key read-back did not match" >&2
        return 1
    fi
    echo "  [OK] secret/${path} seeded with the verified active key"
}

echo "==> Waiting for OpenBao to be ready..."
for i in $(seq 1 30); do
    if run_bao status -format=json 2>/dev/null | grep -q '"sealed":false'; then
        break
    fi
    sleep 1
done

echo "==> Seeding OpenBao KV v2 (real env values only)..."

seed_group settings/auth \
    "JWT_SECRET,TOTP_ENCRYPTION_KEY" \
    "jwt_secret=${JWT_SECRET:-}" \
    "totp_encryption_key=${TOTP_ENCRYPTION_KEY:-}"

seed_group settings/network \
    "WIREGUARD_KEY_ENCRYPTION_KEY" \
    "wireguard_key_encryption_key=${WIREGUARD_KEY_ENCRYPTION_KEY:-}"

seed_group settings/radius \
    "RADIUS_AUTH_SHARED_SECRET" \
    "auth_shared_secret=${RADIUS_AUTH_SHARED_SECRET:-}"

# One-time bootstrap only. The helper refuses a seed that differs from the
# active literal and never overwrites an existing managed key or its metadata.
seed_credential_keyring

# The trust anchor `prepaid_funding_attestation` verifies signed funding
# manifests against. A PUBLIC key, so it needs no confidentiality — it is held
# rather than stored as a setting because only OpenBao access may REPLACE it.
seed_optional_group settings/billing \
    "PREPAID_RECONSTRUCTION_ATTESTATION_PUBLIC_KEY" \
    "prepaid_reconstruction_attestation_public_key=${PREPAID_RECONSTRUCTION_ATTESTATION_PUBLIC_KEY:-}"

# The settings-encryption keyring, in the same JSON shape the kernel's
# SETTINGS_ENCRYPTION_KEYS variable accepts:
#   [{"key_id": "k1", "key": "<Fernet.generate_key()>", "status": "active"}]
# `key_id` must stay stable for the life of its material — it is written into
# every value the key encrypts. Rotation ADDS an entry and retires the old one.
seed_optional_group settings/crypto \
    "SETTINGS_ENCRYPTION_KEYRING" \
    "settings_encryption_keyring=${SETTINGS_ENCRYPTION_KEYRING:-}"

seed_group database \
    "DATABASE_URL,POSTGRES_PASSWORD" \
    "url=${DATABASE_URL:-}" \
    "password=${POSTGRES_PASSWORD:-}"

seed_group redis \
    "REDIS_PASSWORD,REDIS_URL,CELERY_BROKER_URL,CELERY_RESULT_BACKEND" \
    "password=${REDIS_PASSWORD:-}" \
    "url=${REDIS_URL:-}" \
    "broker_url=${CELERY_BROKER_URL:-}" \
    "result_backend=${CELERY_RESULT_BACKEND:-}"

seed_group paystack \
    "PAYSTACK_SECRET_KEY,PAYSTACK_PUBLIC_KEY" \
    "secret_key=${PAYSTACK_SECRET_KEY:-}" \
    "public_key=${PAYSTACK_PUBLIC_KEY:-}"

seed_group radius \
    "RADIUS_DB_PASS" \
    "db_password=${RADIUS_DB_PASS:-}" \
    "db_dsn=${RADIUS_DB_DSN:-}"

seed_group genieacs \
    "GENIEACS_MONGODB_PASSWORD,GENIEACS_UI_JWT_SECRET,GENIEACS_CWMP_USER,GENIEACS_CWMP_PASS" \
    "mongodb_dsn=${GENIEACS_MONGODB_DSN:-}" \
    "mongodb_password=${GENIEACS_MONGODB_PASSWORD:-}" \
    "jwt_secret=${GENIEACS_UI_JWT_SECRET:-}" \
    "cwmp_user=${GENIEACS_CWMP_USER:-}" \
    "cwmp_pass=${GENIEACS_CWMP_PASS:-}"

seed_group s3 \
    "S3_ACCESS_KEY,S3_SECRET_KEY" \
    "access_key=${S3_ACCESS_KEY:-}" \
    "secret_key=${S3_SECRET_KEY:-}"

seed_group migration \
    "SMARTOLT_API_KEY" \
    "smartolt_api_key=${SMARTOLT_API_KEY:-}"

seed_group notifications \
    "SMTP_PORT" \
    "smtp_host=${SMTP_HOST:-}" \
    "smtp_port=${SMTP_PORT:-}" \
    "smtp_username=${SMTP_USERNAME:-}" \
    "smtp_password=${SMTP_PASSWORD:-}" \
    "sms_api_key=${SMS_API_KEY:-}" \
    "sms_api_secret=${SMS_API_SECRET:-}"

echo ""
echo "==> Available OpenBao paths:"
run_bao kv list secret/ 2>/dev/null || echo "(listing unavailable)"
