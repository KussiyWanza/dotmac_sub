"""Live egress confinement through a real PodmanEgressGateway.

Phase 4b of ADR 0005. Proves the property the whole phase exists for: a
connector reaches exactly the hosts its manifest declares, and nothing else —
including when it ignores its proxy environment entirely.

Skipped where Podman or the proxy image is unavailable. Meant to run on seabone.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.services.integrations.egress_gateway import (
    PROXY_IMAGE,
    PodmanEgressGateway,
    network_name,
    proxy_name,
)
from app.services.integrations.egress_policy import EgressPolicy

pytestmark = pytest.mark.skipif(
    shutil.which("podman") is None, reason="rootless Podman not available"
)

CONNECTOR = "egresstest"
ALLOWED_HOST = "example.com"
DENIED_HOST = "api.paystack.co"
CLIENT_IMAGE = "python:3.12-alpine"


def _podman(*args: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["podman", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _image_present(image: str) -> bool:
    return _podman("image", "exists", image).returncode == 0


@pytest.fixture(scope="module")
def attachment():
    if not _image_present(PROXY_IMAGE):
        pytest.skip(f"{PROXY_IMAGE} not built on this host")
    if not _image_present(CLIENT_IMAGE):
        pytest.skip(f"{CLIENT_IMAGE} not present on this host")
    gateway = PodmanEgressGateway()
    attached = gateway.attach(
        connector_key=CONNECTOR, policy=EgressPolicy(hosts=(ALLOWED_HOST,))
    )
    yield attached
    _podman("rm", "--force", proxy_name(CONNECTOR))
    _podman("network", "rm", network_name(CONNECTOR))


def _fetch(url: str, attachment, *, use_proxy: bool) -> str:
    """Attempt a fetch from inside the confined network; report the outcome."""
    script = (
        "import urllib.request\n"
        "try:\n"
        f"    r = urllib.request.urlopen({url!r}, timeout=12)\n"
        "    print('STATUS', r.status)\n"
        "except Exception as e:\n"
        "    print('ERR', type(e).__name__, str(e)[:120])\n"
    )
    argv = ["run", "--rm", f"--network={attachment.network}"]
    if use_proxy:
        argv += [f"--env=https_proxy={attachment.proxy_url}"]
    argv += [CLIENT_IMAGE, "python3", "-c", script]
    return _podman(*argv).stdout.strip()


def test_an_allowed_host_is_reachable_through_the_gateway(attachment):
    out = _fetch(f"https://{ALLOWED_HOST}", attachment, use_proxy=True)
    assert "STATUS 200" in out, out


def test_a_host_outside_the_allowlist_is_refused_by_the_proxy(attachment):
    out = _fetch(f"https://{DENIED_HOST}/bank", attachment, use_proxy=True)
    assert "Tunnel connection failed" in out or "403" in out, out
    assert "STATUS 200" not in out


def test_ignoring_the_proxy_reaches_nothing(attachment):
    """Confinement is the absent route, not the proxy variable.

    A connector that ignores its proxy environment must still be unable to
    reach even an allowlisted host directly.
    """
    out = _fetch(f"https://{ALLOWED_HOST}", attachment, use_proxy=False)
    assert "STATUS" not in out, out
    assert "ERR" in out, out


def test_attaching_again_reuses_the_same_network_and_proxy(attachment):
    """The gateway is per connector, not per operation."""
    gateway = PodmanEgressGateway()
    again = gateway.attach(
        connector_key=CONNECTOR, policy=EgressPolicy(hosts=(ALLOWED_HOST,))
    )
    assert again.network == attachment.network
    assert again.proxy_url == attachment.proxy_url


def test_changing_the_allowlist_replaces_the_proxy(attachment):
    """A stale allowlist must never linger after policy changes."""
    gateway = PodmanEgressGateway()
    changed = gateway.attach(
        connector_key=CONNECTOR, policy=EgressPolicy(hosts=(DENIED_HOST,))
    )
    assert changed.network == attachment.network
    # What was denied a moment ago is now the only permitted host.
    out = _fetch(f"https://{DENIED_HOST}/bank", changed, use_proxy=True)
    assert "Tunnel connection failed" not in out, out
    # ...and what was allowed is now refused.
    blocked = _fetch(f"https://{ALLOWED_HOST}", changed, use_proxy=True)
    assert "STATUS 200" not in blocked, blocked
