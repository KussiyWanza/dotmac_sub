"""Deployment guardrails for composed commercial module prerequisites."""

from __future__ import annotations

import ast
import configparser
import errno
import importlib.util
import os
import re
import stat
import subprocess
import tomllib
from collections.abc import Iterable
from pathlib import Path

import pytest

from app.commercial_module_prereqs import (
    COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT,
    module_schema_contract,
)
from app.migration_schema_ops import declared_idempotent_schema_create_target

ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = ROOT / "alembic.ini"
MIGRATION_546 = ROOT / "alembic" / "versions" / "546_module_db_roles_prereq.py"
DEPLOY = ROOT / "scripts" / "deploy.sh"
BOOTSTRAP = ROOT / "scripts" / "bootstrap_commercial_module_prereqs.py"


def _executed_sql(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    statements: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute) and function.attr == "execute"):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                statements.append(argument.value)
            elif isinstance(argument, ast.JoinedStr):
                statements.append(ast.unparse(argument))
    return "\n".join(statements)


def _declared_lineages() -> tuple[str, ...]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ALEMBIC_INI)
    entries = parser["alembic"]["version_locations"].split()
    return tuple(
        entry.removesuffix(".migrations:versions")
        for entry in entries
        if entry.endswith(".migrations:versions")
    )


def test_the_schema_set_is_derived_rather_than_restated() -> None:
    """Derivation is the guard, so prove the chain is intact end to end.

    The contract used to be a hand-written tuple asserted equal to
    `alembic.ini`. That caught a missing entry but not a missing *environment*:
    the tuple and the five prose lists still had to be edited by hand, and on
    2026-08-31 `mod_inbox` was in the tuple and in none of the prose.
    """
    derived = module_schema_contract()
    assert {item.import_name for item in derived} == set(_declared_lineages())
    assert derived, "the derivation must not silently produce an empty contract"

    for item in derived:
        assert item.schema.startswith("mod_"), item.schema
        assert item.owner_role == "dotmac_app"
        assert item.usage_roles == ("app_admin", "app_user", "platform_api")

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = {
        requirement.split("==")[0]
        for requirement in data["project"]["dependencies"]
        if requirement.startswith("dotmac-")
    }
    assert {item.distribution for item in derived} <= dependencies


def test_the_rendered_schema_document_is_the_only_list() -> None:
    """One derived document, and the prose must not grow a rival.

    `make schema-contract-check` is the byte comparison; this is the guard that
    the documents which used to carry their own lists now point at it instead.

    The premise is deliberately about ENUMERATIONS, not mentions. Three of these
    documents are dated historical records, and a sentence like "installed under
    `mod_billing` and `mod_coll`" is a statement about two specific modules, not
    a copy of the required set — rewriting that would be falsifying history to
    satisfy a checker. What went stale was the five-item list, restated in three
    places and edited by hand. So: no line may name three or more module
    schemas, which is what a list looks like and what contextual prose does not.
    """
    schemas = {item.schema for item in module_schema_contract()}

    rendered_path = ROOT / "docs" / "generated" / "MODULE_SCHEMA_CONTRACT.md"
    rendered = rendered_path.read_text(encoding="utf-8")
    for schema in schemas:
        assert f"`{schema}`" in rendered

    offenders: list[str] = []
    for path in sorted(ROOT.joinpath("docs").rglob("*.md")):
        if path == rendered_path:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            named = sorted(schema for schema in schemas if schema in line)
            if len(named) >= 3:
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}:{number} enumerates "
                    f"{', '.join(named)}"
                )

    assert not offenders, (
        "these lines keep a parallel copy of the module schema set; the derived "
        "list lives in docs/generated/MODULE_SCHEMA_CONTRACT.md and documents "
        "must point at it:\n  " + "\n  ".join(offenders)
    )

    # Sensitivity: the detector must be able to fire, or it proves nothing.
    fabricated = " ".join(sorted(schemas)[:3])
    assert len([s for s in schemas if s in fabricated]) >= 3


def _module_lineage_migrations() -> list[Path]:
    """Every migration file in every composed module lineage."""
    files: list[Path] = []
    for import_name in _declared_lineages():
        spec = importlib.util.find_spec(f"{import_name}.migrations")
        if spec is None or not spec.submodule_search_locations:
            continue
        versions = Path(list(spec.submodule_search_locations)[0]) / "versions"
        if versions.is_dir():
            files.extend(sorted(versions.glob("*.py")))
    return files


def test_sub_migrations_never_create_a_schema() -> None:
    """Schema creation is a deployment prerequisite, not a migration effect.

    Sub's own lineage runs as the restricted migration role, which deliberately
    has no database-level CREATE. A `CREATE SCHEMA` here could only work by
    someone having granted that privilege, which is the thing ADR-0011 forbids.
    """
    for path in (ROOT / "alembic" / "versions").glob("*.py"):
        statements = _executed_sql(path).upper()
        assert "CREATE SCHEMA" not in statements, (
            f"{path.relative_to(ROOT).as_posix()} emits CREATE SCHEMA from "
            "Alembic; module schemas belong to "
            "scripts/bootstrap_commercial_module_prereqs.py."
        )


def test_every_module_lineage_schema_create_is_intercepted() -> None:
    """The composed lineages DO ship `CREATE SCHEMA`; that is upstream's right.

    Sub cannot edit an exact-pinned third-party lineage, and would not want to:
    the same distribution has to install under a product that does grant its
    migration role CREATE. What Sub owns is the interception. Every such
    statement must be one `declared_idempotent_schema_create_target` recognises,
    because anything it does not recognise reaches the database verbatim and
    fails the deploy as a permission error.

    This is the guard the repo lacked: `CREATE ROLE` was forbidden in Sub's own
    migrations and nothing at all looked at the package lineages, which are
    where the real second creator lives.
    """
    inspected = 0
    for path in _module_lineage_migrations():
        for statement in _executed_sql(path).splitlines():
            if "CREATE SCHEMA" not in statement.upper():
                continue
            inspected += 1
            target = declared_idempotent_schema_create_target(statement.strip())
            assert target is not None, (
                f"{path.name} emits an unrecognised schema create "
                f"({statement.strip()!r}); app/migration_schema_ops.py would "
                "let it through to the restricted migration role."
            )
            assert target in {item.schema for item in module_schema_contract()}

    # Sensitivity: a guard over an empty set passes for the wrong reason.
    assert inspected > 0, (
        "no module lineage CREATE SCHEMA statements were found; either the "
        "lineages are not installed or the scan stopped working"
    )


def test_cluster_role_creation_is_owned_by_the_bootstrap_script() -> None:
    bootstrap_source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "CREATE ROLE" in bootstrap_source
    assert 'sql.SQL("CREATE ROLE {} {}")' in bootstrap_source
    assert 'sql.SQL("ALTER ROLE {} {}")' in bootstrap_source
    assert "BOOTSTRAP_DATABASE_URL" in bootstrap_source
    assert "MIGRATION_DATABASE_URL" in bootstrap_source

    for path in (ROOT / "alembic" / "versions").glob("*.py"):
        sql = _executed_sql(path).upper()
        assert "CREATE ROLE" not in sql, (
            f"{path.relative_to(ROOT).as_posix()} emits CREATE ROLE from "
            "Alembic; cluster identities belong to the explicit bootstrap."
        )
        assert "ALTER ROLE" not in sql, (
            f"{path.relative_to(ROOT).as_posix()} emits ALTER ROLE from "
            "Alembic; cluster identities belong to the explicit bootstrap."
        )


def test_546_verifies_module_roles_instead_of_creating_them() -> None:
    source = MIGRATION_546.read_text(encoding="utf-8")
    assert "module_database_role_violations" in source
    assert "_assert_module_database_roles_exist()" in source
    assert "CREATE ROLE" not in _executed_sql(MIGRATION_546)
    assert "scripts/bootstrap_commercial_module_prereqs.py" in source


def test_deploy_preflights_prerequisites_before_backup_and_alembic() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "run_database_prerequisite_bootstrap" in deploy
    assert "verify_database_prerequisites" in deploy
    assert "scripts/bootstrap_commercial_module_prereqs.py --repair" in deploy
    assert "scripts/bootstrap_outbox_dispatcher_roles.py --repair" in deploy
    assert "scripts/bootstrap_commercial_module_prereqs.py --verify-only" in deploy
    assert "scripts/bootstrap_outbox_dispatcher_roles.py --verify-only" in deploy

    verify_call = re.search(r"^verify_database_prerequisites$", deploy, re.MULTILINE)
    assert verify_call is not None
    assert verify_call.start() < deploy.index("Backing up database before migrations")
    assert verify_call.start() < deploy.index(
        'log "Applying migrations (alembic upgrade heads)"'
    )


