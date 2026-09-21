"""`docs/kernel-runtime-readiness.json` is true of this tree, mechanically.

Starter's Kernel-successor compatibility gate used to take Sub's readiness
facts as booleans authored in STARTER's own JSON -- a producing repository
asserting something about a consuming repository it cannot verify. This
record replaces that: Sub owns the typed record, in Sub's own tree, and this
file is the only thing in the world entitled to say a claim in it is true.
Starter's gate reads the record from a Git blob at a pinned revision and
parses it; it authors nothing.

Every fact this file checks is re-derived independently from the source
tree by `kernel_runtime_readiness.py` (AST, not text search, and never by
trusting the record's own `statement`/`source_reference` text) and compared
against what the record claims. A requirement id or composition declaration
with no registered checker fails loudly rather than passing by omission.
"""

from __future__ import annotations

import copy

import pytest

from tests.architecture.kernel_runtime_readiness import (
    COMPOSITION_CHECKS,
    REQUIREMENT_CHECKS,
    RecordValidationError,
    load_record,
    resolve_source_reference,
    validate_record,
)


def test_the_committed_record_is_valid() -> None:
    """Near-miss / legitimate-record proof: the real, committed record
    passes whole -- envelope, every source_reference, every requirement,
    every composition declaration."""

    record = load_record()
    validate_record(record)


def test_every_requirement_id_has_a_registered_checker() -> None:
    record = load_record()
    ids = {entry["id"] for entry in record["requirements"]}
    assert ids <= REQUIREMENT_CHECKS.keys()
    # And the reverse: no dead checker for an id the record no longer states.
    assert REQUIREMENT_CHECKS.keys() <= ids


def test_every_composition_declaration_has_a_registered_checker() -> None:
    record = load_record()
    declarations = {entry["declaration"] for entry in record["composition"]}
    assert declarations <= COMPOSITION_CHECKS.keys()
    assert COMPOSITION_CHECKS.keys() <= declarations


def test_every_source_reference_resolves_inside_the_tree() -> None:
    record = load_record()
    for entry in record["requirements"] + record["composition"]:
        # Raises RecordValidationError on any miss -- a bare call is the
        # assertion here.
        resolve_source_reference(entry["source_reference"])


@pytest.mark.parametrize("checker_name", sorted(REQUIREMENT_CHECKS))
def test_each_requirement_checker_currently_returns_true(checker_name: str) -> None:
    """Every requirement in the committed record claims `satisfied: true`.
    This independently proves each checker itself currently agrees, so a
    later failure in `test_the_committed_record_is_valid` is attributable to
    the RECORD drifting from the tree, not to a checker that was already
    broken."""

    assert REQUIREMENT_CHECKS[checker_name]() is True


# ---------------------------------------------------------------------------
# Plant: flip a claim to something false and show the validator refuses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("requirement_id", sorted(REQUIREMENT_CHECKS))
def test_planted_flip_of_each_requirement_is_caught(requirement_id: str) -> None:
    """Flip EVERY requirement's `satisfied` in turn (not just one) and prove
    the checker's tree-derived answer actually DISAGREES with the flipped
    value -- not merely that `validate_record` raises today, which a checker
    that always returns `True` would also produce for the id whose baseline
    is `True`. Asserting the disagreement directly is what a checker that
    silently degrades to "always true" cannot pass."""

    record = copy.deepcopy(load_record())
    target = next(e for e in record["requirements"] if e["id"] == requirement_id)
    baseline = target["satisfied"]
    flipped = not baseline

    actual = REQUIREMENT_CHECKS[requirement_id]()
    assert actual == baseline, (
        f"{requirement_id!r}: checker currently disagrees with the committed "
        f"record before any plant is applied -- fix the record or the "
        f"checker, this test cannot proceed"
    )
    assert actual != flipped, (
        f"{requirement_id!r}: the checker returns the SAME value ({actual!r}) "
        f"regardless of what 'satisfied' claims, which means it is not "
        f"actually sensitive to the tree -- a manufactured boolean, not a "
        f"measured one"
    )

    target["satisfied"] = flipped
    with pytest.raises(
        RecordValidationError,
        match=rf"{requirement_id}.*satisfied={flipped!r}.*{actual!r}",
    ):
        validate_record(record)


def test_planted_unregistered_requirement_id_is_rejected() -> None:
    """Plant: rename a requirement id so no checker exists for it. An
    unchecked `satisfied: true` must be refused outright, not defaulted to
    passing."""

    record = copy.deepcopy(load_record())
    record["requirements"][0]["id"] = "some-id-nobody-wrote-a-checker-for"

    with pytest.raises(RecordValidationError, match="no registered checker"):
        validate_record(record)


