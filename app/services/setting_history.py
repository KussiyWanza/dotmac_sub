"""The one writer of settings history, and the one reader of it.

Recorded at the MODEL boundary rather than in `DomainSettings`, for the same
reason `_reject_undeclared_domain` lives there: the service layer is not the
only writer. Seeds, the generic settings API, the admin surface and several
domain services all persist rows, and a history that only covers the paths
somebody remembered is worse than none — it reads as a complete record and is
not.

## Who records what, during the cutover

Sub's settings writes are moving onto `dotmac_kernel.settings_resolver`'s
writers, which record history themselves via `_record_history`. The two cannot
double up: the kernel writes through the kernel's own `DomainSetting` class, and
these listeners are attached to SUB's, so a row written by the kernel never
reaches them. When the last Sub writer moves, this module's listeners stop
firing and can be deleted; until then it is the only recorder there is.

## The actor is usually absent, and that is honest

Sub's staff identity is a system user, not a `Party`, so `changed_by_party_id`
is NULL for almost every change today. The kernel's model makes every actor
field optional precisely because a seed, a migration or a CLI genuinely has no
actor — and recording "unknown" honestly beats inventing one. Request context
reaches this through `set_change_context`, which the web adapters set; nothing
guesses.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_setting_history import (
    DomainSettingHistory,
)


@dataclass(frozen=True, slots=True)
class SettingChangeContext:
    """Who is making a settings change, and on what request.

    Frozen, and every field a scalar — no mutable container to be shared
    between two changes and then edited. Mirrors the kernel's context of the
    same name so the cutover is a swap rather than a translation.
    """

    actor_party_id: Any | None = None
    reason: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


#: Set per request/task by whoever knows the actor. A `ContextVar` rather than a
#: parameter because the recorder is an ORM event with no access to the caller,
#: and threading an actor through every settings writer would mean editing every
#: one of them to record anything at all.
_change_context: contextvars.ContextVar[SettingChangeContext | None] = (
    contextvars.ContextVar("setting_change_context", default=None)
)


def set_change_context(context: SettingChangeContext | None) -> contextvars.Token:
    """Name the actor for changes made on this task. Returns a reset token."""

    return _change_context.set(context)


def reset_change_context(token: contextvars.Token) -> None:
    _change_context.reset(token)


def current_change_context() -> SettingChangeContext:
    return _change_context.get() or SettingChangeContext()


def history_for(
    db: Session, domain: object, key: str, *, limit: int = 50
) -> list[DomainSettingHistory]:
    """Recorded transitions for one setting, newest first."""

    return list(
        db.scalars(
            select(DomainSettingHistory)
            .where(DomainSettingHistory.domain == domain)
            .where(DomainSettingHistory.key == key)
            .order_by(DomainSettingHistory.changed_at.desc())
            .limit(limit)
        )
    )
