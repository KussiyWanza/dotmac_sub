"""The audit middleware's defaults and its specs must state the same thing.

The five request-audit controls (`enabled`, `methods`, `skip_paths`,
`read_trigger_header`, `read_trigger_query`) were writable through
`settings_api_custom` with no `SettingSpec` at all: the custom handler validates
against its own `_AUDIT_SETTING_*` sets, so nothing ever required a declaration.
They are declared now, ahead of routing that handler through the spec.

Until the handler IS routed through it, two statements of the same defaults
exist — the specs, and `app.main._default_audit_settings`, which the middleware
reads directly rather than via `resolve_value`. That duplication is deliberate
and temporary (expand before contract), but a duplicate that nobody checks is
exactly the drift this whole slice is removing. So it gets pinned rather than
trusted, and this test retires when `_default_audit_settings` derives from the
specs.
"""

from __future__ import annotations

from app.main import _default_audit_settings
from app.models.domain_settings import SettingDomain
from app.services.settings_spec import get_spec

AUDIT_CONTROL_KEYS = (
    "enabled",
    "methods",
    "skip_paths",
    "read_trigger_header",
    "read_trigger_query",
)


def test_every_audit_control_is_declared() -> None:
    for key in AUDIT_CONTROL_KEYS:
        assert get_spec(SettingDomain.audit, key) is not None, (
            f"audit.{key} is writable through the settings API but has no "
            "SettingSpec; declare it in app/services/settings_spec.py"
        )


def test_the_spec_defaults_match_the_middleware_defaults() -> None:
    runtime = _default_audit_settings()
    assert set(runtime) == set(AUDIT_CONTROL_KEYS), (
        "the middleware grew or lost an audit control; declare or retire its "
        "spec in the same change"
    )

    for key in AUDIT_CONTROL_KEYS:
        spec = get_spec(SettingDomain.audit, key)
        assert spec is not None
        expected = runtime[key]
        # `methods` is a set at runtime for membership testing and a list in the
        # spec, because a JSON setting has no set literal. Compare as sets so
        # the container type is free but the MEMBERS are not.
        if isinstance(expected, set | list):
            assert set(spec.default) == set(expected), (
                f"audit.{key}: spec default {spec.default!r} disagrees with "
                f"app.main._default_audit_settings {expected!r}"
            )
        else:
            assert spec.default == expected, (
                f"audit.{key}: spec default {spec.default!r} disagrees with "
                f"app.main._default_audit_settings {expected!r}"
            )