def _executable_lines(text: str) -> str:
    """Drop whole-line comments.

    These guards forbid a construct while the file deliberately DOCUMENTS that
    construct — the workflow explains the `/proc` path that failed, and the
    deploy leg quotes the `.env` fallback it replaced. Matching raw text would
    make the explanation itself the violation, and the cure for that would be
    deleting the explanation, which is worse code. A leading `#` is the only
    comment form here; `#` inside an expression (`${VAR#*://}`) is untouched.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_the_prerequisite_leg_reports_a_typed_outcome() -> None:
    """`already_satisfied`, `repaired` and `blocked` must all be reachable words.

    The defect: the old leg returned 0 both when there was nothing to do and
    when nothing could be done. Three named outcomes are the fix, so the three
    names have to actually exist in the deploy owner and the bootstrap.
    """
    deploy = DEPLOY.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")

    for outcome in ("already_satisfied", "repaired", "blocked"):
        assert outcome in deploy, f"deploy.sh never reports {outcome}"
        assert outcome in bootstrap, f"the bootstrap never reports {outcome}"

    assert "PREREQUISITE_OUTCOME" in deploy
    assert "DEPLOY RECEIPT:" in deploy


def test_the_prerequisite_leg_refuses_instead_of_returning_success() -> None:
    """The exact regression guard.

    `return 0` on a missing credential is what let two candidates and a
    production host reach a verification step that could not pass. A blocked
    repair must terminate the deploy.
    """
    deploy = DEPLOY.read_text(encoding="utf-8")
    leg = deploy[
        deploy.index("run_database_prerequisite_bootstrap() {") : deploy.index(
            "verify_database_prerequisites() {"
        )
    ]
    assert "DEPLOY REFUSED" in leg
    assert "exit 1" in leg
    assert "No BOOTSTRAP_DATABASE_URL supplied" not in leg, (
        "the silent short-circuit is back"
    )

    blocked_at = leg.index('PREREQUISITE_OUTCOME="blocked"')
    assert leg.index("exit 1", blocked_at) > blocked_at


def test_the_deployment_never_reaches_for_the_application_password() -> None:
    """The 2026-08-31 production failure, as a guard.

    The repair connected as `postgres` using `.env`'s `POSTGRES_PASSWORD`,
    which is the APPLICATION password; the container's superuser password comes
    from `PG_LOCAL_BOOTSTRAP_PASSWORD`. Staging passed only because its two
    values happened to be equal. Nothing on a bootstrap path may read either:
    the credential is held in a pgpass file and read by libpq.
    """
    surfaces = {
        "scripts/deploy.sh": DEPLOY,
        ".github/workflows/temporary-module-prereq-repair.yml": (
            ROOT / ".github" / "workflows" / "temporary-module-prereq-repair.yml"
        ),
    }
    for label, path in surfaces.items():
        text = path.read_text(encoding="utf-8")
        assert "POSTGRES_PASSWORD" not in text, (
            f"{label} reads POSTGRES_PASSWORD; the schema bootstrap credential "
            "is a pgpass file, and POSTGRES_PASSWORD is the application's."
        )
        assert "PG_LOCAL_BOOTSTRAP_PASSWORD" not in text, (
            f"{label} reads a superuser password directly; the deployment holds "
            "a dedicated least-privilege credential instead."
        )


def test_the_repair_workflow_no_longer_uses_the_unproved_socket_path() -> None:
    """PR #2843's assumption, measured false on the production host.

    `/var/run` is a symlink to `/run` inside the alpine image, and the host
    kernel resolves that absolute target against the HOST root, so
    `/proc/<pid>/root/var/run/postgresql` cannot exist. The step was a bare
    `test -S` with no diagnostics, so it failed silently.
    """
    workflow = (
        ROOT / ".github" / "workflows" / "temporary-module-prereq-repair.yml"
    ).read_text(encoding="utf-8")
    executable = _executable_lines(workflow)
    # `/proc/` broadly, not just the one path that failed: a different
    # `/proc/<pid>/root` route into the container would be the same mistake.
    assert "/proc/" not in executable
    assert "s.PGSQL" not in executable
    assert "dotmac_pg_local" not in executable
    # And it must not be usable against production at all.
    assert "REFUSED" in executable

    # Sensitivity: the file must still EXPLAIN the measured root cause, and the
    # comment stripper must be what allows that to coexist with the assertions.
    assert "/proc/" in workflow, "the measured root cause is no longer recorded"


def test_the_bootstrap_separates_the_two_credentials() -> None:
    """One mode creates roles, the other cannot, and they are different jobs."""
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert "--repair-schemas" in bootstrap
    assert "allow_role_creation" in bootstrap
    assert "NOCREATEROLE" in bootstrap
    assert "EXIT_BLOCKED = 3" in bootstrap


def test_an_elevated_dsn_may_not_be_persisted_in_the_deploy_env() -> None:
    """Standing privilege must not be one edited line away.

    `env_value` greps the deploy directory's `.env`, so
    `${BOOTSTRAP_DATABASE_URL:-$(env_value BOOTSTRAP_DATABASE_URL)}` made every
    deploy auto-repairing the moment anyone wrote that key into the file. The
    file being empty was the entire safety property and nothing enforced it.

    Note this is not satisfied by the leg merely *mentioning* `env_value`: it
    must read the key in order to REFUSE it. Governance ADR 0028's warning
    applies — presence of a step is not evidence the step does anything.
    """
    deploy = DEPLOY.read_text(encoding="utf-8")
    leg = deploy[
        deploy.index("run_database_prerequisite_bootstrap() {") : deploy.index(
            "verify_database_prerequisites() {"
        )
    ]
    executable = _executable_lines(leg)
    fallback = "${BOOTSTRAP_DATABASE_URL:-$(env_value BOOTSTRAP_DATABASE_URL)}"

    assert fallback not in executable, (
        "a .env-persisted elevated DSN would arm auto-repair on every deploy"
    )
    assert 'persisted_url="$(env_value BOOTSTRAP_DATABASE_URL)"' in executable
    refusal_at = executable.index('persisted_url="$(env_value BOOTSTRAP_DATABASE_URL)"')
    assert "DEPLOY REFUSED" in executable[refusal_at:]
    assert "exit 1" in executable[refusal_at:]
    # The operator path survives, from the process environment only.
    assert 'bootstrap_url="${BOOTSTRAP_DATABASE_URL:-}"' in executable

    # Sensitivity: the leg must still describe the trap it closed, and the
    # stripper must be why that description is not itself a violation.
    assert fallback in leg, "the trap this guard closes is no longer described"


#: PostgreSQL allows `$` as an identifier CONTINUATION character (never as
#: the first character, but that distinction doesn't matter for a boundary
#: check that only asks "is this still the same identifier"), so
#: `dotmac_ro$archive` is a single, legitimate, DISTINCT PostgreSQL
#: identifier — not `dotmac_ro` followed by something else — and `$`
#: immediately adjacent to the needle must suppress the match, not merely
#: `[A-Za-z0-9_]`.
#:
#: This boundary is DELIBERATELY ASCII-ONLY — it does NOT also exclude
#: non-ASCII bytes, even though PostgreSQL's grammar also accepts non-ASCII
#: LETTERS as identifier continuation characters (so `dotmac_roé` is,
#: strictly, also one legitimate distinct identifier). An earlier version
#: of this boundary treated EVERY non-ASCII byte as a continuation
#: character to close that gap, and that was a worse defect than the one it
#: fixed: prose next to the needle is not always plain ASCII — a curly
#: quote, an em-dash, or a non-breaking space in a Markdown document (the
#: guard's own retirement documents are exactly this kind of prose) are
#: each encoded with a byte in `\x80-\xff`, and treating that byte as a
#: continuation character made a REAL occurrence of `dotmac_ro` sitting
#: next to one of them silently UNDETECTED. Distinguishing a non-ASCII
#: LETTER from non-ASCII punctuation at the byte level, without decoding,
#: is not practical. Between over-reporting (`dotmac_roé` wrongly flagged —
#: `$` is in the boundary class and correctly suppresses `dotmac_ro$archive`,
#: but a non-ASCII LETTER is not, so this is the actual accepted
#: over-report: a visible annoyance a human resolves in a minute) and
#: under-reporting (a real reference silently never flagged — the exact
#: failure this whole guard exists to prevent), THIS GUARD MUST
#: OVER-REPORT. Do not re-narrow this boundary to non-ASCII bytes without
#: an actual decode-based letter/punctuation test; a byte-range shortcut
#: reintroduces the under-reporting defect this comment describes.
_ASCII_IDENTIFIER_BOUNDARY_BYTES = rb"[A-Za-z0-9_$]"


def _byte_needle_patterns(needle: str) -> tuple[re.Pattern[bytes], ...]:
    """Case-insensitive BYTE patterns for a role-identifier needle — ONE of
    the three is word-boundary-aware, the other two deliberately are not.

    This is a LITERAL-IDENTIFIER RATCHET, not a proof of absence: it catches
    the literal string `dotmac_ro` appearing in a tracked file's RAW BYTES,
    in any of three covered patterns across two encodings, as a standalone
    identifier ONLY in the ASCII/UTF-8 case — the two UTF-16 patterns match
    the encoded needle unconditionally, with no standalone-identifier check
    at all (see below for why). It does not, and cannot, prove the absence
    of a constructed or obfuscated reference — string concatenation, base64
    or other encoding, an environment-variable name that only resolves to
    `dotmac_ro` at runtime, unicode homoglyphs, and similar evasions are
    all outside what this pattern can see.

    Matching happens against each file's RAW BYTES, never after decoding to
    `str`, and needs no assumption about a file's text encoding: it applies
    identically to a `.py` source file and to this repository's 156 tracked
    binary assets (images, fonts) — a SQLite fixture, a compiled catalog, a
    PDF, an object file, or an archived export can all carry the literal
    bytes `dotmac_ro`, and this scan finds them exactly as it would in a
    `.py` file. There is no binary exemption: "a binary file cannot
    meaningfully contain this identifier" is an assumption, not an
    enforceable premise, and a byte-level scan removes the need to make it.

    The enforceable premise is which ENCODINGS are covered, not which files
    are scanned — exactly two, and no others (not UTF-32, not EBCDIC, not a
    custom obfuscation):

    - **ASCII/UTF-8** — `dotmac_ro`, `DOTMAC_RO`, or any mixed-cased
      spelling, byte-for-byte (PostgreSQL folds an unquoted identifier to
      lowercase, but a quoted identifier or a comment can carry any case).
    - **UTF-16, both byte orders** — each ASCII character interleaved with a
      null byte (`d\x00o\x00t\x00...` little-endian, or the reverse for
      big-endian), matching how a UTF-16-encoded SQL script or export would
      carry the same identifier.

    A plain substring match on `dotmac_ro` also fires inside unrelated
    identifiers that merely happen to start with those characters —
    `dotmac_router_ssh` (a real docker-compose volume/service name) and
    `dotmac_roles_r1_...` (a real test-generated role prefix) both contain
    `dotmac_ro` as a substring without naming this role. In the ASCII/UTF-8
    pattern ONLY, requiring that the needle not be preceded or followed by
    another ASCII identifier CONTINUATION character (`[A-Za-z0-9_$]`) on
    either side rules those out, while still matching standalone
    `dotmac_ro`, quoted/backtick-quoted forms, and any differently-cased
    spelling. The UTF-16 patterns have NO such requirement and WILL flag
    either collision shape if it appears UTF-16-encoded — see below for
    why.

    The ASCII/UTF-8 boundary is deliberately ASCII-only — see
    `_ASCII_IDENTIFIER_BOUNDARY_BYTES`'s comment for why treating non-ASCII
    bytes as boundary characters was tried and reverted: it silently
    stopped detecting a real occurrence sitting next to ordinary prose
    punctuation (a curly quote, an em-dash, a non-breaking space), which is
    a FAR worse failure than the narrower over-reporting this guard accepts
    instead. Concretely: `dotmac_ro$archive` is correctly NOT flagged (`$`
    is in the ASCII boundary class), but `dotmac_roé` IS flagged — the
    guard reports the `dotmac_ro` prefix inside it as a match, even though
    `dotmac_roé` is, strictly, one legitimate distinct PostgreSQL
    identifier. That is a visible, quickly-resolved false positive, chosen
    deliberately over a silent miss.

    The UTF-16 patterns (both byte orders) have NO boundary check at all —
    see `_utf16_pattern`'s docstring for why a boundary lookaround is
    actively unsafe there, not just imprecise.

    ACCEPTED RESIDUAL RISK, stated explicitly rather than silently carried:
    the ASCII/UTF-8 pattern operates on raw bytes with no awareness of
    whether the surrounding file is actually UTF-16, so it can — in
    principle — match ACROSS a pair of UTF-16 code-unit boundaries when
    unrelated multi-byte characters happen to align to the exact needle
    bytes (e.g. five specific UTF-16BE CJK-range code points whose sixteen
    raw bytes happen to spell `dotmac_ro `). This is a FALSE POSITIVE risk,
    not a false negative, and it is NOT suppressed by first classifying a
    file as "is this UTF-16" and skipping the ASCII pattern there: no
    reliable, BOM-independent way to make that classification exists
    (a UTF-16 file need carry no BOM at all), and reintroducing per-file
    text-encoding classification is exactly the kind of assumption this
    design deliberately avoids elsewhere (see `_files_containing`'s
    docstring on the binary-file exemption it removed for the same reason).
    The residual risk is accepted as-is: this ratchet already discloses
    that it is not a proof of absence, and an accidental sixteen-byte
    coincidence spelling the exact needle is a vanishingly unlikely way for
    that disclosed limitation to bite in practice.
    """
    ascii_needle = needle.encode("ascii")
    ascii_pattern = re.compile(
        rb"(?<!"
        + _ASCII_IDENTIFIER_BOUNDARY_BYTES
        + rb")"
        + re.escape(ascii_needle)
        + rb"(?!"
        + _ASCII_IDENTIFIER_BOUNDARY_BYTES
        + rb")",
        re.IGNORECASE,
    )

    def _utf16_pattern(byteorder: str) -> re.Pattern[bytes]:
        """The UTF-16 pattern for one byte order — deliberately UNBOUNDED.

        This has NO boundary check, unlike the ASCII/UTF-8 pattern above,
        and that is a deliberate choice, not an oversight: a 2-byte-wide
        lookaround assumes the needle's code units are the ones actually
        aligned in the file, but nothing about a raw byte scan can know a
        UTF-16 payload's alignment relative to file offset zero. A genuine
        occurrence starting at an ODD offset shifts every neighboring
        "code unit" the lookaround would inspect by one byte, so it
        inspects the wrong bytes entirely — not "imprecisely," but
        WRONGLY, and in a way that can SUPPRESS a real match. Concretely,
        `b"!A\\x00" + <needle in UTF-16LE> + b"A"` defeats a boundary
        check in both directions at once: the LE lookaround sees the
        preceding `A\\x00` as if it were an ASCII identifier character
        (it is actually the unrelated preceding byte `!` and the needle's
        own first byte, misaligned), and the trailing `\\x00A` likewise
        looks like a following identifier character to the BE lookaround
        computed over the same bytes. Two earlier, narrower fixes to this
        boundary (non-ASCII-inclusive, then ASCII-only) each closed one
        false-negative shape and left this one: a boundary check that
        cannot know its own alignment is not a boundary check, it is a
        source of silent misses.

        The trade is the same one already accepted for the ASCII/UTF-8
        pattern's non-letter case, taken further: this pattern matches the
        encoded needle UNCONDITIONALLY, with no adjacent-character check
        at all. It WILL flag `dotmac_router_ssh` or `dotmac_roles_r1_...`
        if either appears UTF-16-encoded — a real over-report, wider than
        the ASCII pattern's. That is accepted deliberately: this guard
        must over-report rather than under-report, and a UTF-16-encoded
        collision with either known decoy shape has never once appeared
        in this repository's actual tracked content in any round of this
        guard's development.
        """
        encoded = needle.encode(f"utf-16-{byteorder}")
        return re.compile(re.escape(encoded), re.IGNORECASE)

    return (ascii_pattern, _utf16_pattern("le"), _utf16_pattern("be"))


#: The ONE literal phrase every refusal in this scan emits — from a
#: prohibited git index mode, an undecodable filename, a symlink or
#: non-regular file encountered on disk, or any other premise
#: violation — regardless of which specific mechanism (errno, index mode,
#: decode failure) produced it. A test asserting THIS marker is asserting
#: the security PROPERTY — "refused, not silently skipped, not left to
#: hang, not left to propagate as a raw exception" — instead of one
#: platform's or one code path's mechanism. CI on the hosted (Linux)
#: runner found that a test asserting `match="without following a"`
#: (wording emitted ONLY by one specific branch) failed on a CORRECT
#: guard, because the identical logical refusal took a different branch
#: on Linux than on macOS. This marker exists so that mistake cannot
#: recur, for any refusal this scan performs.
_SCAN_REFUSAL_MARKER = "scan refusal, not a silent skip"


#: Git index modes this scan permits to be scanned at all — an EXACT
#: ALLOWLIST, checked on the index's word alone before anything is read
#: from disk. `100644` is an ordinary file; `100755` is an ordinary
#: executable file. Everything else refuses, including a mode this scan
#: has never named. An EARLIER version of this was a DENYLIST naming only
#: `120000` (symlink) and `160000` (gitlink/submodule): a mode neither of
#: those two — unrecognized, unexpected, or simply never anticipated —
#: fell THROUGH the denylist and was scanned as ordinary content, because
#: "not in the denylist" and "safe to scan" are not the same claim. An
#: allowlist has no such gap by construction: a mode is scanned only
#: because it was explicitly proven safe, never because it merely failed
#: to match a specific list of known-bad values. Compared as bytes,
#: matching the raw `git ls-files --stage -z` output — the mode is never
#: decoded to text just to run this comparison.
_ALLOWED_INDEX_MODES: frozenset[bytes] = frozenset({b"100644", b"100755"})

#: Human-readable descriptions for the two non-allowed modes this scan has
#: actually observed in practice — a real symlink or a real gitlink/
#: submodule — used only to make a refusal message name the specific
#: thing when it can. Any OTHER non-allowed mode, with no entry here,
#: still refuses via `_ALLOWED_INDEX_MODES` above; this dict only makes
#: two known cases more readable, it is not itself the enforcement.
_KNOWN_PROHIBITED_INDEX_MODE_DESCRIPTIONS: dict[bytes, str] = {
    b"120000": "a symlink",
    b"160000": "a gitlink/submodule",
}


def _tracked_files_with_modes(root: Path) -> tuple[tuple[Path, str], ...]:
    """Every git-tracked path under `root`, paired with its INDEX mode.

    Tracked rather than on-disk on purpose: an untracked scratch file (a
    local `.env`, a build artifact) must never be able to trip this guard,
    and it must never be able to hide a real offender from it either.

    Uses `git ls-files --stage -z`, not plain `git ls-files -z`, because
    the mode is exactly the information a disk-state-only enumeration
    cannot supply. This scan's `O_NOFOLLOW` walk (see
    `_read_verified_tracked_bytes`) refuses a symlink or non-regular file
    it ENCOUNTERS on disk — but that walk can only see whatever currently
    sits on disk. If a tracked symlink (mode `120000`) or
    gitlink/submodule (mode `160000`) has been locally replaced by an
    ordinary regular file, the on-disk walk sees a perfectly normal file
    and scans it, never learning that the INDEX declares this path to be
    something this guard must refuse. The filesystem and the index are two
    DIFFERENT sources of truth about the same path, and a guard that reads
    only one of them has a gap shaped exactly like the other.

    Filenames are decoded as UTF-8 explicitly, one record at a time, so a
    non-UTF-8 tracked filename raises a controlled, typed refusal here —
    not an uncontrolled `UnicodeDecodeError` from a whole-output decode
    with no stable refusal contract.

    The mode is checked against `_ALLOWED_INDEX_MODES` HERE, in the
    enumerator, before a single path is even decoded — not in
    `_dotmac_ro_scan_targets`, which filters this function's output by the
    three self-referencing exclusions before anything else runs. An
    earlier draft put the mode check there instead, after that exclusion
    filter, which meant a mode violation on one of the three excluded
    paths (this guard file, ADR-0016, the runbook) would never be
    noticed — an excluded path is exactly as capable of being recorded as
    a symlink or an unrecognized mode as any other tracked path, and
    exclusion from the SCAN is not exclusion from the INDEX's mode
    declaration. Checking here, over every record `git ls-files --stage`
    returns with no exclusion applied yet, covers every tracked path
    unconditionally.
    """
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        capture_output=True,
        check=True,
        cwd=root,
    )
    entries: list[tuple[Path, str]] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, _, relative_bytes = record.partition(b"\t")
        mode_bytes = metadata.split(b" ", 1)[0]
        if mode_bytes not in _ALLOWED_INDEX_MODES:
            description = _KNOWN_PROHIBITED_INDEX_MODE_DESCRIPTIONS.get(
                mode_bytes, "not one of the allowed modes (100644, 100755)"
            )
            raise AssertionError(
                f"a git-tracked path under {root} ({relative_bytes!r}) is "
                f"recorded in the git INDEX at mode "
                f"{mode_bytes.decode('ascii', errors='replace')} "
                f"({description}), regardless of what currently sits on "
                "disk at that path; the index is the authoritative "
                "declaration of what a tracked path IS, and only 100644 "
                "(ordinary file) and 100755 (ordinary executable file) "
                "are allowed — every other mode is refused on the "
                f"index's word alone, before anything is read from disk — "
                f"{_SCAN_REFUSAL_MARKER}"
            )
        mode = mode_bytes.decode("ascii")
        try:
            relative = relative_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AssertionError(
                f"a git-tracked path under {root} is not valid UTF-8 "
                f"({relative_bytes!r}); this scan's completeness requires "
                "every tracked path's name to be decodable, and an "
                f"undecodable name is refused rather than crashing — "
                f"{_SCAN_REFUSAL_MARKER}"
            ) from exc
        entries.append((root / relative, mode))
    return tuple(sorted(entries, key=lambda entry: entry[0]))


#: The only three tracked files permitted to name `dotmac_ro` literally,
#: because each must do so in order to describe and permanently forbid it:
#: this guard test itself, ADR-0016, and its companion runbook. Excluded by
#: exact relative-path match, not by directory or suffix — every OTHER test
#: file and every OTHER Markdown document (including other docs and other
#: runbooks) is a real control surface and is scanned like any other
#: tracked file.
_DOTMAC_RO_SELF_REFERENCING_FILES: tuple[Path, ...] = (
    Path("tests/architecture/test_commercial_module_prerequisites.py"),
    Path("docs/adr/0016-retire-dotmac-ro-legacy-reporting-role.md"),
    Path("docs/runbooks/DOTMAC_RO_RETIREMENT.md"),
)


def _dotmac_ro_scan_targets(root: Path) -> tuple[Path, ...]:
    """The tracked files this freeze ratchet actually scans.

    This is a LITERAL-IDENTIFIER RATCHET (see `_byte_needle_patterns`), not
    proof that `dotmac_ro` cannot reappear by some other means — it catches
    the literal identifier reappearing in a tracked file's bytes, nothing
    stronger.

    Every git-tracked file in the repository is in scope EXCEPT the three
    files in `_DOTMAC_RO_SELF_REFERENCING_FILES`, each of which must itself
    name `dotmac_ro` in order to describe and forbid it: this guard test
    file, ADR-0016, and its companion runbook. Exclusion is by exact
    relative-path match against that explicit tuple, not by directory or
    suffix — unlike an earlier version of this guard, which exempted ALL of
    `tests/` and ALL `.md` files repo-wide. That was too broad: an
    operational runbook is a real control surface (one could legitimately
    reference a role-grant procedure and should be guarded, not
    blanket-exempted for being Markdown), and so is every other test file.
    Both are now scanned like any other tracked file.

    A directory allowlist was an even earlier design and it structurally
    cannot keep up: it silently missed `.github/actions/setup-ci-python/
    action.yml`, `config/freeradius/schema.sql`, and
    `config/freeradius/sql/admin_schema.sql` — all real, tracked,
    unscanned surfaces. Selecting every tracked file minus the three
    documented self-referencing exclusions has no such PATH-SELECTION blind
    spot by construction.

    THE LESSON THIS FUNCTION EXISTS TO RECORD: a tracked path has TWO
    sources of truth, not one — the git INDEX, which declares what the
    path IS (mode `100644` ordinary file, `100755` ordinary executable
    file, `120000` symlink, `160000` gitlink/submodule, ...), and the
    WORKING TREE, which is whatever currently happens to sit on disk at
    that path. `_read_verified_tracked_bytes` asks the filesystem: it
    refuses a symlinked ancestor, a dangling target, a FIFO, a non-regular
    leaf — every one of those is a DISK-STATE check, made by actually
    opening path components with `O_NOFOLLOW`. But a disk-state check can
    only see what is currently on disk. If a tracked symlink or gitlink
    has been locally replaced by an ORDINARY regular file, the disk-state
    walk sees a perfectly normal file and scans it — it never learns that
    the INDEX declares this path to be something this guard must refuse,
    because the index is a different source of truth that a disk-only
    walk never reads. `_tracked_files_with_modes` reads the index
    (`git ls-files --stage -z`) and refuses any mode outside
    `_ALLOWED_INDEX_MODES` there, in the enumerator, before a single path
    is filtered by exclusion or read from disk — closing the half of this
    guard's completeness that a disk-only walk structurally cannot reach.
    The next person strengthening this guard should check both sources,
    not just the one already checked.

    This function itself no longer inspects the mode at all — it trusts
    that every `(path, mode)` pair `_tracked_files_with_modes` returns has
    already survived the allowlist, and only applies the three
    self-referencing exclusions on top. That ordering matters: checking
    the mode in the ENUMERATOR, before this function's exclusion filter
    ever runs, means the three excluded paths (this guard file, ADR-0016,
    the runbook) are validated exactly like every other tracked path — an
    earlier draft checked the mode here instead, after exclusion, which
    would have let one of those three paths carry a prohibited mode
    unnoticed.

    This function still makes no claim about whether a selected path's
    CONTENT can be safely read from disk — that remains
    `_files_containing`'s enforceable-premise contract (no symlinks or
    non-regular files ENCOUNTERED on disk, no missing paths; a violation
    refuses rather than silently skips). It also makes no claim about text
    encoding for file CONTENT, because `_files_containing` scans raw bytes
    and needs no such assumption — but it does refuse a non-UTF-8 tracked
    FILENAME itself, via `_tracked_files_with_modes`.
    """
    excluded = {root / relative for relative in _DOTMAC_RO_SELF_REFERENCING_FILES}
    return tuple(
        path for path, _mode in _tracked_files_with_modes(root) if path not in excluded
    )


def _read_verified_tracked_bytes(path: Path, root: Path) -> bytes:
    """Read `path`'s bytes with no symlink anywhere between `root` and the leaf.

    A stat-then-read check on the LEAF alone (`path.is_symlink()` followed
    later by `path.read_bytes()`) has real gaps:

    - It only inspects the leaf. If a tracked directory such as `scripts/`
      is replaced in the worktree by a symlink to a DIFFERENT directory
      holding same-named regular files, the leaf itself is not a symlink,
      `exists()`/`is_file()` succeed by following the parent, and the old
      code would silently scan the SUBSTITUTE content — the actual tracked
      content is neither scanned nor refused.
    - It has a check/read race: a leaf replaced between the `is_symlink()`
      check and the later `read_bytes()` call has the same effect.
    - Blocking `open()` on a leaf that is actually a FIFO with no writer
      hangs forever, never even reaching a type check — a guard that hangs
      produces no verdict at all, which is worse than one that refuses.

    This closes all three by opening every path component, INCLUDING
    `root` itself, from `root` down to the leaf with `O_NOFOLLOW`
    (directories additionally with `O_DIRECTORY`, and the leaf additionally
    with `O_NONBLOCK` so opening a FIFO cannot block), chained via `dir_fd`
    so each `open` resolves strictly relative to the previous, already-open
    directory — a symlink ANYWHERE in the chain makes that `open` fail
    outright (`ELOOP`) rather than silently following it. The leaf is then
    checked and read from that SAME file descriptor, so there is no window
    between "validated" and "read" for something else to swap the content
    out from under the scan: what is checked is exactly what is read,
    because it is the same open file.

    Genuine premise violations — this scan genuinely cannot read the
    committed content in each case — REFUSE loudly rather than silently
    skipping or hanging: a symlink anywhere in the chain (including `root`
    itself), a missing tracked path, or a non-regular leaf (a
    gitlink/submodule directory, or a FIFO/device/socket substituted for a
    tracked file, are the concrete cases: `git ls-files` lists a path, but
    there is no ordinary file content behind it to read).

    STATED RESIDUAL PREMISE, not closed and not silently assumed away: this
    walk does not detect a SAME-TYPE ancestor substitution racing it — an
    already-validated ancestor directory renamed away and replaced by
    ANOTHER ordinary directory between that component's `open()` and the
    next component's `open()` is indistinguishable from the original at
    the point of the second `open()`, because both are ordinary,
    non-symlink directories and neither `O_NOFOLLOW` nor a `dir_fd` chain
    can see past that. Closing this at reasonable cost is not possible
    without a broader filesystem-level guarantee (e.g. an immutable
    checkout) this test suite does not control. What IS enforced — a
    symlinked or non-regular component anywhere in the chain — covers the
    concrete tampering shape this guard was written to catch; a
    same-type-substitution race is a different, narrower threat left
    explicitly open rather than implicitly assumed closed.
    """
    relative_parts = path.relative_to(root).parts
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(relative_parts):
            is_leaf = index == len(relative_parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if is_leaf:
                # Without this, opening a leaf that has been replaced by a
                # FIFO with no writer blocks here indefinitely, before the
                # type check below ever runs.
                flags |= os.O_NONBLOCK
            else:
                flags |= os.O_DIRECTORY
            try:
                next_fd = os.open(part, flags, dir_fd=dir_fd)
            except OSError as exc:
                component = root.joinpath(*relative_parts[: index + 1])
                # `errno` differs by PLATFORM and by which flag combination
                # tripped it, for the identical logical defect: opening a
                # symlinked ancestor with `O_NOFOLLOW | O_DIRECTORY` raises
                # `ELOOP` on macOS but `ENOTDIR` on Linux (confirmed by CI on
                # the hosted runner — this repository's own history, not a
                # hypothetical). A leaf-level `O_NOFOLLOW` symlink, and a
                # missing path, may differ the same way on some platform this
                # scan hasn't run on yet. Every branch below therefore emits
                # the SAME literal marker phrase
                # (`_SCAN_REFUSAL_MARKER`) regardless of which specific
                # errno produced it, so a caller — and a test — can assert
                # the PROPERTY ("this was refused, not silently skipped or
                # left to propagate as a raw OSError") without encoding any
                # one platform's mechanism.
                if exc.errno == errno.ENOENT:
                    raise AssertionError(
                        f"{path} is a tracked path missing on disk at "
                        f"{component} (a dangling symlink target, an "
                        "unfetched gitlink/submodule, or a "
                        "sparse-checkout exclusion); this scan's "
                        "completeness requires every tracked path to "
                        f"actually be present — {_SCAN_REFUSAL_MARKER}"
                    ) from exc
                if exc.errno == errno.ELOOP:
                    raise AssertionError(
                        f"{path} could not be read without following a "
                        f"symlink at {component}; this scan's completeness "
                        "requires no symlink anywhere between the scan "
                        "root and the leaf — not just the leaf itself — "
                        f"{_SCAN_REFUSAL_MARKER}"
                    ) from exc
                raise AssertionError(
                    f"{path} could not be opened at {component} ({exc}); "
                    f"this scan cannot see inside it — {_SCAN_REFUSAL_MARKER}"
                ) from exc
            os.close(dir_fd)
            dir_fd = next_fd

        leaf_stat = os.fstat(dir_fd)
        if not stat.S_ISREG(leaf_stat.st_mode):
            raise AssertionError(
                f"{path} is tracked but is not a regular file (e.g. a "
                "gitlink/submodule, or a FIFO/device/socket substituted "
                f"for it); this scan cannot see inside it — "
                f"{_SCAN_REFUSAL_MARKER}"
            )

        chunks: list[bytes] = []
        while True:
            chunk = os.read(dir_fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(dir_fd)


def _files_containing(paths: Iterable[Path], needle: str, root: Path) -> list[Path]:
    """Every tracked file among `paths` whose RAW BYTES contain `needle`.

    Reading goes through `_read_verified_tracked_bytes`, which refuses
    (rather than silently skips OR HANGS) a symlink anywhere between `root`
    and a leaf (including `root` itself), a missing tracked path, or a
    non-regular leaf (including a FIFO with no writer, which a blocking
    open would hang on forever) — see that function's docstring for why a
    leaf-only, stat-then-read check is not enough, and for the one
    STATED, deliberately unclosed residual premise (no same-type ancestor
    substitution races the walk) — a guard exemption states an enforceable
    premise, or the region is unmonitored rather than exempt. That refusal
    is a DISK-STATE check; it complements, and does not replace, the git
    INDEX-mode check `_tracked_files_with_modes` performs, via
    `_dotmac_ro_scan_targets`, before any path ever reaches this function
    (see that function's docstring for why the index and the working tree
    are two different sources of truth about the same path, and why a
    guard needs both).

    There is deliberately NO binary-file exemption. Matching happens
    directly against each file's RAW BYTES (see `_byte_needle_patterns`),
    not after decoding to `str`, so it needs no assumption about a file's
    text encoding and applies uniformly to every tracked file, including
    this repository's 156 tracked binary assets (images, fonts). "A binary
    file cannot meaningfully contain this identifier" is an assumption, not
    an enforceable premise — a tracked SQLite fixture, a compiled catalog, a
    PDF, an object file, or an archived export can all carry the literal
    bytes `dotmac_ro`, and a byte-level scan finds them exactly as it would
    in a `.py` file, with nothing left unmonitored. The enforceable premise
    that DOES apply is which encodings of the identifier are covered — see
    `_byte_needle_patterns` for the exact two (ASCII/UTF-8 and UTF-16, both
    byte orders) and nothing else. This is the ONE scanning function: both
    the repository-wide freeze guard and its sensitivity proofs call it,
    over different path sets, instead of each keeping its own copy of the
    same `pattern.search()` loop.
    """
    patterns = _byte_needle_patterns(needle)
    offenders: list[Path] = []
    for path in paths:
        raw = _read_verified_tracked_bytes(path, root)
        if any(pattern.search(raw) for pattern in patterns):
            offenders.append(path)
    return offenders


def test_dotmac_ro_never_reenters_migrations_app_or_scripts() -> None:
    """The legacy `dotmac_ro` reporting role is permanently forbidden here.

    ADR-0016 retires `dotmac_ro` (observed on the known production cluster,
    ~291 public-schema SELECT grants, never captured in this repo — whether
    it also exists elsewhere in the fleet or on DR/standby infrastructure is
    unverified and is Phase 1 work) without folding it into the closed
    application-role contract. This guard is the freeze (Phase 0) made
    structural — but it is a LITERAL-IDENTIFIER RATCHET, not a proof of
    absence: it catches the literal string `dotmac_ro` reappearing in a
    tracked file's RAW BYTES, in either of two covered encodings (see
    `_byte_needle_patterns`), and nothing stronger. It is a STANDALONE
    identifier check ONLY in the ASCII/UTF-8 encoding — the UTF-16 patterns
    (both byte orders) match the encoded needle unconditionally, with no
    standalone check at all, because a boundary check cannot know a UTF-16
    payload's byte alignment and a wrong one is a source of silent misses
    (see `_utf16_pattern`'s docstring). It cannot prove the absence of a
    constructed or obfuscated reference (string concatenation, base64 or
    other encoding, an environment-variable name that only resolves to
    `dotmac_ro` at runtime, unicode homoglyphs).

    The scan is every git-tracked file that also satisfies the enforceable
    completeness premises in `_files_containing` (no tracked symlinks, no
    gitlinks/submodules, no missing tracked paths — a violated premise
    REFUSES the scan rather than silently skipping the offending path).
    There is deliberately no binary-file exemption: matching is byte-level,
    so it applies uniformly to text and binary tracked files alike, EXCEPT
    the three files in `_DOTMAC_RO_SELF_REFERENCING_FILES`
    (this guard test file, ADR-0016, and its companion runbook), each of
    which must itself name `dotmac_ro` to describe and forbid it, and which
    this test asserts are EXACTLY those three via a literal written
    independently of that tuple. Every OTHER test file and every OTHER
    Markdown document — including other docs and other runbooks — is a real
    control surface and IS scanned: an earlier version of this guard
    exempted all of `tests/` and all `.md` files repo-wide, which was too
    broad (an operational runbook can legitimately reference a role-grant
    procedure and needs guarding, not a blanket Markdown exemption).

    It is also deliberately not a directory allowlist: an even earlier round
    of this guard listed `alembic/`, `app/`, `scripts/`, `.github/workflows/`,
    `deploy/`, `docker/`, `nginx/`, plus a non-recursive top-level scan, and
    that list still missed real tracked surfaces
    (`.github/actions/setup-ci-python/action.yml`,
    `config/freeradius/schema.sql`,
    `config/freeradius/sql/admin_schema.sql`) simply because no one had
    added them to it yet. A tracked-files scan has no such gap: a new
    top-level directory or CI asset is in scope the moment it is committed,
    with no allowlist edit required.
    """
    targets = _dotmac_ro_scan_targets(ROOT)
    scanned_files = set(targets)
    offenders = _files_containing(targets, "dotmac_ro", ROOT)

    assert not offenders, (
        "dotmac_ro must never appear in migrations, migration machinery, "
        "application code, operational scripts, CI workflows, deploy "
        "assets, or other tracked operational surfaces (ADR-0016 Phase 0 "
        "freeze); found it in:\n  "
        + "\n  ".join(path.relative_to(ROOT).as_posix() for path in offenders)
    )

    assert BOOTSTRAP in scanned_files, (
        "the scan did not include scripts/bootstrap_commercial_module_prereqs.py; "
        "the legacy role must not be added to the bootstrap script, and this "
        "guard cannot prove that if the file was silently missed"
    )

    assert ROOT / "Makefile" in scanned_files, (
        "the scan did not include the repo-root Makefile, which carries the "
        "real database-role-bootstrap targets (bootstrap-test-database-roles)"
    )
    assert ROOT / "docker-compose.yml" in scanned_files, (
        "the scan did not include docker-compose.yml, a tracked operational "
        "surface the legacy role could reappear in"
    )
    assert ROOT / "config" / "freeradius" / "schema.sql" in scanned_files, (
        "the scan did not include config/freeradius/schema.sql; a directory "
        "allowlist missed this real tracked surface until this round found "
        "it, which is exactly the structural gap the tracked-files scan "
        "closes"
    )

    # `_tracked_files_with_modes` must enumerate via unrestricted
    # `git ls-files --stage`, not a hand-maintained list of directories.
    # These three are diagnostic samples, not the proof: naming SPECIFIC
    # expected files gives a
    # readable failure message, but sampling more directories only moves
    # the goalpost — a pathspec listing exactly `docs`, `tests`, `scripts`,
    # `app`, `alembic`, `.github`, `config/freeradius`, and the asserted
    # root files would satisfy every one of these while still silently
    # dropping `docker/`, `nginx/`, `.dotmac/`, or anything added later.
    # The set-equality assertion below this one is what actually closes
    # the enumeration question.
    assert ROOT / "app" / "__init__.py" in scanned_files, (
        "the scan did not include app/__init__.py; the enumeration must be "
        "unrestricted `git ls-files`, not a directory list that happens to "
        "have satisfied every OTHER assertion in this test while quietly "
        "dropping app/"
    )
    assert ROOT / "alembic" / "env.py" in scanned_files, (
        "the scan did not include alembic/env.py; the enumeration must be "
        "unrestricted `git ls-files`, not a directory list that happens to "
        "have satisfied every OTHER assertion in this test while quietly "
        "dropping alembic/"
    )
    assert ROOT / ".github" / "workflows" / "ci.yml" in scanned_files, (
        "the scan did not include .github/workflows/ci.yml; the enumeration "
        "must be unrestricted `git ls-files`, not a directory list that "
        "happens to have satisfied every OTHER assertion in this test "
        "while quietly dropping .github/"
    )

    # THIS is what actually closes the enumeration question: an
    # INDEPENDENTLY computed `git ls-files` call (its own subprocess
    # invocation, not a reuse of `_tracked_files_with_modes`) enumerates
    # every tracked file in the repository, and the scan's target set must
    # equal EXACTLY
    # that set minus the three accepted exclusions. No sampling, no
    # directory list, nothing assumed — a narrowed OR widened enumeration
    # shows up here even if it happened to satisfy every sampled assertion
    # above.
    independently_enumerated = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    independently_tracked_files = {
        ROOT / entry for entry in independently_enumerated.stdout.split("\0") if entry
    }
    expected_scanned_files = independently_tracked_files - {
        ROOT / relative for relative in _DOTMAC_RO_SELF_REFERENCING_FILES
    }
    assert scanned_files == expected_scanned_files, (
        "_dotmac_ro_scan_targets's output is not EXACTLY every tracked "
        "file minus the three accepted exclusions; missing: "
        f"{sorted(p.relative_to(ROOT).as_posix() for p in expected_scanned_files - scanned_files)}; "
        "unexpected: "
        f"{sorted(p.relative_to(ROOT).as_posix() for p in scanned_files - expected_scanned_files)}"
    )

    for relative in _DOTMAC_RO_SELF_REFERENCING_FILES:
        assert ROOT / relative not in scanned_files, (
            f"{relative.as_posix()} must be excluded from the scan; it is one "
            "of the three files that must itself name dotmac_ro to describe "
            "and forbid it"
        )

    # This literal is written INDEPENDENTLY of `_DOTMAC_RO_SELF_REFERENCING_FILES`
    # — copying it from the tuple would make this assertion pass no matter how
    # the tuple grows. Appending a fourth path to the tuple (e.g. a migration,
    # workflow, or operational script someone wants to quietly exempt) removes
    # it from the scan above with no other test catching that; comparing the
    # tuple's actual contents against this fixed, hand-written set is what
    # makes widening the exclusion list fail the build.
    accepted_dotmac_ro_exclusions = frozenset(
        {
            "tests/architecture/test_commercial_module_prerequisites.py",
            "docs/adr/0016-retire-dotmac-ro-legacy-reporting-role.md",
            "docs/runbooks/DOTMAC_RO_RETIREMENT.md",
        }
    )
    actual_dotmac_ro_exclusions = frozenset(
        relative.as_posix() for relative in _DOTMAC_RO_SELF_REFERENCING_FILES
    )
    assert actual_dotmac_ro_exclusions == accepted_dotmac_ro_exclusions, (
        "_DOTMAC_RO_SELF_REFERENCING_FILES no longer matches the three "
        "accepted self-referencing files exactly; got "
        f"{sorted(actual_dotmac_ro_exclusions)}, expected "
        f"{sorted(accepted_dotmac_ro_exclusions)} — an unexpected member "
        "here silently drops a file out of the freeze scan, which is the "
        "exact defect this ratchet exists to prevent"
    )

    assert ROOT / "CHANGELOG.md" in scanned_files, (
        "the scan did not include CHANGELOG.md; a Markdown file that is NOT "
        "one of the three self-referencing exclusions is a real control "
        "surface and must be scanned like any other tracked file"
    )
    assert (
        ROOT / "tests" / "architecture" / "adapter_keyword_service_call.py"
        in scanned_files
    ), (
        "the scan did not include another test-suite file; a test file that "
        "is NOT this guard test itself is a real control surface and must be "
        "scanned like any other tracked file"
    )

    assert "dotmac_ro" not in COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT, (
        "dotmac_ro must never become a key in COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT, "
        "the bootstrap-role contract that also carries the closed "
        "application-role set (ADR-0016)"
    )


def test_the_dotmac_ro_scan_actually_detects_the_string(tmp_path) -> None:
    """Sensitivity proof: point the scanner at a planted match outside the repo.

    A guard that only ever runs over a clean tree proves nothing about
    itself. This writes `dotmac_ro` into a file under pytest's `tmp_path`
    (never inside the repo's own scanned directories) and asserts the same
    scanning function used above actually finds it there — once in the
    lowercase spelling, and once in a differently-cased spelling
    (`DOTMAC_RO`), because PostgreSQL folds an unquoted identifier to
    lowercase and a case-sensitive scan would miss the second planted file.

    It also plants a decoy, `dotmac_router_ssh_example`, which contains
    `dotmac_ro` as a plain substring but is not a reference to this role;
    the word-boundary-aware matcher must NOT flag it, proving the fix for
    the true-positive cases above did not trade away correctness on the
    known collision shapes (`dotmac_router_ssh`, `dotmac_roles_r1_...`).

    Further planted cases close specific mutation gaps a smaller test suite
    left open:

    - A reference embedded inside otherwise-arbitrary binary bytes proves
      there is no binary-file exemption (an earlier design would have
      silently exempted this via a NUL-byte binary heuristic).
    - UTF-16LE AND UTF-16BE references, each in BOTH lowercase and
      differently-cased spellings, prove the covered-encodings premise and
      case folding are real in both byte orders — not just documented, and
      not just proven for one order or one case.
    - A UTF-16BE reference followed by NOTHING (end of file immediately
      after the last code unit) proves BE coverage is real, not incidental:
      every OTHER planted BE fixture here happens to be followed by
      another code unit, and that unit's zero byte is exactly what an LE
      pattern shifted one byte to the right needs to find the SAME bytes
      by accident — so deleting BE coverage entirely, or replacing it with
      a second LE pattern, would still pass every other BE assertion.
      Only a fixture with no trailing code unit can tell real BE coverage
      apart from this accidental shifted-LE match.
    - A decoy in the ASCII pattern with the needle at the START of a
      longer identifier proves the FOLLOWING boundary; a second decoy with
      the needle at the END of a longer identifier proves the PRECEDING
      boundary — a smaller test that only ever planted "prefix" decoys
      would still pass if both negative lookbehinds were deleted entirely.
      The same two collision shapes, UTF-16-encoded (both byte orders),
      are planted too — but the UTF-16 patterns have NO boundary check at
      all (see `_utf16_pattern`'s docstring for why), so these are proven
      to be DETECTED, as the accepted over-report, not excluded.
    - A UTF-16LE reference preceded by a single odd-length ASCII byte
      (so the reference itself starts at an odd file offset, not aligned
      to an even code-unit boundary from byte zero) proves detection does
      not depend on any assumption about where in the file a UTF-16
      payload begins.
    - The exact double-sided alignment collision that defeated an earlier
      boundary-lookaround design for UTF-16 — a genuine occurrence at an
      odd offset with unrelated bytes on BOTH sides that a 2-byte-wide
      lookaround misreads as identifier-continuation characters in each
      byte order simultaneously — must be DETECTED. This is the concrete
      case that proved a UTF-16 boundary check cannot know its own
      alignment and is a source of silent misses, not a refinement.
    - References adjacent to non-ASCII prose punctuation — surrounded by
      UTF-8 curly quotes, preceded by a UTF-8 em-dash, preceded by a UTF-8
      non-breaking space — must all still be DETECTED. An earlier version
      of the ASCII/UTF-8 boundary treated every non-ASCII byte as an
      identifier continuation character to close a narrower false-positive
      gap (`dotmac_roé` being flagged even though it is, strictly, one
      legitimate distinct identifier), and that silently stopped detecting
      exactly this shape of real occurrence — prose right next to the
      needle in a Markdown document, which is what this guard's OWN
      retirement documents look like. This guard must over-report, never
      under-report; these cases prove the reverted mistake stays reverted.
    - A UTF-16LE reference with a byte-order mark (`\xff\xfe`) IMMEDIATELY
      before it, with no gap, must be DETECTED.
    """
    planted = tmp_path / "planted_lowercase_reference.sql"
    planted.write_text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO dotmac_ro;")

    # A filename differing only in the *case* of the needle would collide on
    # a case-insensitive filesystem (e.g. default macOS APFS), silently
    # overwriting the first planted file instead of adding a second one — so
    # this second file's name, not just its content, must be distinct.
    planted_uppercase = tmp_path / "planted_uppercase_reference.sql"
    planted_uppercase.write_text(
        "GRANT SELECT ON ALL TABLES IN SCHEMA public TO DOTMAC_RO;"
    )

    decoy = tmp_path / "planted_decoy_non_reference.yml"
    decoy.write_text("volumes:\n  dotmac_router_ssh_example: null\n")

    decoy_suffix = tmp_path / "planted_decoy_suffix_non_reference.yml"
    decoy_suffix.write_text("volumes:\n  my_dotmac_ro_prefixed: null\n")

    planted_binary = tmp_path / "planted_binary_with_reference.bin"
    planted_binary.write_bytes(
        b"\x89PNG\r\n\x1a\x00\x00\x01\x02dotmac_ro\x03\x04\x00\xff\xfe"
    )

    planted_utf16le = tmp_path / "planted_utf16le_reference.sql"
    planted_utf16le.write_bytes(
        "GRANT SELECT ON ALL TABLES IN SCHEMA public TO dotmac_ro;".encode("utf-16-le")
    )
    planted_utf16le_uppercase = tmp_path / "planted_utf16le_uppercase_reference.sql"
    planted_utf16le_uppercase.write_bytes(
        "GRANT SELECT ON ALL TABLES IN SCHEMA public TO DOTMAC_RO;".encode("utf-16-le")
    )
    # The UTF-16 patterns are deliberately UNBOUNDED (see `_utf16_pattern`'s
    # docstring) — these are not "decoys" that must be excluded, they are
    # the accepted over-report this design trades for: a UTF-16-encoded
    # collision shape IS flagged in this encoding, unlike its ASCII/UTF-8
    # counterpart.
    overreport_utf16le_prefix_collision = tmp_path / "overreport_prefix_utf16le.sql"
    overreport_utf16le_prefix_collision.write_bytes(
        "dotmac_router_ssh_example".encode("utf-16-le")
    )
    overreport_utf16le_suffix_collision = tmp_path / "overreport_suffix_utf16le.sql"
    overreport_utf16le_suffix_collision.write_bytes(
        "my_dotmac_ro_prefixed".encode("utf-16-le")
    )

    planted_utf16be = tmp_path / "planted_utf16be_reference.sql"
    planted_utf16be.write_bytes(
        "GRANT SELECT ON ALL TABLES IN SCHEMA public TO dotmac_ro;".encode("utf-16-be")
    )
    planted_utf16be_uppercase = tmp_path / "planted_utf16be_uppercase_reference.sql"
    planted_utf16be_uppercase.write_bytes(
        "GRANT SELECT ON ALL TABLES IN SCHEMA public TO DOTMAC_RO;".encode("utf-16-be")
    )
    overreport_utf16be_prefix_collision = tmp_path / "overreport_prefix_utf16be.sql"
    overreport_utf16be_prefix_collision.write_bytes(
        "dotmac_router_ssh_example".encode("utf-16-be")
    )
    overreport_utf16be_suffix_collision = tmp_path / "overreport_suffix_utf16be.sql"
    overreport_utf16be_suffix_collision.write_bytes(
        "my_dotmac_ro_prefixed".encode("utf-16-be")
    )

    # BE coverage was, until this fixture, proven only by accident: every
    # other planted BE file is followed by another UTF-16 code unit (a
    # semicolon, an underscore, a letter), and that trailing unit's zero
    # byte is exactly what an LE pattern shifted one byte to the right
    # needs to complete a spurious match over the SAME bytes. If the BE
    # pattern were deleted, or replaced with a second copy of the LE
    # pattern, every other BE fixture in this test would still be found —
    # by the shifted LE match, not by BE coverage actually existing. This
    # fixture is exactly `"dotmac_ro"` UTF-16BE-encoded with NOTHING after
    # it — end of file immediately following the last code unit. The
    # shifted-LE match needs a trailing `\x00` that does not exist here
    # (there is no byte after the file's last byte to supply it), so ONLY
    # a real BE pattern can detect this one. A byte-for-byte check: this
    # file's bytes are `\x00d\x00o\x00t\x00m\x00a\x00c\x00_\x00r\x00o`;
    # the LE-encoded needle is `d\x00o\x00t\x00m\x00a\x00c\x00_\x00r\x00o\x00`
    # (note the trailing `\x00`) — shifting the file by one byte gives
    # `d\x00o\x00t\x00m\x00a\x00c\x00_\x00r\x00o` with NO trailing `\x00`
    # (the file simply ends), so the 18-byte LE pattern cannot match
    # within these 17 available shifted bytes.
    planted_utf16be_at_eof_no_trailing_unit = (
        tmp_path / "planted_utf16be_at_eof_no_trailing_unit.sql"
    )
    planted_utf16be_at_eof_no_trailing_unit.write_bytes("dotmac_ro".encode("utf-16-be"))

    # A single odd-length prefix byte before the UTF-16LE payload starts
    # means the reference begins at an ODD offset from byte zero — not
    # aligned to an even code-unit boundary the way every other planted
    # UTF-16 file in this test happens to be.
    planted_utf16le_odd_offset = tmp_path / "planted_utf16le_odd_offset.sql"
    planted_utf16le_odd_offset.write_bytes(b"\x2d" + "dotmac_ro".encode("utf-16-le"))

    # The exact double-sided alignment collision that defeated a
    # boundary-lookaround design: an odd-offset genuine occurrence with
    # bytes on BOTH sides that a 2-byte-wide lookaround would misread as
    # identifier-continuation characters in each byte order at once. With
    # a boundary check, this was UNDETECTED — the LE lookaround saw the
    # preceding `A\x00` (actually `!` plus the needle's own misaligned
    # first byte) as an ASCII identifier character, and the trailing
    # `\x00A` fooled the BE lookaround computed over the same bytes the
    # same way. This is why the UTF-16 patterns are now unbounded.
    planted_utf16le_double_sided_alignment_collision = (
        tmp_path / "planted_utf16le_double_sided_alignment_collision.sql"
    )
    planted_utf16le_double_sided_alignment_collision.write_bytes(
        b"!A\x00" + "dotmac_ro".encode("utf-16-le") + b"A"
    )

    # Non-ASCII prose punctuation immediately adjacent to the needle, in
    # UTF-8 — the exact shape that made an earlier, reverted boundary
    # design silently stop detecting a real occurrence. Each of these MUST
    # be detected.
    planted_curly_quotes = tmp_path / "planted_curly_quotes_reference.md"
    planted_curly_quotes.write_text("the role “dotmac_ro” is retired")

    planted_em_dash_prefix = tmp_path / "planted_em_dash_prefix_reference.md"
    planted_em_dash_prefix.write_text("legacy role—dotmac_ro—retired")

    planted_nbsp_prefix = tmp_path / "planted_nbsp_prefix_reference.md"
    planted_nbsp_prefix.write_text("legacy role dotmac_ro retired")

    # A UTF-16LE byte-order mark immediately followed by the needle, with
    # no gap — the concrete BOM-adjacency failure the reverted boundary
    # design introduced.
    planted_utf16le_bom_adjacent = tmp_path / "planted_utf16le_bom_adjacent.sql"
    planted_utf16le_bom_adjacent.write_bytes(
        b"\xff\xfe" + "dotmac_ro".encode("utf-16-le")
    )

    offenders = _files_containing(sorted(tmp_path.rglob("*")), "dotmac_ro", tmp_path)

    assert planted in offenders, (
        "the scanning function failed to detect a planted dotmac_ro reference; "
        "the guard above would pass vacuously"
    )
    assert planted_uppercase in offenders, (
        "the scanning function failed to detect a differently-cased "
        "DOTMAC_RO reference; PostgreSQL folds unquoted identifiers to "
        "lowercase, so a case-sensitive scan would miss this variant"
    )
    assert decoy not in offenders, (
        "the scanning function flagged dotmac_router_ssh_example as a "
        "dotmac_ro reference; word-boundary matching must not fire on an "
        "unrelated identifier that merely starts with the same characters"
    )
    assert decoy_suffix not in offenders, (
        "the scanning function flagged my_dotmac_ro_prefixed as a "
        "dotmac_ro reference; the PRECEDING boundary (needle at the END of "
        "a longer identifier) must also suppress a match, not just the "
        "following one"
    )
    assert planted_binary in offenders, (
        "the scanning function failed to detect a dotmac_ro reference "
        "embedded in otherwise-arbitrary binary bytes; there is no binary "
        "exemption and byte-level matching must find it regardless"
    )
    assert planted_utf16le in offenders, (
        "the scanning function failed to detect a UTF-16LE-encoded "
        "dotmac_ro reference; the covered-encodings premise requires this, "
        "not just ASCII/UTF-8"
    )
    assert planted_utf16le_uppercase in offenders, (
        "the scanning function failed to detect a differently-cased "
        "UTF-16LE-encoded DOTMAC_RO reference; case folding must hold in "
        "this encoding too, not just ASCII/UTF-8"
    )
    assert overreport_utf16le_prefix_collision in offenders, (
        "the scanning function did NOT flag a UTF-16LE-encoded "
        "dotmac_router_ssh_example; the UTF-16 patterns are deliberately "
        "unbounded (no boundary check, because a boundary check cannot "
        "know a UTF-16 payload's alignment), so this collision shape is "
        "the accepted over-report — if this assertion fails, someone "
        "reintroduced a boundary check on this pattern, reopening the "
        "alignment-based silent-miss defect it was removed to fix"
    )
    assert overreport_utf16le_suffix_collision in offenders, (
        "the scanning function did NOT flag a UTF-16LE-encoded "
        "my_dotmac_ro_prefixed; same reasoning as the prefix-collision "
        "case above — this is the accepted over-report, not a defect"
    )
    assert planted_utf16be in offenders, (
        "the scanning function failed to detect a UTF-16BE-encoded "
        "dotmac_ro reference; both byte orders are a covered encoding, not "
        "just little-endian"
    )
    assert planted_utf16be_uppercase in offenders, (
        "the scanning function failed to detect a differently-cased "
        "UTF-16BE-encoded DOTMAC_RO reference; case folding must hold in "
        "this byte order too"
    )
    assert planted_utf16be_at_eof_no_trailing_unit in offenders, (
        "the scanning function failed to detect a UTF-16BE-encoded "
        "dotmac_ro reference with no trailing code unit at end of file; "
        "every OTHER planted BE fixture in this test is followed by "
        "another code unit whose zero byte would let an LE pattern "
        "shifted one byte to the right find it by accident — this fixture "
        "has no such trailing byte, so only genuine BE coverage can find "
        "it, and if BE coverage were ever deleted this is the one case "
        "that would actually go missing"
    )
    assert overreport_utf16be_prefix_collision in offenders, (
        "the scanning function did NOT flag a UTF-16BE-encoded "
        "dotmac_router_ssh_example; same accepted over-report as the "
        "UTF-16LE case above, in the other byte order"
    )
    assert overreport_utf16be_suffix_collision in offenders, (
        "the scanning function did NOT flag a UTF-16BE-encoded "
        "my_dotmac_ro_prefixed; same accepted over-report as the UTF-16LE "
        "case above, in the other byte order"
    )
    assert planted_utf16le_odd_offset in offenders, (
        "the scanning function failed to detect a UTF-16LE-encoded "
        "dotmac_ro reference starting at an ODD file offset; detection "
        "must not depend on where in the file a UTF-16 payload happens to "
        "begin"
    )
    assert planted_utf16le_double_sided_alignment_collision in offenders, (
        "the scanning function failed to detect a genuine UTF-16LE "
        "dotmac_ro reference at an odd offset with bytes on both sides "
        "that a boundary lookaround would misread as identifier "
        "continuation characters in each byte order at once; this is "
        "exactly why the UTF-16 patterns must have no boundary check"
    )
    assert planted_curly_quotes in offenders, (
        "the scanning function failed to detect a dotmac_ro reference "
        "surrounded by UTF-8 curly quotes; the boundary must be ASCII-only "
        "so this guard over-reports rather than silently under-reporting a "
        "real occurrence next to ordinary prose punctuation"
    )
    assert planted_em_dash_prefix in offenders, (
        "the scanning function failed to detect a dotmac_ro reference "
        "adjacent to a UTF-8 em-dash; a non-ASCII boundary byte must never "
        "suppress a real occurrence"
    )
    assert planted_nbsp_prefix in offenders, (
        "the scanning function failed to detect a dotmac_ro reference "
        "adjacent to a UTF-8 non-breaking space; a non-ASCII boundary byte "
        "must never suppress a real occurrence"
    )
    assert planted_utf16le_bom_adjacent in offenders, (
        "the scanning function failed to detect a UTF-16LE-encoded "
        "dotmac_ro reference immediately preceded by a byte-order mark; "
        "the BOM bytes must never be mistaken for an identifier "
        "continuation character"
    )


def test_the_scan_targets_exclude_only_the_three_accepted_files(tmp_path) -> None:
    """Sensitivity proof for `_dotmac_ro_scan_targets` itself.

    The test above only proves `_files_containing` matches correctly over an
    arbitrary path list — it never exercises `_dotmac_ro_scan_targets` or
    real tracked-file selection at all. That left a real gap: nothing failed
    if `_DOTMAC_RO_SELF_REFERENCING_FILES` grew a fourth entry, because
    `_dotmac_ro_scan_targets` would then legitimately exclude that new entry
    too, and no test exercised the target-builder against a tracked-file
    listing to notice.

    This builds a real, throwaway git repository under `tmp_path`, tracks
    the three genuinely accepted self-referencing files plus several decoy
    files that must NEVER be excludable, commits them, and proves
    `_dotmac_ro_scan_targets` drops exactly the three and nothing else. If
    `_DOTMAC_RO_SELF_REFERENCING_FILES` ever gains a fourth entry equal to
    any decoy's path, this test fails; any other fourth entry is caught
    instead by the independent-literal equality assertion in
    `test_dotmac_ro_never_reenters_migrations_app_or_scripts`.

    The decoys deliberately sit under `app/`, `alembic/`, and `.github/` —
    directories that were part of an EARLIER, rejected directory-allowlist
    design (see the module-level history above) — plus one brand-new
    top-level directory that has never appeared in any historical
    allowlist. A single decoy under `scripts/` alone would not catch a
    regression that narrowed `_tracked_files_with_modes`'s
    `git ls-files --stage` invocation back down to exactly the directories
    this test and the real-tree
    assertions above happen to check (`docs/`, `tests/`, `scripts/`,
    `Makefile`, `docker-compose.yml`, `CHANGELOG.md`,
    `config/freeradius/...`) — that narrowing would satisfy every one of
    those checks while silently dropping everything else. Multiple decoys
    spread across directories NONE of those checks cover close that gap.
    """
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "guard-test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "guard test"], cwd=repo, check=True)

    for relative in _DOTMAC_RO_SELF_REFERENCING_FILES:
        seeded = repo / relative
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text(
            "this file legitimately names dotmac_ro to describe and forbid it\n"
        )

    decoy_relatives = (
        Path("scripts/an_unrelated_operational_script.py"),
        Path("app/an_unrelated_application_module.py"),
        Path("alembic/versions/999_an_unrelated_migration.py"),
        Path(".github/workflows/an_unrelated_workflow.yml"),
        Path("a_brand_new_top_level_surface/an_unrelated_file.py"),
    )
    decoys = []
    for decoy_relative in decoy_relatives:
        decoy = repo / decoy_relative
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_text("nothing to do with the legacy role\n")
        decoys.append(decoy)

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed fake tracked files"], cwd=repo, check=True
    )

    scanned = set(_dotmac_ro_scan_targets(repo))

    for relative in _DOTMAC_RO_SELF_REFERENCING_FILES:
        assert repo / relative not in scanned, (
            f"{relative.as_posix()} must be excluded by _dotmac_ro_scan_targets "
            "over a real tracked-file selection"
        )

    for decoy in decoys:
        assert decoy in scanned, (
            f"{decoy.relative_to(repo).as_posix()} — a tracked file that is "
            "NOT one of the three accepted self-referencing files — was "
            "excluded from the scan targets; either the exclusion list has "
            "silently widened, or the enumeration itself has been narrowed "
            "away from unrestricted `git ls-files`"
        )

    # Set equality, not sampling: an independently computed `git ls-files`
    # over this same fake repo enumerates every tracked file, and the scan
    # target set must equal exactly that minus the three exclusions —
    # closing the enumeration question completely rather than moving the
    # goalpost by naming more decoys.
    independently_enumerated = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo,
    )
    independently_tracked_files = {
        repo / entry for entry in independently_enumerated.stdout.split("\0") if entry
    }
    expected_scanned = independently_tracked_files - {
        repo / relative for relative in _DOTMAC_RO_SELF_REFERENCING_FILES
    }
    assert scanned == expected_scanned, (
        "_dotmac_ro_scan_targets's output over this fake repo is not "
        "EXACTLY every tracked file minus the three accepted exclusions; "
        f"missing: {sorted(p.relative_to(repo).as_posix() for p in expected_scanned - scanned)}; "
        f"unexpected: {sorted(p.relative_to(repo).as_posix() for p in scanned - expected_scanned)}"
    )


def test_the_scan_refuses_an_index_symlink_replaced_by_a_regular_file_on_disk(
    tmp_path,
) -> None:
    """Sensitivity proof for THE defect an independent review found.

    An earlier version of this guard read only the WORKING TREE — `git
    ls-files -z` names paths, and `_read_verified_tracked_bytes` then
    inspects whatever currently sits on disk at each one. That leaves a
    real gap: if the git INDEX records a path as a symlink (mode `120000`)
    but the working tree has been locally replaced with an ORDINARY
    regular file, the on-disk `O_NOFOLLOW` walk sees a perfectly normal
    file and scans it — it never learns the index disagrees, because the
    index is a different source of truth the walk never reads.

    This builds a real git repository, stages a blob directly into the
    INDEX at mode `120000` via `git update-index --add --cacheinfo`
    (which never touches the working tree, so index and disk can be made
    to disagree on purpose), then writes an ORDINARY regular file at that
    exact path on disk. `_dotmac_ro_scan_targets` must refuse this path on
    the index's word alone — before `_read_verified_tracked_bytes` or
    anything else ever reads a byte from the substituted regular file.
    """
    repo = tmp_path / "fake_repo_index_symlink"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "guard-test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "guard test"], cwd=repo, check=True)

    for relative in _DOTMAC_RO_SELF_REFERENCING_FILES:
        seeded = repo / relative
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text(
            "this file legitimately names dotmac_ro to describe and forbid it\n"
        )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed the three accepted files"],
        cwd=repo,
        check=True,
    )

    blob_sha = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input="target_of_the_symlink\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    substituted_relative = "scripts/index_says_symlink_disk_says_regular_file.py"
    subprocess.run(
        [
            "git",
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{blob_sha},{substituted_relative}",
        ],
        cwd=repo,
        check=True,
    )

    substituted_path = repo / substituted_relative
    substituted_path.parent.mkdir(parents=True, exist_ok=True)
    substituted_path.write_text("an ordinary regular file, not a symlink\n")

    with pytest.raises(AssertionError, match=_SCAN_REFUSAL_MARKER):
        _dotmac_ro_scan_targets(repo)


def test_the_scan_refuses_a_gitlink_recorded_in_the_index(tmp_path) -> None:
    """Sensitivity proof: a real gitlink (submodule) entry, mode `160000`, must REFUSE.

    `git update-index --add --cacheinfo 160000,<sha>,<path>` stages a
    genuine gitlink entry without needing an actual submodule checkout —
    git does not validate a gitlink's SHA against a real object, because
    it names a commit in a DIFFERENT repository this one never stores.
    `_dotmac_ro_scan_targets` must refuse it on the index's word alone.
    """
    repo = tmp_path / "fake_repo_gitlink"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "guard-test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "guard test"], cwd=repo, check=True)

    for relative in _DOTMAC_RO_SELF_REFERENCING_FILES:
        seeded = repo / relative
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text(
            "this file legitimately names dotmac_ro to describe and forbid it\n"
        )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed the three accepted files"],
        cwd=repo,
        check=True,
    )

    fake_commit_sha = "a" * 40
    subprocess.run(
        [
            "git",
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{fake_commit_sha},vendor/fake_submodule",
        ],
        cwd=repo,
        check=True,
    )

    with pytest.raises(AssertionError, match=_SCAN_REFUSAL_MARKER):
        _dotmac_ro_scan_targets(repo)


def test_the_scan_refuses_an_index_mode_outside_the_allowlist(
    monkeypatch, tmp_path
) -> None:
    """Sensitivity proof: an index mode this scan has never named must REFUSE.

    `_ALLOWED_INDEX_MODES` is an ALLOWLIST (`100644`, `100755`), not a
    denylist of the two known-bad modes (`120000`, `160000`). This proves
    the allowlist's actual reason for existing: a mode that is neither
    ordinary-file nor symlink-nor-gitlink — one this scan has never even
    named — must still refuse, not silently scan, because it was never
    proven safe.

    STATED PREMISE, checked directly rather than assumed: on git 2.39.5,
    no supported git can actually RECORD such a mode. `update-index
    --cacheinfo`, `update-index --index-info`, and even `mktree` all exit
    0 and silently NORMALIZE an unrecognized `100xxx` permission bit to
    `100644` (or `100755` on the executable bit) — `mktree` writes the
    normalized mode into the tree object itself, so a `read-tree` of that
    tree yields `100644` too, not the mode that was asked for. A plant
    that staged, say, `100600` through any of those git commands would
    therefore record a PERMITTED mode and trigger no refusal at all — it
    would fail while appearing to prove the guard broken, and someone
    "fixing" that failure by loosening the allowlist would be repairing a
    test artifact, not a real gap. Building the plant that way is exactly
    the mistake this allowlist exists to prevent, so this test does not
    attempt it.

    Instead this drives `_tracked_files_with_modes` directly, at the
    PARSER level: `subprocess.run` is monkeypatched to return a synthetic
    `git ls-files --stage -z` record naming mode `100600`, with no git
    process and no filesystem involved anywhere in this test. That proves
    the refusal is REACHABLE given such a record — a future git, a
    hand-written or corrupted index, or any other producer of this exact
    on-disk format — not that the record is producible by git today. It
    also satisfies "not through the filesystem-shape check" more exactly
    than a staged file could: there is no filesystem-shape check in this
    test's path at all, only the allowlist comparison itself.
    """
    synthetic_stdout = (
        b"100600 " + b"a" * 40 + b" 0\tscripts/never_actually_stageable.py\0"
    )

    def _fake_git_ls_files_stage(*args, **kwargs):
        assert args[0] == ["git", "ls-files", "--stage", "-z"], (
            "this fake only understands the exact invocation "
            "_tracked_files_with_modes makes; anything else means the "
            "function under test changed and this plant needs revisiting"
        )
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=synthetic_stdout, stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", _fake_git_ls_files_stage)

    with pytest.raises(AssertionError, match=_SCAN_REFUSAL_MARKER) as excinfo:
        _tracked_files_with_modes(tmp_path)
    assert "100600" in str(excinfo.value), (
        "the refusal must name the offending mode explicitly, not just say 'refused'"
    )


def test_the_scan_refuses_a_non_utf8_tracked_filename(tmp_path) -> None:
    """Sensitivity proof: a non-UTF-8 tracked filename must REFUSE via a typed error, not crash.

    Git treats a path as raw bytes, not text — a filename need not be
    valid UTF-8 to be tracked. `_tracked_files_with_modes` decodes each
    filename explicitly, one record at a time, so an undecodable name
    raises a controlled `AssertionError` with a stable message here — not
    an uncontrolled `UnicodeDecodeError` from decoding the whole
    subprocess output at once, which would carry no stable refusal
    contract at all.

    The invalid-UTF-8 path is staged directly into the INDEX via
    `git update-index --add --cacheinfo`, never touching the working
    tree, specifically so this test does not depend on whether the local
    filesystem itself accepts an invalid-UTF-8 filename (some do not).
    """
    repo = tmp_path / "fake_repo_non_utf8_filename"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "guard-test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "guard test"], cwd=repo, check=True)

    for relative in _DOTMAC_RO_SELF_REFERENCING_FILES:
        seeded = repo / relative
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text(
            "this file legitimately names dotmac_ro to describe and forbid it\n"
        )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed the three accepted files"],
        cwd=repo,
        check=True,
    )

    blob_sha = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=b"nothing to do with the legacy role\n",
        capture_output=True,
        check=True,
    ).stdout.strip()

    non_utf8_cacheinfo = b"100644," + blob_sha + b",invalid_\xffname.sql"
    subprocess.run(
        [b"git", b"update-index", b"--add", b"--cacheinfo", non_utf8_cacheinfo],
        cwd=repo,
        check=True,
    )

    with pytest.raises(AssertionError, match="not valid UTF-8"):
        _dotmac_ro_scan_targets(repo)


def test_the_scan_refuses_a_symlinked_leaf(tmp_path) -> None:
    """Sensitivity proof: a tracked-looking LEAF that is a symlink must REFUSE.

    Plants a real file and a symlink to it at the path the scan is asked to
    read, then proves `_files_containing` raises rather than silently
    reading through the symlink (a stat-then-read check with no refusal
    would have simply followed it).

    Neither fixture path contains the word "symlink" — if it did, deleting
    the `ELOOP` branch entirely would still leave the generic fallback
    branch's message (which embeds the path) satisfying a `match="symlink"`
    assertion for the wrong reason.

    This asserts `_SCAN_REFUSAL_MARKER`, not branch-specific wording like
    "without following a" (the `ELOOP` branch's own phrase) — CI on the
    hosted (Linux) runner found that the sibling ancestor-symlink test
    below asserted exactly that branch-specific phrase, and a symlinked
    ancestor opened with `O_DIRECTORY` raises `ENOTDIR` on Linux instead of
    `ELOOP`, landing in the GENERIC branch and failing a correct guard. A
    plain leaf-level `O_NOFOLLOW` (no `O_DIRECTORY`, this test's case) is
    expected to raise `ELOOP` consistently, but asserting the shared
    marker instead of the mechanism makes that expectation irrelevant:
    this test passes regardless of which branch actually fires on
    whichever platform runs it.
    """
    real_target = tmp_path / "aliased_target.sql"
    real_target.write_text("nothing to do with the legacy role\n")
    leaf_via_alias = tmp_path / "leaf_via_alias.sql"
    leaf_via_alias.symlink_to(real_target)

    with pytest.raises(AssertionError, match=_SCAN_REFUSAL_MARKER):
        _files_containing([leaf_via_alias], "dotmac_ro", tmp_path)


def test_the_scan_refuses_a_missing_tracked_path(tmp_path) -> None:
    """Sensitivity proof: a tracked path missing on disk must REFUSE, not vanish.

    No file is ever created at this path — it stands in for a dangling
    symlink target, an unfetched gitlink/submodule, or a sparse-checkout
    exclusion. `_files_containing` must raise rather than silently treating
    the missing path as "nothing to scan." Asserts `_SCAN_REFUSAL_MARKER`
    rather than branch-specific wording, for the same reason given in
    `test_the_scan_refuses_a_symlinked_leaf`.
    """
    missing = tmp_path / "never_created.sql"

    with pytest.raises(AssertionError, match=_SCAN_REFUSAL_MARKER):
        _files_containing([missing], "dotmac_ro", tmp_path)


def test_the_scan_refuses_a_non_regular_leaf(tmp_path) -> None:
    """Sensitivity proof: a tracked path that is a directory (the gitlink shape) must REFUSE.

    A gitlink/submodule entry in `git ls-files` names a path with no
    regular-file content behind it; a real submodule directory is the
    concrete case, stood in for here by a plain directory at that path.
    `_files_containing` must raise rather than silently skipping it.
    Asserts `_SCAN_REFUSAL_MARKER` rather than branch-specific wording, for
    the same reason given in `test_the_scan_refuses_a_symlinked_leaf` —
    this branch is not errno-dependent today, but the marker keeps every
    refusal test in this file uniform and immune to the same class of
    platform surprise regardless.
    """
    non_regular = tmp_path / "looks_like_a_submodule"
    non_regular.mkdir()

    with pytest.raises(AssertionError, match=_SCAN_REFUSAL_MARKER):
        _files_containing([non_regular], "dotmac_ro", tmp_path)


def test_the_scan_refuses_an_ancestor_symlink_not_just_the_leaf(tmp_path) -> None:
    """Sensitivity proof: a symlinked ANCESTOR directory must REFUSE, not be silently followed.

    This is the exact gap a leaf-only check misses: `scripts/` (or any
    tracked directory) replaced in the worktree by a symlink to a
    DIFFERENT directory holding a same-named regular file. The leaf itself
    is not a symlink, `exists()`/`is_file()` succeed by following the
    parent, and a leaf-only check would silently scan the SUBSTITUTE
    content instead of the tracked one. `_files_containing` must refuse
    the moment it tries to open the symlinked ancestor component, before
    ever reaching the leaf.

    Neither fixture path contains the word "symlink", for the same reason
    given in `test_the_scan_refuses_a_symlinked_leaf`.

    This asserts `_SCAN_REFUSAL_MARKER`, not `ELOOP`-specific wording. CI on
    the hosted (Linux) runner failed this exact test when it asserted
    `match="without following a"`: opening a symlinked ancestor with
    `O_NOFOLLOW | O_DIRECTORY` raises `ELOOP` on macOS (where this guard
    was written) but `ENOTDIR` on Linux (confirmed by that CI failure) —
    landing in the GENERIC fallback branch there instead, which does not
    contain that phrase. The guard refused correctly either way; only the
    test's assertion named one platform's mechanism. Asserting the shared
    marker instead asserts the actual security property under test.
    """
    real_directory = tmp_path / "real_directory"
    real_directory.mkdir()
    substitute_file = real_directory / "leaf.sql"
    substitute_file.write_text("substituted content, not the tracked content\n")

    ancestor_via_alias = tmp_path / "ancestor_via_alias"
    ancestor_via_alias.symlink_to(real_directory, target_is_directory=True)

    tracked_looking_path = ancestor_via_alias / "leaf.sql"

    with pytest.raises(AssertionError, match=_SCAN_REFUSAL_MARKER):
        _files_containing([tracked_looking_path], "dotmac_ro", tmp_path)


def test_the_scan_refuses_a_fifo_leaf_without_hanging(tmp_path) -> None:
    """Sensitivity proof: a tracked-looking LEAF that is a FIFO must REFUSE, not hang.

    A blocking `open()` on a FIFO with no writer connected blocks forever —
    the existing non-regular-leaf test only plants a plain directory, which
    opens without blocking, so it never exercised this. `os.mkfifo` creates
    a real FIFO with no writer. `_files_containing` must refuse immediately
    (via `O_NONBLOCK` on the leaf's open) rather than hang: a guard that
    hangs produces no verdict at all, which is worse than one that fails.
    Asserts `_SCAN_REFUSAL_MARKER` rather than branch-specific wording, for
    the same reason given in `test_the_scan_refuses_a_symlinked_leaf`.
    """
    fifo_leaf = tmp_path / "fifo_leaf.sql"
    os.mkfifo(fifo_leaf)

    with pytest.raises(AssertionError, match=_SCAN_REFUSAL_MARKER):
        _files_containing([fifo_leaf], "dotmac_ro", tmp_path)


def test_the_scan_refuses_via_the_generic_branch_for_an_uncategorized_os_error(
    tmp_path,
) -> None:
    """Sensitivity proof: an OSError that is neither ENOENT nor ELOOP must still REFUSE.

    Nothing exercised the generic (neither-missing-nor-symlink) branch of
    `_read_verified_tracked_bytes`'s error handling before this. Addressing
    an ordinary regular file as though it were an ancestor DIRECTORY of a
    deeper path makes the intermediate `open(..., O_DIRECTORY)` fail with
    `ENOTDIR` — an errno this scan does not specifically name — and it must
    still raise a refusal through the generic fallback branch rather than
    let a raw `OSError` propagate or silently skip the path. `ENOTDIR` for
    an `O_DIRECTORY` open against a plain regular file is POSIX-guaranteed
    and platform-consistent, unlike the symlink-plus-`O_DIRECTORY` case
    above — but this still asserts `_SCAN_REFUSAL_MARKER`, not
    `"could not be opened"` (the generic branch's own specific wording),
    for uniformity with every other refusal test in this file: the
    property under test is "an AssertionError, not a raw OSError, and not
    a silent skip," which the shared marker proves without also claiming
    to identify which branch fired.
    """
    regular_file = tmp_path / "regular_file_pretending_to_be_a_directory"
    regular_file.write_text("an ordinary file, not a directory\n")

    tracked_looking_path = regular_file / "leaf.sql"

    with pytest.raises(AssertionError, match=_SCAN_REFUSAL_MARKER):
        _files_containing([tracked_looking_path], "dotmac_ro", tmp_path)
