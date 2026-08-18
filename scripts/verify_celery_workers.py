"""Verify that an exact set of Celery worker nodes answers remote-control ping.

This deployment probe intentionally creates a minimal Celery client from the
container's broker environment. Importing ``app.celery_app`` would repeat full
task and scheduler discovery, making a bounded readiness check unnecessarily
slow and coupling the probe to application startup order.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterable, Mapping, Sequence

from celery import Celery


def _responding_nodes(
    replies: Iterable[Mapping[str, Mapping[str, object]]],
) -> set[str]:
    return {
        node
        for reply in replies
        for node, payload in reply.items()
        if payload.get("ok") == "pong"
    }


def _active_queue_names(
    queues_by_node: Mapping[str, Sequence[Mapping[str, object]]] | None,
) -> dict[str, set[str]]:
    return {
        node: {
            str(queue.get("name"))
            for queue in queues
            if isinstance(queue.get("name"), str)
        }
        for node, queues in (queues_by_node or {}).items()
    }


def verify_worker_nodes(
    expected_nodes: Sequence[str],
    *,
    timeout: float,
    required_queues: Mapping[str, Sequence[str]] | None = None,
) -> bool:
    expected = set(expected_nodes)
    broker_url = os.getenv("CELERY_BROKER_URL")
    if not expected or not broker_url:
        return False

    probe = Celery("dotmac_deploy_worker_probe", broker=broker_url)
    destinations = sorted(expected)
    replies = probe.control.ping(
        destination=destinations,
        timeout=timeout,
    )
    if _responding_nodes(replies) != expected:
        return False
    if not required_queues:
        return True
    inspector = probe.control.inspect(destination=destinations, timeout=timeout)
    active_queues = _active_queue_names(inspector.active_queues())
    return all(
        set(queue_names) <= active_queues.get(node, set())
        for node, queue_names in required_queues.items()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--require-queue",
        action="append",
        default=[],
        metavar="NODE=QUEUE",
    )
    parser.add_argument("nodes", nargs="+")
    args = parser.parse_args()
    required_queues: dict[str, list[str]] = {}
    for requirement in args.require_queue:
        node, separator, queue = requirement.partition("=")
        if not separator or not node or not queue:
            parser.error("--require-queue must use NODE=QUEUE")
        required_queues.setdefault(node, []).append(queue)
    return (
        0
        if verify_worker_nodes(
            args.nodes,
            timeout=args.timeout,
            required_queues=required_queues,
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
