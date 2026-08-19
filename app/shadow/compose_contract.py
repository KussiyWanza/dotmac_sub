"""Typed contract over the shadow Compose file.

The shadow stack's safety properties are all statements about a YAML file, and a
YAML file is the easiest thing in this programme to edit in a hurry on a host at
2am. So the file is parsed into closed models and checked, rather than grepped.

Two design choices carry most of the weight:

**Parsing is typed and closed.** `extra="forbid"` means a Compose key nobody
modelled here is a loud parse failure rather than an unchecked passenger. If
someone adds `devices:` or `userns_mode:` to a service, this refuses to parse
until the key is modelled and a decision recorded about it. Fail-closed is the
correct default when the question is "what else did this file grant?".

**Forbidden keys are modelled, not omitted.** `privileged`, `pid`, `cap_add` and
friends are declared fields precisely so a file containing them parses and is
then *rejected with a reason*. Leaving them out would make a dangerous file fail
with a confusing schema error, and would tempt a future editor to relax
`extra="forbid"` to make the error go away.

`contract_violations` returns every violation rather than raising on the first,
because a reviewer fixing a hardening regression wants the whole list.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict

from app.shadow.identity import ArtifactIdentityError, pinned_reference

#: The compose project. Distinct from `keycloak` (the co-tenant on the target
#: host) and from Sub's production project, so `docker compose down` in one can
#: never reach the other.
SHADOW_PROJECT: Final[str] = "dotmac-sub-thin-shadow"

#: The only loopback address the app may publish on, and the only port.
SHADOW_BIND_HOST: Final[str] = "127.0.0.1"
SHADOW_BIND_PORT: Final[int] = 18001

#: The two networks, and the different way each denies egress.
#:
#: `shadow_internal` is `internal: true` — no route off the host at all — and
#: everything that holds state lives only there. `shadow_edge` cannot be
#: internal, because Docker silently ignores port publishing for a container
#: whose every network is internal, so a loopback bind declared on such a
#: container is accepted and then never listens. The edge instead denies egress
#: by disabling IP masquerade: a packet leaving the container keeps its RFC1918
#: source and is unroutable past this host, while inbound DNAT still works.
SHADOW_INTERNAL_NETWORK: Final[str] = "shadow_internal"
SHADOW_EDGE_NETWORK: Final[str] = "shadow_edge"

#: The only DNS forwarder a service reaching the edge may use: the container's
#: own loopback, where nothing listens. Docker's embedded resolver still answers
#: service names; only names it must forward externally fail.
SHADOW_DNS: Final[tuple[str, ...]] = ("127.0.0.1",)

#: Driver options the edge bridge must set, and the only values accepted.
EDGE_DRIVER_OPTS: Final[dict[str, str]] = {
    "com.docker.network.bridge.enable_ip_masquerade": "false",
    "com.docker.network.bridge.host_binding_ipv4": SHADOW_BIND_HOST,
}

#: Exact image digests this stack is pinned to. A digest not on this list is
#: refused even if it is syntactically pinned: "pinned" and "pinned to the
#: artifact we reviewed" are different claims.
PINNED_IMAGES: Final[dict[str, str]] = {
    "app": "sha256:342a9b805d6ac9d56a116b9ac833c594b0ccd5f2f79a9b6c75b3f85ba36885d4",
    "migrate": "sha256:342a9b805d6ac9d56a116b9ac833c594b0ccd5f2f79a9b6c75b3f85ba36885d4",
    "postgres": "sha256:681931a625df344215e9b8998bf34daf146b6a395ceacee4439eb9c85869239f",
    "redis": "sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2",
}

#: Hostnames a shadow service may talk to. Anything else in a connection string
#: is a live endpoint by definition — there is nothing else on this network.
INTERNAL_HOSTS: Final[frozenset[str]] = frozenset(
    {"postgres", "redis", "127.0.0.1", "localhost"}
)

#: Environment names that must never carry a value here. Credentials for real
#: providers and an OpenBao token are the two ways a disposable environment
#: acquires the power to cause a real consequence.
FORBIDDEN_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {
        "OPENBAO_TOKEN",
        "OPENBAO_ROLE_ID",
        "OPENBAO_SECRET_ID",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "MONO_SECRET_KEY",
        "PAYSTACK_SECRET_KEY",
        "FLUTTERWAVE_SECRET_KEY",
        "STRIPE_SECRET_KEY",
        "SMTP_PASSWORD",
        "TWILIO_AUTH_TOKEN",
        "WHATSAPP_TOKEN",
        "SENTRY_DSN",
        "GLITCHTIP_DSN",
    }
)

#: Service-name fragments that must not appear. Workers and Beat are excluded
#: "initially" by the handoff, and a public route would undo the loopback bind.
FORBIDDEN_SERVICE_FRAGMENTS: Final[tuple[str, ...]] = (
    "celery",
    "worker",
    "beat",
    "nginx",
    "traefik",
    "caddy",
)

#: Names owned by the co-tenant Keycloak stack on the target host.
KEYCLOAK_RESERVED: Final[tuple[str, ...]] = ("keycloak",)


class DependsCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    condition: str


class HealthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    test: tuple[str, ...]
    interval: str | None = None
    timeout: str | None = None
    retries: int | None = None
    start_period: str | None = None


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    driver: str
    options: dict[str, str] = {}


class ShadowService(BaseModel):
    """One compose service. Unknown keys are refused; dangerous keys are named."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str
    container_name: str | None = None
    restart: str | None = None
    command: tuple[str, ...] | None = None
    environment: dict[str, str] = {}
    ports: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    depends_on: dict[str, DependsCondition] = {}
    healthcheck: HealthCheck | None = None
    security_opt: tuple[str, ...] = ()
    logging: LoggingConfig | None = None
    mem_limit: str | None = None
    mem_reservation: str | None = None
    cpus: float | None = None
    pids_limit: int | None = None
    dns: tuple[str, ...] = ()

    # Modelled so a file that uses them parses and is then rejected by name.
    privileged: bool | None = None
    pid: str | None = None
    ipc: str | None = None
    network_mode: str | None = None
    userns_mode: str | None = None
    cap_add: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    sysctls: dict[str, str] = {}
    extra_hosts: tuple[str, ...] = ()
    build: str | None = None
    env_file: tuple[str, ...] = ()


