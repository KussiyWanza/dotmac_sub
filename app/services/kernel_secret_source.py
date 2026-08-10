"""Sub's `SecretSource` — OpenBao, read once at startup.

ADR-0009 (`dotmac_starter_mt`): **a secret is held, never dereferenced.** Nothing
on a settings resolution path reaches a network, and a value that cannot be held
is not a setting. Material that lives in a secret store is read by the PRODUCT
and installed at boot; the kernel ships no client and never fetches.

This is that product half, over the OpenBao client Sub already has.

## Why these five

Per the classification ruled on 2026-08-09, five specs stop being settings:

* `auth/credential_encryption_key`
* `auth/totp_encryption_key`
* `network/wireguard_key_encryption_key`

  Each encrypts data **in this same database**. Holding them as settings — even
  encrypted at rest — would put the key inside the store it protects, secured by
  another key facing the identical question. Sub's existing `bao://` handling for
  these is already correct; this moves *where the code looks*, not where the
  secret lives.

* `auth/jwt_secret`
* `radius/auth_shared_secret`

  Dotmac issues and rotates both, and both are boot-stable, so holding them in
  memory costs nothing on the read path and keeps rotation central.

The other seven (`smtp_password`, the geocoding keys, `meta_app_secret`, the
vLLM keys, `voice_transcription_api_key`) are third-party credentials Sub holds
a COPY of. Those become real settings encrypted at rest — a settings row gives
history, an actor and an admin surface that a vault path does not.

## Failure behaviour is the whole contract

`load` RAISES when OpenBao is unreachable. It must never return a partial or
empty mapping for that case: empty is indistinguishable from "nothing is
configured", and the kernel would install it as a successful load. So this uses
`resolve_openbao_ref`, which raises, and NOT `get_secret`, which swallows every
exception and returns a default — convenient for a caller with a fallback,
catastrophic for a source whose silence the kernel would trust.

`install_secret_source` then fails the boot rather than starting with secrets it
could not fetch. That is deliberate: a process that starts without them would
discover it at the first request that needed one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from app.services.secrets import resolve_openbao_ref

logger = logging.getLogger(__name__)

# Secret name -> the OpenBao reference holding it. The names are what
# application code asks for via `get_secret(name)`; the references are where
# this deployment keeps them, and are the only place those paths appear.
SECRET_REFS: Mapping[str, str] = {
    "credential_encryption_key": "bao://secret/settings/auth#credential_encryption_key",
    "totp_encryption_key": "bao://secret/settings/auth#totp_encryption_key",
    "wireguard_key_encryption_key": (
        "bao://secret/settings/network#wireguard_key_encryption_key"
    ),
    "jwt_secret": "bao://secret/settings/auth#jwt_secret",
    "radius_auth_shared_secret": "bao://secret/settings/radius#auth_shared_secret",
}


class OpenBaoSecretSource:
    """Loads Sub's boot-time secret material from OpenBao.

    Satisfies `dotmac_kernel.secret_sources.SecretSource` structurally — the
    kernel declares the protocol and holds the result; it never learns what
    OpenBao is, and takes no dependency on this client.
    """

    def __init__(self, refs: Mapping[str, str] | None = None) -> None:
        self._refs = dict(refs if refs is not None else SECRET_REFS)

    def load(self) -> Mapping[str, str]:
        """Every secret this deployment holds, by name.

        Raises rather than returning a partial mapping: a missing secret and an
        unreachable store are both reasons to stop, and the kernel treats a
        successful return as the complete set.
        """
        loaded: dict[str, str] = {}
        for name, reference in self._refs.items():
            # `resolve_openbao_ref` raises; `get_secret` would swallow and
            # return a default, which the kernel would install as success.
            loaded[name] = resolve_openbao_ref(reference)
        logger.info("Loaded %d secret(s) from OpenBao", len(loaded))
        return loaded


def install() -> tuple[str, ...]:
    """Install the source at startup. Returns the names loaded, never values."""
    from dotmac_kernel.secret_sources import install_secret_source

    return install_secret_source(OpenBaoSecretSource())


def install_if_configured() -> tuple[str, ...]:
    """Install the source when this deployment names an OpenBao. Boot entry point.

    Gated on `is_openbao_configured`, which reads configuration and performs no
    I/O — NOT on `is_openbao_available`, which probes the store. The difference
    is the whole contract:

    * **Not configured** — a developer machine, a CI shard, an install that
      keeps these five in the environment. Nothing is held, `get_secret`
      answers None, and every reader falls back to its environment variable
      exactly as before. Returns an empty tuple.
    * **Configured but unreachable** — an outage, a bad token, a wrong address.
      This RAISES and the boot fails. That is deliberate and it is the reason
      the gate cannot be a reachability probe: a probe would answer
      "unavailable", skip the install, and hand every reader a `None` that
      reads as "not configured" — a total loss of credential encryption
      reported as a warning line. A process that cannot get the secrets it was
      told to hold has not started correctly, and saying so at boot is cheaper
      than discovering it at the first request that needed one.
    """

    from app.services.secrets import is_openbao_configured

    if not is_openbao_configured():
        logger.info(
            "No OpenBao configured; holding no boot secrets "
            "(readers fall back to their environment variables)"
        )
        return ()
    return install()
