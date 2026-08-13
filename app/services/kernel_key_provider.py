"""Sub's `KeyProvider` — the settings-encryption keyring, held from OpenBao.

`dotmac_kernel.settings_crypto` encrypts a secret setting's value at rest, and
needs key material to do it. The kernel reads the environment by default and
ships no secret-store client; a product whose keys live in a store supplies a
`KeyProvider` instead. This is Sub's, over the OpenBao client it already has.

It is the exact sibling of `app/services/kernel_secret_source.py`: one method,
loaded once at boot by `install_key_provider`, held in memory thereafter. That
is what makes reading KEYS from a network store safe when reading VALUES from
one is not — settings resolution is a per-request path, and a key fetched at
startup is already in the process when the store goes down an hour later.
Rotation is an explicit `refresh_keys()`, never a side effect of a read.

## Why not `SECRET_REFS`

`kernel_secret_source` loads an all-or-nothing set: a name it cannot fetch
fails the whole load, deliberately, because a partial set is indistinguishable
from a misconfiguration. The keyring cannot join it, because a deployment that
has not provisioned one yet must still boot — every secret setting Sub holds
today is a `bao://` reference, and encryption becomes possible before it
becomes used.

So this fetches its own reference and distinguishes two failures the way the
kernel requires:

* **The path does not exist** (404) — no keyring is configured. Returns
  nothing, the kernel builds an empty keyring, and writing a secret setting
  raises `SettingsEncryptionError` exactly as it does with no key at all. This
  is the state a deployment is in until the keyring is created.
* **Anything else** — unreachable, bad token, wrong address, a malformed
  keyring. RAISES, and `install_key_provider` fails the boot. Returning nothing
  here would be indistinguishable from "not configured yet" and would silently
  degrade every secret write.

## The stored shape

One OpenBao field holding the same JSON the kernel's `SETTINGS_ENCRYPTION_KEYS`
variable accepts — a list of `{"key_id": …, "key": …, "status": …}` — so the
two sources have one format and a deployment can move between them without a
re-encryption. `status` defaults to `active` and may be `retired` (still
decrypts, encrypts nothing new) or `revoked` (decrypts nothing, on purpose).

**`key_id` must be stable for the life of the key material.** The stored form
is `enc:<key_id>:<token>`, so the id in a row is how a later read finds the key
that wrote it. Renaming an id, or reusing one for new material, makes every
value written under it unreadable — which the read path degrades to the spec
default, substituting a default for a credential. Rotation therefore ADDS an
entry and marks the old one `retired`; it never edits one in place.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from dotmac_kernel.settings_crypto import EncryptionKey, KeyringError, KeyStatus

from app.services.secrets import resolve_openbao_ref_optional

logger = logging.getLogger(__name__)

#: Where this deployment keeps its settings-encryption keyring. The only place
#: this path appears.
KEYRING_REF = "bao://secret/settings/crypto#settings_encryption_keyring"


def _parse(raw: str) -> tuple[EncryptionKey, ...]:
    """The stored JSON as kernel keys, or `KeyringError` naming what is wrong.

    Validated here rather than trusted, because the alternative to a clear
    error at boot is a `Keyring` construction failure with no indication that
    OpenBao is where the bad entry came from. Values are never quoted in a
    message — only ids and field names.
    """

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KeyringError(f"{KEYRING_REF} does not contain valid JSON") from exc
    if not isinstance(entries, list):
        raise KeyringError(f"{KEYRING_REF} must contain a JSON list of keys")

    keys: list[EncryptionKey] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise KeyringError(f"{KEYRING_REF}[{index}] must be an object")
        missing = {"key_id", "key"} - entry.keys()
        if missing:
            raise KeyringError(f"{KEYRING_REF}[{index}] is missing {sorted(missing)}")
        try:
            status = KeyStatus(str(entry.get("status", "active")))
        except ValueError as exc:
            raise KeyringError(
                f"{KEYRING_REF}[{index}].status must be one of "
                f"{[member.value for member in KeyStatus]}"
            ) from exc
        keys.append(
            EncryptionKey(
                key_id=str(entry["key_id"]),
                material=str(entry["key"]),
                status=status,
            )
        )
    return tuple(keys)


class OpenBaoKeyProvider:
    """Loads the settings-encryption keyring from OpenBao.

    Satisfies `dotmac_kernel.settings_crypto.KeyProvider` structurally — the
    kernel declares the protocol and builds the `Keyring` from what this
    returns, so the duplicate-id, malformed-id and one-active-key checks cannot
    be skipped by returning a hand-built one.
    """

    def __init__(self, reference: str = KEYRING_REF) -> None:
        self._reference = reference

    def load_keys(self) -> Iterable[EncryptionKey]:
        """Every key this deployment decrypts with, retired ones included."""

        # `_optional` returns None only for a path that does not exist. Every
        # other failure propagates — see the module docstring on why those two
        # must not collapse into one answer. The status-code check lives with
        # the client in `app.services.secrets`, so this module needs no FastAPI
        # type to make the distinction.
        raw = resolve_openbao_ref_optional(self._reference)
        if raw is None:
            logger.info(
                "No settings-encryption keyring in OpenBao; secret settings "
                "cannot be written until one is created"
            )
            return ()
        keys = _parse(raw)
        logger.info(
            "Loaded settings-encryption keyring: %s",
            ", ".join(f"{key.key_id}={key.status.value}" for key in keys) or "empty",
        )
        return keys


def install_if_configured() -> tuple[str, ...]:
    """Install the provider when this deployment names an OpenBao.

    Returns the key ids loaded, never material. Gated on configuration and not
    on reachability, for the same reason as
    `kernel_secret_source.install_if_configured`: a reachability probe would
    skip the install during an outage and leave the process quietly unable to
    write a secret, which is a failure an operator meets hours later at a
    settings screen rather than at boot.
    """

    from dotmac_kernel.settings_crypto import install_key_provider

    from app.services.secrets import is_openbao_configured

    if not is_openbao_configured():
        logger.info(
            "No OpenBao configured; settings encryption falls back to the "
            "kernel's environment keyring"
        )
        return ()
    keyring = install_key_provider(OpenBaoKeyProvider())
    return tuple(key.key_id for key in keyring.keys)
