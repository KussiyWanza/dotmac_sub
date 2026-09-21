"""Transport-neutral errors returned by public domain service boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class DomainError(Exception):
    """Stable, safe error contract for adapters to translate.

    ``code`` is the machine contract. ``message`` is safe to expose to an
    operator or client. ``details`` must contain structured, non-secret
    decision evidence only; transport status codes do not belong here.

    ``retryable`` declares whether a durable event-handler caller (see
    ``app.services.events.dispatcher.EventDispatcher.dispatch``) should
    treat this failure as transient (worth a later automatic retry) or
    permanent (a genuine, reviewable failure that will not resolve itself on
    replay). Defaults to ``True`` so every existing raise site keeps its
    current transient/retry-eligible behavior with no code change required;
    a caller that knows a failure is terminal sets ``retryable=False``
    explicitly.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        retryable: bool = True,
    ) -> None:
        if not code.strip():
            raise ValueError("domain error code cannot be empty")
        if not message.strip():
            raise ValueError("domain error message cannot be empty")
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = retryable
        super().__init__(message)
