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


def verify_worker_nodes(expected_nodes: Sequence[str], *, timeout: float) -> bool:
    expected = set(expected_nodes)
    broker_url = os.getenv("CELERY_BROKER_URL")
    if not expected or not broker_url:
        return False

    probe = Celery("dotmac_deploy_worker_probe", broker=broker_url)
    replies = probe.control.ping(
        destination=sorted(expected),
        timeout=timeout,
    )
    return _responding_nodes(replies) == expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("nodes", nargs="+")
    args = parser.parse_args()
    return 0 if verify_worker_nodes(args.nodes, timeout=args.timeout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
