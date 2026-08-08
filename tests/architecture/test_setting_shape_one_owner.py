"""`control.settings_spec` is the only declaration of a setting's shape.

Value type, bounds, allowed values, default and secrecy belong to `SettingSpec`.
The settings API selects a domain and key and delegates to
`_normalize_spec_setting`; it must not carry a private key list, type mapping,
bound or allowed set.

Seven per-domain handlers used to, and every one of them had drifted from the
spec it shadowed — a floor of 1 where the spec said 1024, a floor of 1 where the
spec said 5, a floor of 1 where the spec allowed 0, an unenforced `allowed` set,
and a key stored as a string where the spec said integer.

The copy also defeated `test_no_orphan_settings`, whose reader corpus counts a
quoted key literal anywhere under `app/`: `scheduler.refresh_minutes` had no
reader at all and was kept alive purely by appearing in a handler's key set.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API_MODULES = (
    REPO_ROOT / "app" / "services" / "settings_api_custom.py",
    REPO_ROOT / "app" / "services" / "settings_api_generic.py",
)

#: The one function allowed to decide whether a submitted value is acceptable.
DECIDER = "_normalize_spec_setting"


def _module_level_collections(path: Path) -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, ast.AST] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Set | ast.List | ast.Dict | ast.Tuple):
            found[target.id] = node.value
    return found


def test_no_settings_api_module_declares_a_private_key_set() -> None:
    """A per-domain key list is a second decision system, not an adapter."""

    offenders: list[str] = []
    for path in API_MODULES:
        for name, value in _module_level_collections(path).items():
            elements = getattr(value, "elts", None) or getattr(value, "keys", [])
            if not elements:
                continue
            if all(
                isinstance(element, ast.Constant) and isinstance(element.value, str)
                for element in elements
            ):
                offenders.append(f"{path.name}:{name}")
    assert not offenders, (
        "these modules declare setting keys of their own; the spec owns them "
        f"(docs/SOT_RELATIONSHIP_MAP.md): {sorted(offenders)}"
    )


def test_every_upsert_path_goes_through_the_spec() -> None:
    """No handler may normalise a payload by any route but the decider."""

    source = (REPO_ROOT / "app" / "services" / "settings_api_custom.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    normalisers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("_normalize_")
        and node.name != DECIDER
    }
    assert not normalisers, (
        "per-domain normalisers reimplement the spec's decision and drift from "
        f"it: {sorted(normalisers)}"
    )


def test_the_decider_consults_the_spec_bounds() -> None:
    """Guard the guard.

    If `_normalize_spec_setting` stopped reading `min_value`/`max_value`/
    `allowed`, every test above would still pass while the bounds went
    unenforced again — which is exactly the state this slice repaired.
    """

    tree = ast.parse(
        (REPO_ROOT / "app" / "services" / "settings_api_custom.py").read_text(
            encoding="utf-8"
        )
    )
    decider = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == DECIDER
    )
    consulted = {
        node.attr for node in ast.walk(decider) if isinstance(node, ast.Attribute)
    }
    for attribute in ("min_value", "max_value", "allowed", "value_type", "is_secret"):
        assert attribute in consulted, (
            f"{DECIDER} no longer reads spec.{attribute}; the spec would stop "
            "being the authority for that half of a setting's shape"
        )
