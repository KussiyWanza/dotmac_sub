"""Allowlist egress gateway for external connectors.

Phase 4b of ADR 0005. Phase 4a made egress default-deny: a connector with no
declared hosts gets no network, and one that declares hosts was refused because
nothing could confine it. This supplies the confinement.

Topology, validated against rootless Podman:

- The connector runs on a per-connector ``--internal`` network. That network has
  no NAT, so the connector has no route out at the IP level — not merely no DNS.
  It keeps ``--cap-drop=ALL`` and every other Phase 3 restriction.
- A proxy container is dual-homed onto that internal network and an external
  one. It is the only path to the internet. It needs ``NET_ADMIN`` solely to
  correct its own default route, because Podman puts the default route on the
  internal network regardless of attach order. That privilege stays on our own
  trusted proxy image; the connector never receives it.
- The proxy runs tinyproxy with ``FilterDefaultDeny``, permitting ``CONNECT``
  only to the hosts the manifest declares. HTTPS is tunnelled, never
  intercepted, so the allowlist matches the CONNECT hostname and no TLS is
  terminated.

A connector that ignores its proxy environment reaches nothing, because the
enforcement is the absent route rather than the proxy variable.

Naming, argv construction, and allowlist rendering are pure functions so the
security-relevant parts are asserted without invoking Podman; the orchestration
is covered by a live test.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.services.integrations.egress_policy import EgressPolicy

PROXY_IMAGE = "localhost/dotmac-egress-proxy:latest"
PROXY_PORT = 8888
_NETWORK_PREFIX = "dm-egress-"
_PROXY_PREFIX = "dm-egress-proxy-"
_ALLOWLIST_LABEL = "io.dotmac.egress.allowlist"
_UNSAFE = re.compile(r"[^a-z0-9-]+")
# Podman 3.4.4 writes new CNI configs as cniVersion 1.0.0, but the plugins
# shipped on Ubuntu 22.04 (0.9.1) reject that version, which makes the network
# unusable ("CNI network not found"). Rewriting the version is a contained
# workaround for that distro skew; installing CNI plugins 1.x, or moving to
# netavark, removes the need for it. See the ADR deployment note.
_COMPATIBLE_CNI_VERSION = "0.4.0"


class EgressGatewayError(RuntimeError):
    """The gateway could not be established, so the connector must not run."""


@dataclass(frozen=True)
class EgressAttachment:
    """How a connector container attaches to its confined egress path."""

    network: str
    proxy_url: str

    def proxy_env(self) -> dict[str, str]:
        """Proxy variables for the connector. Not secret, but not the control.

        Confinement comes from the missing route; these merely tell a
        well-behaved client where the only exit is.
        """
        return {
            "http_proxy": self.proxy_url,
            "https_proxy": self.proxy_url,
            "HTTP_PROXY": self.proxy_url,
            "HTTPS_PROXY": self.proxy_url,
        }


class EgressGateway(Protocol):
    def attach(
        self, *, connector_key: str, policy: EgressPolicy
    ) -> EgressAttachment: ...


def network_name(connector_key: str) -> str:
    return _NETWORK_PREFIX + _UNSAFE.sub("-", connector_key.strip().lower()).strip("-")


def proxy_name(connector_key: str) -> str:
    return _PROXY_PREFIX + _UNSAFE.sub("-", connector_key.strip().lower()).strip("-")


def render_allowlist(policy: EgressPolicy) -> str:
    """The exact allowlist handed to the proxy, comma separated.

    Deterministic ordering so an unchanged policy does not look like a change
    and force the proxy to be recreated.
    """
    return ",".join(sorted(policy.hosts))


def build_proxy_argv(
    *,
    connector_key: str,
    policy: EgressPolicy,
    internal_network: str,
    external_network: str,
    external_gateway: str,
    image: str = PROXY_IMAGE,
) -> list[str]:
    """``podman run`` argv for the per-connector egress proxy.

    NET_ADMIN is the one privilege granted, and only so the proxy can repair
    its own default route. It is never granted to a connector.
    """
    allowlist = render_allowlist(policy)
    return [
        "podman",
        "run",
        "--detach",
        "--name",
        proxy_name(connector_key),
        f"--network={internal_network},{external_network}",
        "--cap-drop=ALL",
        "--cap-add=NET_ADMIN",
        "--security-opt=no-new-privileges",
        "--read-only",
        # Scratch for the rendered allowlist. Deliberately /tmp and not
        # /etc/tinyproxy: a tmpfs over the config directory would mask the
        # baked-in tinyproxy.conf.
        "--tmpfs=/tmp:rw,noexec,nosuid,size=1m",
        "--memory=64m",
        "--pids-limit=64",
        "--restart=no",
        f"--label={_ALLOWLIST_LABEL}={allowlist}",
        f"--env=EXTERNAL_GATEWAY={external_gateway}",
        f"--env=ALLOWED_HOSTS={allowlist}",
        image,
    ]


def _run(argv: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )


class PodmanEgressGateway:
    """Establish a confined egress path using rootless Podman."""

    def __init__(
        self,
        *,
        external_network: str = "podman",
        proxy_image: str = PROXY_IMAGE,
        cni_config_dir: str | None = None,
    ) -> None:
        self._external_network = external_network
        self._proxy_image = proxy_image
        self._cni_config_dir = cni_config_dir or str(
            Path.home() / ".config" / "cni" / "net.d"
        )

    # -- network ---------------------------------------------------------

    def _network_exists(self, name: str) -> bool:
        return _run(["podman", "network", "exists", name]).returncode == 0

    def _patch_cni_version(self, name: str) -> None:
        """Rewrite an unusable cniVersion written by Podman 3.4.4."""
        path = Path(self._cni_config_dir) / f"{name}.conflist"
        if not path.exists():
            return
        try:
            config = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if config.get("cniVersion") == _COMPATIBLE_CNI_VERSION:
            return
        config["cniVersion"] = _COMPATIBLE_CNI_VERSION
        path.write_text(json.dumps(config, indent=2))

    def _ensure_network(self, name: str) -> None:
        if self._network_exists(name):
            return
        created = _run(["podman", "network", "create", "--internal", name])
        if created.returncode != 0 and not self._network_exists(name):
            raise EgressGatewayError(
                f"could not create internal egress network {name}: "
                f"{created.stderr.strip()[:300]}"
            )
        self._patch_cni_version(name)

    def _external_gateway(self) -> str:
        """The external network's gateway, which the proxy routes out through.

        Parsed from JSON rather than a Go template: Podman 3.4.4 returns the raw
        CNI conflist (lower-case keys under ``plugins[].ipam.ranges``) while
        netavark returns ``subnets[]``. Reading both keeps this working across
        the network-backend change.
        """
        inspected = _run(["podman", "network", "inspect", self._external_network])
        if inspected.returncode != 0:
            raise EgressGatewayError(
                f"could not inspect external network {self._external_network}: "
                f"{inspected.stderr.strip()[:200]}"
            )
        try:
            payload = json.loads(inspected.stdout)
        except json.JSONDecodeError as exc:
            raise EgressGatewayError(
                f"external network {self._external_network} inspect was not JSON"
            ) from exc

        for entry in payload if isinstance(payload, list) else [payload]:
            if not isinstance(entry, dict):
                continue
            # netavark
            for subnet in entry.get("subnets") or []:
                gateway = (subnet or {}).get("gateway")
                if gateway:
                    return str(gateway)
            # CNI
            for plugin in entry.get("plugins") or []:
                ranges = ((plugin or {}).get("ipam") or {}).get("ranges") or []
                for group in ranges:
                    for item in group or []:
                        gateway = (item or {}).get("gateway")
                        if gateway:
                            return str(gateway)

        raise EgressGatewayError(
            f"could not determine the gateway of external network "
            f"{self._external_network}"
        )

    # -- proxy -----------------------------------------------------------

    def _proxy_state(self, name: str) -> tuple[bool, str | None]:
        """Whether the proxy runs, and the allowlist it was started with."""
        inspected = _run(
            [
                "podman",
                "inspect",
                name,
                "--format",
                "{{.State.Running}}|{{index .Config.Labels "
                f'"{_ALLOWLIST_LABEL}"'
                "}}",
            ]
        )
        if inspected.returncode != 0:
            return False, None
        running, _, allowlist = inspected.stdout.strip().partition("|")
        return running == "true", allowlist

    def _proxy_address(self, name: str, network: str) -> str:
        inspected = _run(
            [
                "podman",
                "inspect",
                name,
                "--format",
                f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
            ]
        )
        address = inspected.stdout.strip()
        if not address:
            raise EgressGatewayError(f"egress proxy {name} has no address on {network}")
        return address

    def attach(self, *, connector_key: str, policy: EgressPolicy) -> EgressAttachment:
        if not policy.requires_network:
            raise EgressGatewayError(
                "attach called for a connector that needs no network"
            )
        if not policy.hosts:
            # allow_installation_hosts without resolved hosts would mean an
            # empty allowlist, i.e. a proxy that permits nothing. Refuse rather
            # than start a gateway that cannot work.
            raise EgressGatewayError(
                f"connector {connector_key} declares installation-provided egress "
                "hosts, which the gateway cannot resolve statically"
            )

        network = network_name(connector_key)
        proxy = proxy_name(connector_key)
        self._ensure_network(network)

        wanted = render_allowlist(policy)
        running, current = self._proxy_state(proxy)
        if running and current == wanted:
            return EgressAttachment(
                network=network,
                proxy_url=f"http://{self._proxy_address(proxy, network)}:{PROXY_PORT}",
            )

        # Either absent, stopped, or carrying a stale allowlist. Replace it: an
        # allowlist change must take effect, never linger.
        _run(["podman", "rm", "--force", proxy])
        argv = build_proxy_argv(
            connector_key=connector_key,
            policy=policy,
            internal_network=network,
            external_network=self._external_network,
            external_gateway=self._external_gateway(),
            image=self._proxy_image,
        )
        started = _run(argv)
        if started.returncode != 0:
            raise EgressGatewayError(
                f"could not start egress proxy for {connector_key}: "
                f"{started.stderr.strip()[:300]}"
            )
        return EgressAttachment(
            network=network,
            proxy_url=f"http://{self._proxy_address(proxy, network)}:{PROXY_PORT}",
        )


__all__ = [
    "PROXY_IMAGE",
    "PROXY_PORT",
    "EgressAttachment",
    "EgressGateway",
    "EgressGatewayError",
    "PodmanEgressGateway",
    "build_proxy_argv",
    "network_name",
    "proxy_name",
    "render_allowlist",
]
