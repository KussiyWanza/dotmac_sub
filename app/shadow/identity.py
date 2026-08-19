"""Artifact identity for the shadow manifest: a digest, and a reference pinned to it.

This mirrors the rule `dotmac_release_catalog.identity` enforces vendor-side —
**an artifact is its content digest, never a tag** — because the shadow manifest
records release identity and would otherwise be the one place in the chain where
a mutable tag could enter.

It is a mirror rather than an import on purpose, and the distinction matters for
the one-writer rule: `dotmac-release-catalog` is the *publishing authority* and
is installable only in vendor/OEM control-plane assemblies. Sub is a product
data plane; it pins `dotmac-kernel==0.1.0a50` and does not install the
catalogue at all. What lives here is a *validator over local configuration*,
which decides nothing about what was published — so it is not a second writer of
release truth, and it must not grow into one. If Sub ever installs the
catalogue, delete this module and import from it.

Parsing refuses rather than repairs. A caller holding `wheel:0.1.0a1` has a bug
one layer up — it resolved a version where it should have resolved a digest —
and resolving the tag here would put a moment-in-time network decision inside a
value object, which is exactly how a mutable tag gets laundered into an
immutable-looking record.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, ConfigDict, model_validator

#: The one accepted algorithm, and its exact hex width. An allowlist rather than
#: `<alg>:<hex>` because an unrecognised algorithm is not a harmless unknown: a
#: weaker one that still parses would be compared for equality with the same
#: confidence as a strong one.
SHA256: Final[str] = "sha256"
_DIGEST_WIDTHS: Final[dict[str, int]] = {SHA256: 64}

_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^([a-z0-9]+):([0-9a-f]+)$")

#: `<anything>@<alg>:<hex>` anchored at the END — the trailing anchor is what
#: stops `repo@sha256:...:latest`, a pin with a tag appended, from passing.
_PINNED_REF_RE: Final[re.Pattern[str]] = re.compile(r"^(\S+)@([a-z0-9]+:[0-9a-f]+)$")


class ArtifactIdentityError(ValueError):
    """This value cannot serve as an artifact identity."""


class DigestError(ArtifactIdentityError):
    """A digest is malformed, or names an algorithm this manifest will not accept."""


class UnpinnedReferenceError(ArtifactIdentityError):
    """A reference names an artifact by something a publisher can move."""


class Digest(BaseModel):
    """A content address. Frozen, compared by value, valid by construction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: str
    hex_digest: str

    @model_validator(mode="after")
    def _check(self) -> Digest:
        width = _DIGEST_WIDTHS.get(self.algorithm)
        if width is None:
            raise DigestError(
                f"unsupported digest algorithm {self.algorithm!r}; "
                f"accepted: {', '.join(sorted(_DIGEST_WIDTHS))}"
            )
        if len(self.hex_digest) != width:
            raise DigestError(
                f"{self.algorithm} digest must be {width} hex characters, "
                f"got {len(self.hex_digest)}"
            )
        if _DIGEST_RE.fullmatch(str(self)) is None:
            raise DigestError(
                "digest must be lowercase hexadecimal; uppercase and non-hex "
                "characters are refused rather than normalised, so two "
                "spellings of one digest cannot both be stored"
            )
        return self

    @classmethod
    def parse(cls, value: str) -> Digest:
        """Parse `<algorithm>:<hex>`, or raise `DigestError`."""
        match = _DIGEST_RE.fullmatch(value.strip())
        if match is None:
            raise DigestError(
                f"{value!r} is not a digest; expected '<algorithm>:<hex>', "
                f"e.g. 'sha256:{'0' * 64}'"
            )
        return cls(algorithm=match.group(1), hex_digest=match.group(2))

    def __str__(self) -> str:
        return f"{self.algorithm}:{self.hex_digest}"


def pinned_reference(reference: str, *, expected: Digest | None = None) -> str:
    """Return `reference` unchanged, having proved it is digest-pinned.

    `expected`, when given, additionally proves the reference pins the digest the
    caller believes it does. Those two values sit in adjacent fields, and
    adjacent fields drift: a record whose reference pins *different* bytes than
    its digest field is a pin that passes every syntactic check and deploys the
    wrong artifact.
    """
    match = _PINNED_REF_RE.fullmatch(reference.strip())
    if match is None:
        raise UnpinnedReferenceError(
            f"{reference!r} is not digest-pinned. A reference must end in "
            "'@<algorithm>:<hex>' — a tag is a pointer the publisher can move "
            "after the plan naming it was approved."
        )
    digest = Digest.parse(match.group(2))
    if expected is not None and digest != expected:
        raise UnpinnedReferenceError(
            f"{reference!r} pins {digest}, but the recorded digest is "
            f"{expected}. The reference and the digest must address the same bytes."
        )
    return reference.strip()


__all__ = [
    "SHA256",
    "ArtifactIdentityError",
    "Digest",
    "DigestError",
    "UnpinnedReferenceError",
    "pinned_reference",
]
