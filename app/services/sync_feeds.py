"""Shared query contract for bounded cross-application sync feeds."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_
from sqlalchemy.orm import Query

SYNC_FEED_MAX_PAGE_SIZE = 500


def apply_sync_page(
    query: Query,
    model: Any,
    *,
    updated_since: datetime | None,
    limit: int,
    offset: int,
    after_updated_at: datetime | None = None,
    after_id: UUID | None = None,
) -> Query:
    """Apply the inclusive watermark and stable ordering used by every feed.

    ``after_updated_at``/``after_id`` are an ADDITIVE keyset-cursor mode
    (mirrors ``app.services.dotmac_erp.domain_sync._after_cursor``): when both
    are supplied, paging advances by ``(updated_at, id) > (after_updated_at,
    after_id)`` instead of ``OFFSET``. Omitting both leaves the existing
    offset-paged behavior byte-identical. Supplying exactly one is a caller
    error.

    Revision semantics, read carefully before "fixing" a perceived bug: a
    keyset cursor over a MUTABLE ``updated_at`` column cannot promise "each
    row exactly once" under concurrent writes — a row genuinely modified
    after being observed legitimately reappears with its new revision on a
    later page, and callers of this feed are idempotent on the row's natural
    key plus its source-updated-at, so a fresh revision is a valid new
    observation, not a duplicate. The property this cursor DOES guarantee,
    and the actual fix for the offset-based hazard, is narrower: a row whose
    own ``(updated_at, id)`` never changes during a walk is never skipped —
    unlike OFFSET paging, where a concurrent update to a DIFFERENT row can
    re-sort it across a page boundary and drop an untouched row from the
    walk entirely.
    """
    # A caller that holds one session open across several pages of a walk
    # (the shape a keyset cursor exists to support) would otherwise get back
    # its OWN stale, identity-mapped Python object for any row it already
    # touched earlier in that session — SQLAlchemy's default query behavior
    # does not refresh an already-identity-mapped row's attributes from a
    # fresh SELECT. `populate_existing` forces every row this query returns
    # to be refreshed from what was just read, so a row updated by another
    # session mid-walk is picked up on its current revision rather than
    # silently replayed as of whenever this session first saw it. See
    # `app/services/network_operation_dispatch.py` and
    # `app/services/prepaid_draft_reconciliation.py` for the same pattern
    # used elsewhere in this codebase for the identical staleness concern.
    query = query.execution_options(populate_existing=True)
    if updated_since is not None:
        query = query.filter(model.updated_at >= updated_since)
    query = query.order_by(model.updated_at.asc(), model.id.asc())
    if after_updated_at is not None or after_id is not None:
        if after_updated_at is None or after_id is None:
            raise ValueError(
                "after_updated_at and after_id must both be supplied together, "
                "or neither."
            )
        query = query.filter(
            or_(
                model.updated_at > after_updated_at,
                and_(
                    model.updated_at == after_updated_at,
                    model.id > after_id,
                ),
            )
        )
        return query.limit(limit)
    return query.offset(offset).limit(limit)


def sync_page_response(items: list[Any], *, limit: int, offset: int) -> dict[str, Any]:
    """Return the common offset-page envelope consumed by integration clients."""
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}
