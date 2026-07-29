"""Native sales vertical services.

* ``service`` — leads, pipelines, quotes port (§2.1).
* ``selfserve`` — the self-serve quote extraction: feasibility (native FAP),
  estimate, map-pinned request, accept-with-deposit (§2.2).
"""

from app.services.sales import reports, selfserve
from app.services.sales.service import (
    LEAD_SOURCE_OPTIONS,
    Leads,
    Pipelines,
    PipelineStages,
    QuoteLineItems,
    Quotes,
    leads,
    pipeline_stages,
    pipelines,
    quote_line_items,
    quotes,
)

__all__ = [
    "LEAD_SOURCE_OPTIONS",
    "Leads",
    "PipelineStages",
    "Pipelines",
    "QuoteLineItems",
    "Quotes",
    "leads",
    "pipeline_stages",
    "pipelines",
    "quote_line_items",
    "quotes",
    "reports",
    "selfserve",
]
