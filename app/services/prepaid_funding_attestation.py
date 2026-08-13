"""Detached trust boundary for final prepaid reconstruction manifests."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy.orm import Session

SEALED_MANIFEST_SCHEMA = "dotmac.prepaid_funding_sealed_manifest.v1"
RECONSTRUCTION_MANIFEST_SCHEMA = "dotmac.prepaid_funding_reconstruction.v1"
ATTESTATION_SCHEMA = "dotmac.prepaid_funding_replay_attestation.v1"
ATTESTATION_ALGORITHM = "ed25519"
#: The held-secret NAME, not a settings key. The value moved out of
#: `domain_settings` — see `resolve_trusted_public_key_pem`.
TRUST_KEY_NAME = "prepaid_attestation_public_key"

_SEALED_FIELDS = {"schema", "manifest", "attestation"}
_ATTESTATION_FIELDS = {
    "schema",
    "algorithm",
    "key_fingerprint_sha256",
    "manifest_payload_sha256",
    "signed_at",
    "signature_base64",
}


class PrepaidFundingAttestationError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedPrepaidFundingAttestation:
    envelope_sha256: str
    manifest_payload_sha256: str
    key_fingerprint_sha256: str
    signed_at: datetime


def canonical_payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def candidate_cohort_sha256(account_ids: list[str] | tuple[str, ...]) -> str:
    return canonical_payload_sha256({"account_ids": sorted(set(account_ids))})


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _as_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None:
        raise PrepaidFundingAttestationError(f"{label} must include a timezone")
    return value.astimezone(UTC)


def _parse_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PrepaidFundingAttestationError(
            f"{label} must be a timezone-aware timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrepaidFundingAttestationError(f"{label} is invalid") from exc
    return _as_utc(parsed, label=label)


def _timestamp_text(value: datetime) -> str:
    return _as_utc(value, label="signed_at").isoformat().replace("+00:00", "Z")


def _load_private_key(private_key_pem: str) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
    except (TypeError, ValueError) as exc:
        raise PrepaidFundingAttestationError(
            "reconstruction signing key is not a valid unencrypted PEM key"
        ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise PrepaidFundingAttestationError(
            "reconstruction signing key must be Ed25519"
        )
    return key


def _load_public_key(public_key_pem: str) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise PrepaidFundingAttestationError(
            "configured reconstruction trust key is not valid PEM"
        ) from exc
    if not isinstance(key, Ed25519PublicKey):
        raise PrepaidFundingAttestationError(
            "configured reconstruction trust key must be Ed25519"
        )
    return key


def _public_key_fingerprint(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()


def public_key_pem(private_key_pem: str) -> str:
    """Derive the public trust anchor without exposing private key material."""
    public_key = _load_private_key(private_key_pem).public_key()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def sign_prepaid_funding_manifest(
    manifest: dict[str, Any],
    *,
    private_key_pem: str,
    signed_at: datetime | None = None,
) -> dict[str, Any]:
    """Return one content-bound sealed manifest signed by the audit authority."""
    if manifest.get("schema") != RECONSTRUCTION_MANIFEST_SCHEMA:
        raise PrepaidFundingAttestationError(
            "cannot sign an unsupported reconstruction manifest schema"
        )
    key = _load_private_key(private_key_pem)
    effective_signed_at = _as_utc(signed_at or datetime.now(UTC), label="signed_at")
    captured_at = _parse_timestamp(manifest.get("captured_at"), label="captured_at")
    if effective_signed_at < captured_at:
        raise PrepaidFundingAttestationError(
            "reconstruction attestation cannot predate its manifest"
        )
    statement = {
        "schema": ATTESTATION_SCHEMA,
        "algorithm": ATTESTATION_ALGORITHM,
        "key_fingerprint_sha256": _public_key_fingerprint(key.public_key()),
        "manifest_payload_sha256": canonical_payload_sha256(manifest),
        "signed_at": _timestamp_text(effective_signed_at),
    }
    signature = key.sign(_canonical_json(statement))
    return {
        "schema": SEALED_MANIFEST_SCHEMA,
        "manifest": manifest,
        "attestation": {
            **statement,
            "signature_base64": base64.b64encode(signature).decode("ascii"),
        },
    }


def resolve_trusted_public_key_pem(db: Session) -> str:
    """The trust anchor, from material held at boot rather than a settings row.

    This anchor decides whether a signed reconstruction manifest is believed,
    so what protects it is not confidentiality — it is a public key — but
    AUTHORITY: only someone with OpenBao access may replace it, because
    replacing it means forged manifests verify.

    It used to be a `billing` setting holding a `bao://` reference, guarded by
    "must be an OpenBao reference". That guard checked the value WAS a
    reference and never WHICH reference, so anyone able to write settings could
    repoint it at a key they controlled — the protection it appeared to give
    was not there. It is now held from a path named in
    `kernel_secret_source.OPTIONAL_SECRET_REFS`, which the settings surface
    cannot reach at all.

    `db` is kept in the signature: every caller has one, the change is not
    theirs to care about, and a later anchor that IS per-something will need it.
    """

    from dotmac_kernel.secret_sources import get_secret

    public_key = str(get_secret(TRUST_KEY_NAME) or "").strip()
    if not public_key:
        # Same outcome as before for an unconfigured deployment: verification
        # fails loudly at use, rather than the boot failing over material a
        # deployment not using this feature never had.
        raise PrepaidFundingAttestationError(
            "no reconstruction attestation public key is held — provision "
            "the OpenBao path in kernel_secret_source.OPTIONAL_SECRET_REFS"
        )
    return public_key


def verify_prepaid_funding_manifest(
    db: Session,
    sealed_payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], VerifiedPrepaidFundingAttestation]:
    """Verify the sealed manifest against the configured OpenBao trust anchor."""
    if set(sealed_payload) != _SEALED_FIELDS:
        raise PrepaidFundingAttestationError(
            "sealed reconstruction manifest must contain schema, manifest, and "
            "attestation only"
        )
    if sealed_payload.get("schema") != SEALED_MANIFEST_SCHEMA:
        raise PrepaidFundingAttestationError(
            "unsupported sealed reconstruction manifest schema"
        )
    manifest = sealed_payload.get("manifest")
    attestation = sealed_payload.get("attestation")
    if not isinstance(manifest, dict) or not isinstance(attestation, dict):
        raise PrepaidFundingAttestationError(
            "sealed reconstruction manifest sections must be objects"
        )
    if manifest.get("schema") != RECONSTRUCTION_MANIFEST_SCHEMA:
        raise PrepaidFundingAttestationError(
            "unsupported reconstruction manifest schema"
        )
    if set(attestation) != _ATTESTATION_FIELDS:
        raise PrepaidFundingAttestationError(
            "reconstruction attestation fields are incomplete or unexpected"
        )
    if attestation.get("schema") != ATTESTATION_SCHEMA:
        raise PrepaidFundingAttestationError(
            "unsupported reconstruction attestation schema"
        )
    if attestation.get("algorithm") != ATTESTATION_ALGORITHM:
        raise PrepaidFundingAttestationError(
            "unsupported reconstruction attestation algorithm"
        )

    public_key = _load_public_key(resolve_trusted_public_key_pem(db))
    fingerprint = _public_key_fingerprint(public_key)
    presented_fingerprint = (
        str(attestation.get("key_fingerprint_sha256") or "").strip().lower()
    )
    if presented_fingerprint != fingerprint:
        raise PrepaidFundingAttestationError(
            "reconstruction attestation signer is not the configured trust key"
        )
    manifest_hash = canonical_payload_sha256(manifest)
    if str(attestation.get("manifest_payload_sha256") or "").strip().lower() != (
        manifest_hash
    ):
        raise PrepaidFundingAttestationError(
            "reconstruction attestation does not match the manifest content"
        )
    signed_at = _parse_timestamp(attestation.get("signed_at"), label="signed_at")
    captured_at = _parse_timestamp(manifest.get("captured_at"), label="captured_at")
    if signed_at < captured_at:
        raise PrepaidFundingAttestationError(
            "reconstruction attestation predates its manifest"
        )
    effective_now = _as_utc(now or datetime.now(UTC), label="now")
    if signed_at > effective_now:
        raise PrepaidFundingAttestationError(
            "reconstruction attestation was signed in the future"
        )

    statement = {
        key: attestation[key] for key in attestation if key != "signature_base64"
    }
    try:
        signature = base64.b64decode(
            str(attestation["signature_base64"]), validate=True
        )
    except (binascii.Error, ValueError) as exc:
        raise PrepaidFundingAttestationError(
            "reconstruction attestation signature encoding is invalid"
        ) from exc
    try:
        public_key.verify(signature, _canonical_json(statement))
    except InvalidSignature as exc:
        raise PrepaidFundingAttestationError(
            "reconstruction attestation signature is invalid"
        ) from exc

    return manifest, VerifiedPrepaidFundingAttestation(
        envelope_sha256=canonical_payload_sha256(sealed_payload),
        manifest_payload_sha256=manifest_hash,
        key_fingerprint_sha256=fingerprint,
        signed_at=signed_at,
    )
