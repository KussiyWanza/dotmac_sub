"""Every registered setting must have a behavior consumer ("no dead controls").

The systemic finding across the UX-polish audits: settings keys that the
generic settings UI (``web_system_settings_views`` iterates every
``SETTINGS_SPECS`` entry) lets an operator edit, but which NO runtime code
reads — so the toggle does nothing. Examples the audits caught: the old FUP
settings page, monitoring ``*_warn_pct``, dead notification prefs.

This lint fails the build when a registered setting key has no reference
anywhere in the codebase outside its own spec definition and the seed. A
"reference" is the key appearing as a quoted string literal in ``app/`` /
``templates/`` / ``scripts/`` — i.e. a ``resolve_value(..., "key")`` call, a
key list passed to ``_read_settings``/``resolve_values_atomic``, or a template
lookup. That is a necessary condition for the setting to affect behavior (not
sufficient — a literal in a hand-written settings page still counts — but it
reliably catches the fully-dead keys with zero plumbing and near-zero false
positives).

The historical orphan backlog was removed in July 2026. A registered key with
no reader now fails immediately; there is deliberately no allowlist.
"""

from __future__ import annotations

import pathlib

from app.services.settings_spec import SETTINGS_SPECS

# Files that define/seed keys are not "readers" — exclude from the corpus.
#
# The two `kernel_*` modules are excluded for a subtler reason, and it is the
# reason this guard once passed while four dead controls existed. They declare
# HELD material, and a held name is spelled the same as the settings key it
# replaced: `"jwt_secret"` appears in `kernel_secret_source` as the name a
# secret is asked for by, and in `auth_flow` as `held_secret("jwt_secret")`.
# Neither is a settings read, but both look like one to a corpus search — so
# `auth/jwt_secret` and three siblings kept their specs long after nothing read
# them. Excluding the declaration is half the fix;
# `test_no_setting_shares_a_key_with_held_material` below is the other half,
# and states the rule positively.
_EXCLUDED_FILES = {
    "app/services/settings_spec.py",
    "app/services/settings_seed.py",
    "app/services/kernel_secret_source.py",
    "app/services/kernel_key_provider.py",
}


def _repo_root() -> pathlib.Path:
    # tests/architecture/<this file> -> repo root
    return pathlib.Path(__file__).resolve().parents[2]


def _reader_corpus(root: pathlib.Path) -> str:
    chunks: list[str] = []
    for pattern in ("app/**/*.py", "templates/**/*.html", "scripts/**/*.py"):
        for path in root.glob(pattern):
            if str(path.relative_to(root)) in _EXCLUDED_FILES:
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except OSError:
                continue
    return "\n".join(chunks)


def _find_orphans() -> set[str]:
    corpus = _reader_corpus(_repo_root())
    keys = {spec.key for spec in SETTINGS_SPECS}
    return {k for k in keys if f'"{k}"' not in corpus and f"'{k}'" not in corpus}


def test_no_orphan_settings() -> None:
    orphans = _find_orphans()
    assert not orphans, (
        "Registered setting(s) with no reader (dead control): "
        f"{sorted(orphans)}. Either read the value somewhere it changes "
        "behavior, or drop it from SETTINGS_SPECS."
    )


def _held_field_names() -> set[str]:
    """The FIELD each held reference names — `bao://…#<field>`.

    The field, not the held name: they differ where a name is qualified
    (`radius_auth_shared_secret` holds `…/radius#auth_shared_secret`), and it
    is the FIELD that a settings key would have collided with.
    """

    import re

    from app.services.kernel_key_provider import KEYRING_REF
    from app.services.kernel_secret_source import (
        OPTIONAL_SECRET_REFS,
        SECRET_REFS,
    )

    refs = [*SECRET_REFS.values(), *OPTIONAL_SECRET_REFS.values(), KEYRING_REF]
    return {match.group(1) for ref in refs if (match := re.search(r"#(.+)$", ref))}


def test_no_setting_shares_a_key_with_held_material() -> None:
    """A value that is HELD is not a setting, so it must not also be declared.

    The rule from the 2026-08-10 classification: `is_secret` means
    CONFIDENTIAL — encrypt at rest, settings-write may change it — while a
    value whose AUTHORITY matters is held material, loaded at boot from a path
    named in code. A key declared in both places is a control an operator can
    edit that changes nothing, because the reader takes the held value.

    That is not hypothetical: `auth/jwt_secret`,
    `auth/credential_encryption_key`, `auth/totp_encryption_key` and
    `network/wireguard_key_encryption_key` were exactly this for a day, and the
    orphan check above could not see it — the key name still appeared in
    `app/`, as the name its held secret is asked for by.
    """

    declared = {spec.key for spec in SETTINGS_SPECS}
    collisions = sorted(declared & _held_field_names())
    assert not collisions, (
        f"setting(s) {collisions} are also held material — a held value is not "
        "a setting, and declaring both leaves an editable control that changes "
        "nothing. Retire the spec and its seed entry; the reader already takes "
        "the held value."
    )
