from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPAIR = ROOT / "scripts" / "one_off" / "repair_notification_templates.py"


def test_notification_repair_uses_the_template_owner() -> None:
    source = REPAIR.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(REPAIR))

    assigned_attributes = {
        target.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
    }
    assert not {"subject", "body", "is_active"}.intersection(assigned_attributes)
    assert "db.delete(" not in source
    assert "notification_service.templates.update(" in source
    assert "notification_service.templates.delete(" in source
