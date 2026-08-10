"""Credential encryption utilities.

Provides Fernet encryption for storing sensitive credentials at rest.
Follows the same pattern as wireguard_crypto.py for consistency.

SECURITY: In production, CREDENTIAL_ENCRYPTION_KEY must be set.
Use require_encryption_key() at startup to enforce this.
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

# Environment variable for the encryption key
_ENCRYPTION_KEY_ENV = "CREDENTIAL_ENCRYPTION_KEY"
_PREVIOUS_ENCRYPTION_KEY_ENV = "CREDENTIAL_ENCRYPTION_PREVIOUS_KEY"
_logger = logging.getLogger(__name__)
_encryption_warning_logged = False
_encryption_key_required = (
    False  # Set to True in production via require_encryption_key()
)

ENCRYPTED_MODEL_FIELDS: dict[str, tuple[str, ...]] = {
    "NasDevice": (
        "shared_secret",
        "ssh_password",
        "ssh_key",
        "api_password",
        "api_token",
        "snmp_community",
    ),
    "NetworkDevice": (
        "snmp_community",
        "snmp_rw_community",
        "snmp_auth_secret",
        "snmp_priv_secret",
    ),
    "AccessCredential": ("secret_hash",),
    "OLTDevice": (
        "ssh_password",
        "snmp_ro_community",
        "snmp_rw_community",
        "api_password",
        "api_token",
    ),
    "OntUnit": ("pppoe_password",),
    "OntProfileWanService": ("pppoe_static_password",),
    "Tr069AcsServer": ("cwmp_password", "connection_request_password"),
    "PaymentMethod": ("token",),
    "BankAccount": ("token",),
    "SystemUser": ("device_login_secret",),
    "Router": ("rest_api_username", "rest_api_password"),
    "JumpHost": ("ssh_key", "ssh_password"),
}


def _as_key_bytes(value: object) -> bytes | None:
    """A Fernet key as bytes, or None when the material cannot be one.

    Split out of `_resolve_key_candidate` so a value that is ALREADY secret
    material — one held in memory since boot — can be encoded without being
    passed through reference resolution, which would put a network call on the
    decryption path for a string that only happened to look like a reference.
    """

    if not value:
        return None
    if isinstance(value, bytes):
        return value
    try:
        return str(value).encode("ascii")
    except UnicodeEncodeError:
        return None


def _resolve_key_candidate(value: str | bytes | None) -> bytes | None:
    if not value:
        return None
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        return None
    try:
        from app.services.secrets import resolve_secret

        resolved = resolve_secret(value)
    except Exception:
        _logger.debug(
            "Credential encryption key reference lookup failed", exc_info=True
        )
        return None
    return _as_key_bytes(resolved)


def get_encryption_key() -> bytes | None:
    """Get the Fernet encryption key from the environment or the held secrets.

    Checks in order:
    1. Environment variable CREDENTIAL_ENCRYPTION_KEY
    2. The secret held at boot (`app/services/kernel_secret_source.py`)

    Two lookups replace three, and neither touches the network. The two that
    went are the same value reached two other ways: the
    `auth/credential_encryption_key` SETTING, whose column holds a `bao://`
    reference this function dereferenced, and a direct OpenBao read of the
    legacy `auth` path. Both put a network call on the path that decrypts a
    device credential — under `ont_reconcile`, that is once per device — and
    both swallowed every failure into a debug line, so an outage looked
    identical to "not configured" and the fallback was to store credentials in
    the clear.

    Starter ADR-0009 states the rule this now follows: a secret is held, never
    dereferenced. `kernel_secret_source` reads
    `bao://secret/settings/auth#credential_encryption_key` once at boot, which
    is the same material the removed paths reached.

    Returns:
        Fernet key bytes if set, None otherwise
    """
    global _encryption_warning_logged

    # Explicit runtime config still wins, and `_resolve_key_candidate` keeps
    # honouring an `env://`/`bao://` reference given THERE — an operator naming
    # a reference in a variable is asking for a lookup, which is different from
    # a stored row silently causing one.
    key = _resolve_key_candidate(os.environ.get(_ENCRYPTION_KEY_ENV))

    if not key:
        from dotmac_kernel.secret_sources import get_secret as held_secret

        key = _as_key_bytes(held_secret("credential_encryption_key"))

    if not key:
        if not _encryption_warning_logged:
            _logger.warning(
                "CREDENTIAL_ENCRYPTION_KEY not configured. "
                "NAS device credentials will be stored unencrypted."
            )
            _encryption_warning_logged = True
        return None

    return key


def get_previous_encryption_key() -> bytes | None:
    """Return the temporary previous key retained during scheduled rotation."""
    key = _resolve_key_candidate(os.environ.get(_PREVIOUS_ENCRYPTION_KEY_ENV))
    if key:
        return key
    try:
        from app.services.secrets import get_secret

        value = get_secret("settings/auth", "credential_encryption_previous_key")
        key = _resolve_key_candidate(value)
        if key:
            return key
        return _resolve_key_candidate(
            get_secret("auth", "credential_encryption_previous_key")
        )
    except Exception:
        _logger.debug("Previous credential encryption key lookup failed", exc_info=True)
        return None


def _decryption_keys(*, refresh: bool = False) -> tuple[bytes, ...]:
    if refresh:
        try:
            from app.services.secrets import clear_cache

            clear_cache()
        except Exception:
            pass
    keys: list[bytes] = []
    for key in (get_encryption_key(), get_previous_encryption_key()):
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def generate_encryption_key() -> str:
    """Generate a new Fernet encryption key.

    Returns:
        URL-safe base64-encoded Fernet key

    Use this to generate a key for the CREDENTIAL_ENCRYPTION_KEY env var:
        python -c "from app.services.credential_crypto import generate_encryption_key; print(generate_encryption_key())"
    """
    return Fernet.generate_key().decode("ascii")


class EncryptionKeyMissingError(Exception):
    """Raised when encryption key is required but not configured."""

    pass


def require_encryption_key(*, enforce: bool = True) -> bytes | None:
    """Require encryption key to be configured.

    Call this at application startup to enforce encryption in production.
    After calling with enforce=True, all credential encryption operations
    will fail if the key is not configured (instead of falling back to plaintext).

    Args:
        enforce: If True, raise error if key not configured. If False,
                 just return the key (or None if not configured).

    Returns:
        The encryption key bytes if configured.

    Raises:
        EncryptionKeyMissingError: If enforce=True and key not configured.

    Example:
        # In app startup (main.py or similar):
        from app.services.credential_crypto import require_encryption_key
        require_encryption_key(enforce=not settings.debug)
    """
    global _encryption_key_required

    key = get_encryption_key()

    if enforce:
        _encryption_key_required = True
        if not key:
            raise EncryptionKeyMissingError(
                "CREDENTIAL_ENCRYPTION_KEY must be configured in production. "
                "Set via environment variable, database setting, or OpenBao. "
                "Generate a key with: python -c "
                '"from app.services.credential_crypto import generate_encryption_key; '
                'print(generate_encryption_key())"'
            )

    return key


def is_encryption_required() -> bool:
    """Check if encryption key enforcement is enabled."""
    return _encryption_key_required


def _coerce_encryption_key(encryption_key: str | bytes | None) -> bytes | None:
    if not encryption_key:
        return None
    if isinstance(encryption_key, bytes):
        return encryption_key
    if not isinstance(encryption_key, str):
        raise TypeError("encryption_key must be a str, bytes, or None")
    try:
        return encryption_key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("encryption_key must be ASCII-safe Fernet text") from exc


def is_encrypted(value: str | None) -> bool:
    """Check if a value is already encrypted.

    Args:
        value: The value to check

    Returns:
        True if the value has 'enc:' or 'plain:' prefix
    """
    if not value:
        return False
    return value.startswith(("enc:", "plain:"))


def _is_secret_reference(value: str | None) -> bool:
    if not value:
        return False
    try:
        from app.services.secrets import is_secret_ref

        return is_secret_ref(value)
    except Exception:
        return False


def encrypt_credential(value: str | None) -> str | None:
    """Encrypt a credential for storage at rest.

    If no encryption key is configured, returns the credential unchanged
    but prefixed with "plain:" for identification.

    Args:
        value: Plain credential value to encrypt

    Returns:
        Encrypted credential (prefixed with "enc:") or plain credential
        (prefixed with "plain:"), or None if input is None/empty
    """
    if not value:
        return value

    if _is_secret_reference(value):
        return value

    # Don't double-encrypt, but do not preserve plaintext once enforcement is active.
    if value.startswith("enc:"):
        return value
    if value.startswith("plain:"):
        if _encryption_key_required:
            return encrypt_credential_with_key(value[6:], get_encryption_key())
        return value

    return encrypt_credential_with_key(value, get_encryption_key())


def encrypt_credential_with_key(
    value: str | None, encryption_key: str | bytes | None
) -> str | None:
    """Encrypt a credential using an explicit Fernet key.

    If ``encryption_key`` is absent and encryption is not required,
    the value is stored with a ``plain:`` prefix.

    Raises:
        EncryptionKeyMissingError: If encryption is required but key is missing.
    """
    if not value:
        return value
    if is_encrypted(value):
        return value

    key_bytes = _coerce_encryption_key(encryption_key)
    if not key_bytes:
        if _encryption_key_required:
            raise EncryptionKeyMissingError(
                "Cannot store credential: CREDENTIAL_ENCRYPTION_KEY is required "
                "but not configured. This is a security policy violation."
            )
        _logger.warning(
            "CREDENTIAL_ENCRYPTION_KEY not configured — storing credential as plaintext"
        )
        return f"plain:{value}"

    fernet = Fernet(key_bytes)
    encrypted = fernet.encrypt(value.encode("utf-8"))
    return f"enc:{encrypted.decode('ascii')}"


def decrypt_credential(value: str | None) -> str | None:
    """Decrypt a credential from storage.

    Handles encrypted (enc:), plain (plain:), and legacy (no prefix) formats.

    Args:
        value: Stored credential with prefix

    Returns:
        Decrypted/plain credential value, or None if input is None/empty

    Raises:
        ValueError: If decryption fails
    """
    if not value:
        return value

    if _is_secret_reference(value):
        try:
            from app.services.secrets import resolve_secret

            resolved = resolve_secret(value)
        except Exception as e:
            raise ValueError(
                f"Failed to resolve credential secret reference: {e}"
            ) from e
        # A credential can be both encryption-at-rest wrapped (``enc:``/``plain:``)
        # *and* stored behind a secret reference — the resolved value is then
        # still an at-rest blob, not the plaintext. Peel that inner layer too,
        # otherwise callers (config masking, outbound auth headers) receive
        # ciphertext. The ``resolved != value`` guard avoids re-resolving a
        # reference that resolves to itself.
        if resolved and resolved != value and is_encrypted(resolved):
            return decrypt_credential(resolved)
        return resolved

    if value.startswith("plain:"):
        return value[6:]

    if value.startswith("enc:"):
        last_error: ValueError | None = None
        for refresh in (False, True):
            for key in _decryption_keys(refresh=refresh):
                try:
                    return decrypt_credential_with_key(value, key)
                except ValueError as exc:
                    last_error = exc
        if last_error:
            raise last_error
        raise ValueError(
            "Encrypted credential found but CREDENTIAL_ENCRYPTION_KEY not set"
        )

    # Legacy format (no prefix) - treat as plain
    return value


def decrypt_credential_with_key(
    value: str | None, encryption_key: str | bytes | None
) -> str | None:
    """Decrypt a credential using an explicit Fernet key.

    Legacy values without an ``enc:`` or ``plain:`` prefix are treated as plaintext.
    """
    if not value:
        return value

    if _is_secret_reference(value):
        return value

    if value.startswith("plain:"):
        return value[6:]

    if value.startswith("enc:"):
        key_bytes = _coerce_encryption_key(encryption_key)
        if not key_bytes:
            raise ValueError(
                "Encrypted credential found but CREDENTIAL_ENCRYPTION_KEY not set"
            )
        fernet = Fernet(key_bytes)
        try:
            decrypted = fernet.decrypt(value[4:].encode("ascii"))
            return decrypted.decode("utf-8")
        except InvalidToken as e:
            raise ValueError("Failed to decrypt credential: invalid token") from e
        except Exception as e:
            raise ValueError(f"Failed to decrypt credential: {e}") from e

    return value


# Credential field names that should be encrypted
ENCRYPTED_CREDENTIAL_FIELDS = frozenset(
    field for fields in ENCRYPTED_MODEL_FIELDS.values() for field in fields
)
ENCRYPTED_NAS_CREDENTIAL_FIELDS = frozenset(ENCRYPTED_MODEL_FIELDS["NasDevice"])


def encrypt_nas_credentials(data: dict) -> dict:
    """Encrypt all credential fields in a NAS device data dict.

    Args:
        data: Dictionary containing NAS device fields

    Returns:
        Dictionary with credential fields encrypted
    """
    result = dict(data)
    for field in ENCRYPTED_NAS_CREDENTIAL_FIELDS:
        if field in result and result[field]:
            result[field] = encrypt_credential(result[field])
    return result