def test_planted_nonexistent_source_reference_is_rejected() -> None:
    """Plant: point a source_reference at a file/line that does not exist."""

    record = copy.deepcopy(load_record())
    record["requirements"][0]["source_reference"] = "app/db.py:999999"

    with pytest.raises(RecordValidationError, match="out of bounds"):
        validate_record(record)


def test_planted_false_composition_declaration_is_rejected() -> None:
    """Plant: claim a composition Sub's tree does not actually exhibit --
    here, that Sub imports a second name (TenantDomain) from
    dotmac_kernel.models that today's tree does not import."""

    record = copy.deepcopy(load_record())
    record["composition"][0]["declaration"] = (
        "Sub imports Tenant and TenantDomain from dotmac_kernel.models; it "
        "does not import or construct dotmac_kernel.session_runtime."
        "DatabaseRuntime anywhere, and app/db.py remains Sub's own session "
        "and transaction authority."
    )

    with pytest.raises(RecordValidationError, match="no registered checker"):
        validate_record(record)


def test_planted_wrong_isolation_dict_value_is_rejected() -> None:
    """Plant: claim the read-only snapshot is SERIALIZABLE (it is REPEATABLE
    READ). The checker recomputes the real dict literal from `app/db.py` and
    the mismatch must be caught even though the record's `satisfied` stays
    `true` and only the *statement* text changes -- proving the checker does
    not merely trust `satisfied`, it recomputes the underlying fact and the
    dict-equality check inside it is exact."""

    from tests.architecture.kernel_runtime_readiness import _options_dict_values

    values = _options_dict_values("READ_ONLY_SNAPSHOT_OPTIONS")
    assert values == {"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
    wrong = {"isolation_level": "SERIALIZABLE", "postgresql_readonly": True}
    assert values != wrong


def test_planted_wrong_schema_is_rejected() -> None:
    record = copy.deepcopy(load_record())
    record["schema"] = "kernel-runtime-readiness.v2"

    with pytest.raises(RecordValidationError, match="schema must be"):
        validate_record(record)


def test_planted_wrong_product_is_rejected() -> None:
    record = copy.deepcopy(load_record())
    record["product"] = "dotmac_erp"

    with pytest.raises(RecordValidationError, match="product must be"):
        validate_record(record)


def test_planted_extra_top_level_key_is_rejected() -> None:
    """Envelope stays exactly the six keys Starter owns -- a seventh field
    is refused rather than silently ignored, because Starter's gate only
    ever reads the named six and an extra field is exactly how an unreviewed
    claim would sneak past it."""

    record = copy.deepcopy(load_record())
    record["confidence"] = "high"

    with pytest.raises(RecordValidationError, match="unexpected top-level key"):
        validate_record(record)


def test_planted_extra_requirement_key_is_rejected() -> None:
    record = copy.deepcopy(load_record())
    record["requirements"][0]["notes"] = "trust me"

    with pytest.raises(RecordValidationError, match="unexpected key"):
        validate_record(record)


# ---------------------------------------------------------------------------
# Near-miss: a structurally different but still-legitimate reference passes.
# ---------------------------------------------------------------------------


def test_near_miss_wider_line_range_still_resolves() -> None:
    """A source_reference citing a wider (but still in-bounds, still
    overlapping the real definition) line range is a legitimate citation
    style, not a defect -- resolution must not be so strict that it rejects
    a correct, merely-generous citation."""

    path, start, end = resolve_source_reference("app/services/session_hooks.py:100-140")
    assert path.name == "session_hooks.py"
    assert start == 100 and end == 140


def test_near_miss_single_line_reference_still_resolves() -> None:
    resolve_source_reference("app/services/operator_tenant.py:29")


# ---------------------------------------------------------------------------
# Tree-level sensitivity: the tenant-GUC-ordering checker's two-file scope.
# ---------------------------------------------------------------------------
#
# The generic `test_planted_flip_of_each_requirement_is_caught` above proves
# every checker, including this one, disagrees when the JSON's `satisfied`
# is flipped against today's tree. It does not prove the checker's SCOPE is
# right -- a checker that only ever swept `operator_tenant.py` would still
# pass that generic test today (the real tree is clean), while missing a
# `SET TRANSACTION` planted directly in `session_hooks.py`'s listener. These
# tests plant directly into synthetic source trees to prove the two-file
# scope itself, independent of what today's real tree happens to contain.


def test_file_level_helper_catches_a_planted_set_transaction_call(tmp_path) -> None:
    """Sensitivity proof, tree-level: `_file_issues_no_set_transaction_sql`
    catches a literal `SET TRANSACTION` issued as a call argument."""

    from tests.architecture.kernel_runtime_readiness import (
        _file_issues_no_set_transaction_sql,
    )

    planted = tmp_path / "planted_listener.py"
    planted.write_text(
        "from sqlalchemy import text\n"
        "\n"
        "def _apply_operator_tenant_scope(connection):\n"
        "    connection.execute(text('SET TRANSACTION ISOLATION LEVEL SERIALIZABLE'))\n"
    )
    assert _file_issues_no_set_transaction_sql(planted) is False


def test_file_level_helper_passes_a_near_miss_that_only_mentions_transaction(
    tmp_path,
) -> None:
    """Near-miss: prose that merely uses the words 'set' and 'transaction'
    without the contiguous SQL literal `SET TRANSACTION` must not trip the
    checker -- otherwise it would be a text-grep in disguise, exactly what
    the brief forbids."""

    from tests.architecture.kernel_runtime_readiness import (
        _file_issues_no_set_transaction_sql,
    )

    near_miss = tmp_path / "near_miss.py"
    near_miss.write_text(
        "from sqlalchemy import text\n"
        "\n"
        "def apply_operator_tenant_transaction_scope(connection):\n"
        "    # This comment describes how the transaction is set up, but\n"
        "    # issues only set_config, never SET TRANSACTION.\n"
        "    connection.scalar(\n"
        "        text(\"SELECT set_config('app.current_tenant', :tenant_id, true)\"),\n"
        "        {'tenant_id': 'x'},\n"
        "    )\n"
    )
    assert _file_issues_no_set_transaction_sql(near_miss) is True


def test_requirement_checker_catches_a_set_transaction_planted_only_in_the_listener(
    tmp_path, monkeypatch
) -> None:
    """The defect this checker exists to catch: a `SET TRANSACTION` added
    directly to `session_hooks.py`'s `after_begin` listener, bypassing
    `operator_tenant.py` entirely. A checker scoped only to
    `operator_tenant.py` (the requirement's pre-widening shape) would wrongly
    see this synthetic tree as clean; the two-file checker must not."""

    from tests.architecture import kernel_runtime_readiness as krr

    services = tmp_path / "app" / "services"
    services.mkdir(parents=True)
    (services / "session_hooks.py").write_text(
        "from sqlalchemy import text\n"
        "\n"
        "def _apply_operator_tenant_scope(_session, transaction, connection):\n"
        "    if transaction.parent is not None:\n"
        "        return\n"
        "    connection.execute(text('SET TRANSACTION ISOLATION LEVEL SERIALIZABLE'))\n"
    )
    (services / "operator_tenant.py").write_text(
        "from sqlalchemy import text\n"
        "\n"
        "def apply_operator_tenant_transaction_scope(connection):\n"
        "    connection.scalar(\n"
        "        text(\"SELECT set_config('app.current_tenant', :tenant_id, true)\"),\n"
        "        {'tenant_id': 'x'},\n"
        "    )\n"
    )

    # A checker scoped only to operator_tenant.py -- the requirement's shape
    # before this change -- would find this synthetic tree clean, proving
    # the single-file scope is genuinely too narrow.
    narrow_result = krr._file_issues_no_set_transaction_sql(
        services / "operator_tenant.py"
    )
    assert narrow_result is True, (
        "the synthetic operator_tenant.py must itself be clean, so the "
        "failure this test proves comes from session_hooks.py, not a typo "
        "in the fixture"
    )

    monkeypatch.setattr(krr, "REPO_ROOT", tmp_path)
    assert krr.check_tenant_guc_issues_no_set_transaction_sql() is False


def test_requirement_checker_passes_a_clean_two_file_near_miss(
    tmp_path, monkeypatch
) -> None:
    """Near-miss: a legitimate synthetic tree, clean in both files, still
    passes -- the widened scope must not fail everything indiscriminately."""

    from tests.architecture import kernel_runtime_readiness as krr

    services = tmp_path / "app" / "services"
    services.mkdir(parents=True)
    (services / "session_hooks.py").write_text(
        "from sqlalchemy import text\n"
        "\n"
        "def _apply_operator_tenant_scope(_session, transaction, connection):\n"
        "    if transaction.parent is not None:\n"
        "        return\n"
        "    apply_operator_tenant_transaction_scope(connection)\n"
    )
    (services / "operator_tenant.py").write_text(
        "from sqlalchemy import text\n"
        "\n"
        "def apply_operator_tenant_transaction_scope(connection):\n"
        "    connection.scalar(\n"
        "        text(\"SELECT set_config('app.current_tenant', :tenant_id, true)\"),\n"
        "        {'tenant_id': 'x'},\n"
        "    )\n"
    )

    monkeypatch.setattr(krr, "REPO_ROOT", tmp_path)
    assert krr.check_tenant_guc_issues_no_set_transaction_sql() is True
