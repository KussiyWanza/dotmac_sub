"""Provider registry.

Providers self-register at import time. ``all_providers()`` returns them in a
stable, deterministic order (by ``ItemKind``) so aggregation output is
reproducible regardless of import order.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.services.workqueue.providers.base import WorkqueueProvider
from app.services.workqueue.types import ItemKind

_REGISTRY: dict[ItemKind, WorkqueueProvider] = {}


def register(provider: WorkqueueProvider) -> WorkqueueProvider:
    _REGISTRY[provider.kind] = provider
    return provider


def all_providers() -> tuple[WorkqueueProvider, ...]:
    return tuple(_REGISTRY[kind] for kind in ItemKind if kind in _REGISTRY)


def get_provider(kind: ItemKind) -> WorkqueueProvider:
    return _REGISTRY[kind]


def registered_kinds() -> Iterable[ItemKind]:
    return tuple(kind for kind in ItemKind if kind in _REGISTRY)


def load_builtin_providers() -> tuple[WorkqueueProvider, ...]:
    """Import the built-in providers so their registration side effect runs."""
    from app.services.workqueue.providers import (  # noqa: F401
        conversations,
        projects,
        tickets,
        work_orders,
    )

    return all_providers()