class ShadowNetwork(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    driver: str | None = None
    internal: bool = False
    external: bool = False
    driver_opts: dict[str, str] = {}


class ShadowVolume(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    external: bool = False


class ShadowComposeFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    services: dict[str, ShadowService]
    networks: dict[str, ShadowNetwork]
    volumes: dict[str, ShadowVolume]


def parse_compose(text: str) -> ShadowComposeFile:
    """Parse shadow compose YAML into the closed model.

    Top-level `x-` extension keys are dropped: they are anchor holders that
    Compose itself ignores, and their content has already been merged into the
    services by the YAML parser, so checking them again would double-count.
    """
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError("shadow compose file must be a mapping")
    cleaned = {key: value for key, value in raw.items() if not key.startswith("x-")}
    return ShadowComposeFile.model_validate(cleaned)


def _host_of(connection_string: str) -> str | None:
    """Hostname in a URL-shaped connection string, or None if there is none."""
    try:
        parsed = urlsplit(connection_string)
    except ValueError:
        return None
    return parsed.hostname


def contract_violations(compose: ShadowComposeFile) -> tuple[str, ...]:
    """Every way this file breaks the shadow contract. Empty means compliant."""
    problems: list[str] = []

    if compose.name != SHADOW_PROJECT:
        problems.append(
            f"compose project is {compose.name!r}, must be {SHADOW_PROJECT!r}"
        )
    if any(reserved in compose.name.lower() for reserved in KEYCLOAK_RESERVED):
        problems.append(f"compose project {compose.name!r} reuses a Keycloak name")

    declared_internal = {
        key for key, network in compose.networks.items() if network.internal
    }
    if set(compose.networks) != {SHADOW_INTERNAL_NETWORK, SHADOW_EDGE_NETWORK}:
        problems.append(
            f"networks are {sorted(compose.networks)}, expected exactly "
            f"[{SHADOW_EDGE_NETWORK!r}, {SHADOW_INTERNAL_NETWORK!r}]"
        )
    internal_net = compose.networks.get(SHADOW_INTERNAL_NETWORK)
    if internal_net is not None and not internal_net.internal:
        problems.append(
            f"{SHADOW_INTERNAL_NETWORK!r} is not internal: everything holding "
            "state must have no route off the host"
        )
    edge = compose.networks.get(SHADOW_EDGE_NETWORK)
    if edge is not None:
        if edge.internal:
            problems.append(
                f"{SHADOW_EDGE_NETWORK!r} is internal, so the loopback publish "
                "on it would be silently ignored by Docker"
            )
        for option, expected in EDGE_DRIVER_OPTS.items():
            if edge.driver_opts.get(option) != expected:
                problems.append(
                    f"{SHADOW_EDGE_NETWORK!r} must set {option}={expected!r} "
                    f"(got {edge.driver_opts.get(option)!r}); without it the "
                    "edge bridge would give the shadow app real egress"
                )
    for key, network in compose.networks.items():
        if network.external:
            problems.append(f"network {key!r} is external: it may be a shared network")
        if any(r in network.name.lower() for r in KEYCLOAK_RESERVED):
            problems.append(f"network {network.name!r} reuses a Keycloak name")
        if not network.name.startswith(SHADOW_PROJECT):
            problems.append(
                f"network {network.name!r} is not namespaced under {SHADOW_PROJECT!r}"
            )

    for key, volume in compose.volumes.items():
        if volume.external:
            problems.append(
                f"volume {key!r} is external: a shadow run must not adopt a "
                "volume it did not create"
            )
        if any(r in volume.name.lower() for r in KEYCLOAK_RESERVED):
            problems.append(f"volume {volume.name!r} reuses a Keycloak name")
        if not volume.name.startswith(SHADOW_PROJECT):
            problems.append(
                f"volume {volume.name!r} is not namespaced under {SHADOW_PROJECT!r}"
            )

    for name, service in compose.services.items():
        problems.extend(
            _service_violations(name, service, set(compose.networks), declared_internal)
        )

    if "migrate" not in compose.services:
        problems.append("no one-shot migrate service")
    else:
        migrate = compose.services["migrate"]
        if str(migrate.restart).lower() not in {"no", "none"}:
            problems.append(
                f"migrate service has restart={migrate.restart!r}: a one-shot "
                "migration must not restart, or a broken migration hides in a "
                "crash loop"
            )
        app = compose.services.get("app")
        if app is not None:
            condition = app.depends_on.get("migrate")
            if (
                condition is None
                or condition.condition != "service_completed_successfully"
            ):
                problems.append(
                    "app does not wait for migrate to complete successfully"
                )

    return tuple(problems)


def _service_violations(
    name: str,
    service: ShadowService,
    declared_networks: set[str],
    internal_networks: set[str],
) -> list[str]:
    problems: list[str] = []

    if any(fragment in name.lower() for fragment in FORBIDDEN_SERVICE_FRAGMENTS):
        problems.append(
            f"service {name!r} is a worker, scheduler or public router; the "
            "shadow stack runs neither initially"
        )

    # Image identity.
    try:
        pinned_reference(service.image)
    except ArtifactIdentityError as exc:
        problems.append(f"service {name!r} image is not digest-pinned: {exc}")
    else:
        expected = PINNED_IMAGES.get(name)
        if expected is None:
            problems.append(f"service {name!r} is not in the pinned image allowlist")
        elif not service.image.endswith(f"@{expected}"):
            problems.append(
                f"service {name!r} pins {service.image.rsplit('@', 1)[-1]}, "
                f"expected {expected}"
            )

    if service.build is not None:
        problems.append(
            f"service {name!r} declares build:; images are pulled, not built"
        )

    # Host privilege.
    if service.privileged:
        problems.append(f"service {name!r} is privileged")
    if service.pid is not None:
        problems.append(
            f"service {name!r} sets pid={service.pid!r} (host PID namespace)"
        )
    if service.ipc is not None:
        problems.append(f"service {name!r} sets ipc={service.ipc!r}")
    if service.userns_mode is not None:
        problems.append(f"service {name!r} sets userns_mode={service.userns_mode!r}")
    if service.network_mode is not None:
        problems.append(
            f"service {name!r} sets network_mode={service.network_mode!r}: it must "
            "join the internal network, not the host"
        )
    if service.cap_add:
        problems.append(f"service {name!r} adds capabilities {list(service.cap_add)}")
    if service.devices:
        problems.append(f"service {name!r} maps devices {list(service.devices)}")
    if service.sysctls:
        problems.append(f"service {name!r} sets sysctls {sorted(service.sysctls)}")
    if service.extra_hosts:
        problems.append(
            f"service {name!r} sets extra_hosts {list(service.extra_hosts)}"
        )
    if "no-new-privileges:true" not in service.security_opt:
        problems.append(f"service {name!r} does not set no-new-privileges")

    # Storage: named volumes only.
    for mount in service.volumes:
        source = mount.split(":", 1)[0]
        if source.startswith("/") or source.startswith("."):
            problems.append(
                f"service {name!r} bind-mounts host path {source!r}: the shadow "
                "stack must not read or write the host filesystem"
            )
        if "docker.sock" in mount:
            problems.append(f"service {name!r} mounts the Docker socket")
        if "wireguard" in mount.lower():
            problems.append(f"service {name!r} mounts WireGuard configuration")

    # Networking.
    for network in service.networks:
        if network not in declared_networks:
            problems.append(f"service {name!r} joins undeclared network {network!r}")
    if SHADOW_INTERNAL_NETWORK not in service.networks:
        problems.append(f"service {name!r} does not join {SHADOW_INTERNAL_NETWORK!r}")

    # The rule this file learned the hard way: Docker ACCEPTS a `ports:` entry
    # on a container whose every network is `internal: true`, then never
    # publishes it. Nothing errors, `docker ps` shows the mapping as empty, and
    # the bind looks configured right up until something tries to connect. A
    # declaration-only check cannot see that, so the check is on membership.
    # A service that can reach the edge must not be able to resolve a name
    # off-host: TCP egress is already denied, but a DNS query that still leaves
    # is an exfiltration channel for anything willing to encode data in it.
    on_edge = SHADOW_EDGE_NETWORK in service.networks
    if service.image.startswith("ghcr.io/michaelayoade/dotmac_sub@") and (
        service.dns != SHADOW_DNS
    ):
        problems.append(
            f"service {name!r} sets dns={list(service.dns)}, expected "
            f"{list(SHADOW_DNS)}: external name resolution must fail, or egress "
            "denial has a hole a query can walk through"
        )
    if service.ports and not on_edge:
        problems.append(
            f"service {name!r} publishes {list(service.ports)} but joins only "
            f"internal networks — Docker will ignore the publish and the port "
            f"will never listen; join {SHADOW_EDGE_NETWORK!r} as well"
        )
    if on_edge and not service.ports:
        problems.append(
            f"service {name!r} joins {SHADOW_EDGE_NETWORK!r} without publishing "
            "anything; only the published service belongs there"
        )
    if (
        not service.ports
        and service.networks
        and set(service.networks) - internal_networks
    ):
        problems.append(
            f"service {name!r} holds state and must be internal-only, but joins "
            f"{sorted(set(service.networks) - internal_networks)}"
        )
    for published in service.ports:
        problems.extend(_port_violations(name, published))

    # Environment.
    for key, value in service.environment.items():
        if key in FORBIDDEN_ENV_KEYS and value.strip():
            problems.append(
                f"service {name!r} sets {key}: a disposable environment must not "
                "hold credentials that can cause a real consequence"
            )
        if "://" in value:
            host = _host_of(value)
            if host is not None and host not in INTERNAL_HOSTS:
                problems.append(
                    f"service {name!r} points {key} at {host!r}, which is not an "
                    "internal shadow service — that is a live endpoint or a real "
                    "delivery path"
                )
    if service.env_file:
        problems.append(
            f"service {name!r} declares env_file {list(service.env_file)}: the "
            "shadow stack passes only the variables it names, so an operator's "
            "production .env cannot leak in wholesale"
        )

    return problems


def _port_violations(name: str, published: str) -> list[str]:
    parts = published.split(":")
    if len(parts) != 3:
        return [
            f"service {name!r} publishes {published!r} without an explicit bind "
            "address: Compose would bind all interfaces"
        ]
    host, host_port, _container_port = parts
    problems: list[str] = []
    if host != SHADOW_BIND_HOST:
        problems.append(
            f"service {name!r} publishes on {host!r}, not {SHADOW_BIND_HOST!r}: a "
            "shadow subscriber system must not be reachable off-host"
        )
    if host_port != str(SHADOW_BIND_PORT):
        problems.append(
            f"service {name!r} publishes host port {host_port}, expected "
            f"{SHADOW_BIND_PORT}"
        )
    return problems


__all__ = [
    "FORBIDDEN_ENV_KEYS",
    "FORBIDDEN_SERVICE_FRAGMENTS",
    "INTERNAL_HOSTS",
    "KEYCLOAK_RESERVED",
    "PINNED_IMAGES",
    "EDGE_DRIVER_OPTS",
    "SHADOW_BIND_HOST",
    "SHADOW_BIND_PORT",
    "SHADOW_DNS",
    "SHADOW_EDGE_NETWORK",
    "SHADOW_INTERNAL_NETWORK",
    "SHADOW_PROJECT",
    "DependsCondition",
    "HealthCheck",
    "LoggingConfig",
    "ShadowComposeFile",
    "ShadowNetwork",
    "ShadowService",
    "ShadowVolume",
    "contract_violations",
    "parse_compose",
]
