"""Audit rows are written through two sanctioned surfaces only.

The audit table has one model (`AuditEvent`) and, as of this pin, exactly two
sanctioned writer surfaces with distinct transaction semantics:

- ``record_audit_event`` (``app/services/audit_adapter.py``) — the keyword
  facade for request/consequence paths; supports defer-until-commit via
  ``AuditEvents.record`` underneath.
- ``AuditEvents.stage`` (``app/services/audit.py``) — stages the row in the
  CALLER'S current transaction without committing; the correct surface for
  services that own their transaction outcome (billing/payments use this
  heavily and deliberately).

``AuditEvents.create`` (commits immediately), direct ``AuditEvents.record``
calls outside the adapter, and direct ``AuditEvent(...)`` construction are
banned. R1 migrated the seven direct model writers through the owner so every
new row receives the same actor validation and ``metadata`` → ``details``
dual-write.

Detection is AST-based and recognizes the actual instance form
``audit_service.audit_events.create(...)`` as well as the class form. Aliasing
(``x = audit_events; x.create(...)``) evades the walker — disclosed limit,
consistent with the repo's other AST governance tests.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

OWNER_MODULES = {
    "app/services/audit.py",
    "app/services/audit_adapter.py",
}
_BANNED_METHODS = {"create", "record"}
_AUDIT_FACADES = {"record_audit_event", "stage_audit_event"}
_ID_REQUIRED_ACTOR_TYPES = {"api_key", "service", "user"}


def _attribute_parts(node: ast.expr) -> tuple[str, ...]:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return tuple(reversed(parts))


def _banned_calls(source: str, rel: str) -> list[str]:
    offenders: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "AuditEvent":
            offenders.append(f"{rel}:{node.lineno} AuditEvent")
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        parts = _attribute_parts(node.func)
        if (
            node.func.attr in _BANNED_METHODS
            and len(parts) >= 2
            and parts[-2] in {"AuditEvents", "audit_events"}
        ):
            offenders.append(f"{rel}:{node.lineno} {'.'.join(parts)}")
    return offenders


def _incomplete_literal_actor_calls(source: str, rel: str) -> list[str]:
    """Reject statically-declared non-system actors with no possible identity."""

    offenders: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id not in _AUDIT_FACADES:
            continue
        keywords = {item.arg: item.value for item in node.keywords if item.arg}
        actor_type = keywords.get("actor_type")
        if not isinstance(actor_type, ast.Attribute):
            continue
        parts = _attribute_parts(actor_type)
        if len(parts) < 2 or parts[-2] != "AuditActorType":
            continue
        if parts[-1] not in _ID_REQUIRED_ACTOR_TYPES:
            continue
        actor_id = keywords.get("actor_id")
        if actor_id is None or (
            isinstance(actor_id, ast.Constant) and actor_id.value is None
        ):
            offenders.append(
                f"{rel}:{node.lineno} {node.func.id} "
                f"actor_type={parts[-1]} without actor_id"
            )
    return offenders


def test_no_direct_audit_create_or_record_outside_owners() -> None:
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel in OWNER_MODULES:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            offenders.extend(_banned_calls(source, rel))
        except SyntaxError:  # pragma: no cover — syntax is checked elsewhere
            continue
    assert not offenders, (
        "Direct AuditEvent construction or AuditEvents.create/.record outside "
        "the audit owners — use "
        "record_audit_event (app/services/audit_adapter.py) for request/"
        "consequence paths, or AuditEvents.stage inside a transaction-owning "
        "service: " + ", ".join(sorted(offenders))
    )


def test_literal_non_system_facade_actors_always_declare_an_id() -> None:
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            offenders.extend(_incomplete_literal_actor_calls(source, rel))
        except SyntaxError:  # pragma: no cover — syntax is checked elsewhere
            continue
    assert not offenders, (
        "Literal user/service/api_key audit actors need actor_id; use system "
        "for an honestly anonymous automated action or name the principal: "
        + ", ".join(sorted(offenders))
    )


def test_writer_detector_rejects_the_real_bypass_forms() -> None:
    source = """
AuditEvent(action="direct")
AuditEvents.create(db, payload)
audit_events.record(db, payload)
audit_service.audit_events.create(db=db, payload=payload)
audit_service.audit_events.stage(db=db, payload=payload)
record_audit_event(db, action="allowed", entity_type="test")
"""

    offenders = _banned_calls(source, "app/example.py")

    assert len(offenders) == 4
    assert any("AuditEvent" in offender for offender in offenders)
    assert not any("stage" in offender for offender in offenders)
    assert not any("record_audit_event" in offender for offender in offenders)


def test_literal_actor_detector_rejects_missing_and_explicit_null_ids() -> None:
    source = """
stage_audit_event(db, actor_type=AuditActorType.service)
record_audit_event(db, actor_type=AuditActorType.user, actor_id=None)
stage_audit_event(db, actor_type=AuditActorType.api_key, actor_id="key-1")
stage_audit_event(db, actor_type=AuditActorType.system)
"""

    offenders = _incomplete_literal_actor_calls(source, "app/example.py")

    assert len(offenders) == 2
    assert any("service" in offender for offender in offenders)
    assert any("user" in offender for offender in offenders)
