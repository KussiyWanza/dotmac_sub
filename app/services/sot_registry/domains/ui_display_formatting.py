"""Canonical SOT declarations for the ui_display_formatting domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    SOTService,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="ui_display_formatting",
    services=(
        SOTService(
            name="ui.display_formatting",
            module="app.services.display_format",
            owns=(
                "display currency-code normalization",
                "single-value money formatting",
                "multi-currency summary grouping and ordering",
                "display-timezone resolution",
                "timestamp display formatting",
                "missing-value display marker",
            ),
            depends_on=("control.settings_spec",),
            notes=(
                "Domain services own amount, currency, unit, timestamp, and "
                "missing-value facts. Web and mobile renderers consume this "
                "projection and do not invent default currency or timezone."
            ),
        ),
    ),
    entrypoints=(
        "app.services.web_billing_overview",
        "app.services.web_billing_payments",
        "app.services.web_billing_ledger",
        "app.services.web_billing_reconciliation",
        "app.web.brand_globals",
        "mobile.lib.src.core.formatters",
    ),
    rule="Domain owners provide typed amount, currency, unit, timestamp, and "
    "availability facts. Display owners normalize and format them once. "
    "Mixed currencies remain separate and explicitly labeled; UI callers "
    "do not maintain local currency defaults or formatter copies.",
)
