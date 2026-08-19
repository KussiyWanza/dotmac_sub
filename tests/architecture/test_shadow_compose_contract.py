"""The shadow stack may not acquire power it has no business having.

Every test here comes in a pair: the shipped file is compliant, and the detector
that proves it *bites* when the property is removed. A hardening test that only
ever sees a hardened file is indistinguishable from a hardening test that has
been silently broken — it passes either way. So each check mutates the parsed
model to construct the exact violation and asserts the detector names it.

Mutation is done with `model_copy(update=...)`, which deliberately skips
validation: the point is to build a file that *should not exist* and prove the
contract rejects it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.shadow.compose_contract import (
    EDGE_DRIVER_OPTS,
    PINNED_IMAGES,
    SHADOW_BIND_HOST,
    SHADOW_BIND_PORT,
    SHADOW_DNS,
    SHADOW_PROJECT,
    ShadowComposeFile,
    ShadowNetwork,
    ShadowService,
    ShadowVolume,
    contract_violations,
    parse_compose,
)

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "deploy/shadow/docker-compose.shadow.yml"


@pytest.fixture(scope="module")
def compose() -> ShadowComposeFile:
    return parse_compose(COMPOSE_PATH.read_text(encoding="utf-8"))


def _with_service(
    compose: ShadowComposeFile, name: str, **changes: object
) -> ShadowComposeFile:
    services = dict(compose.services)
    services[name] = services[name].model_copy(update=changes)
    return compose.model_copy(update={"services": services})


def _violations_for(compose: ShadowComposeFile, name: str, **changes: object) -> str:
    return " | ".join(contract_violations(_with_service(compose, name, **changes)))


# ── The shipped file ────────────────────────────────────────────────────────


def test_the_shipped_shadow_compose_file_is_compliant(
    compose: ShadowComposeFile,
) -> None:
    assert contract_violations(compose) == ()


def test_the_shadow_stack_runs_exactly_four_services(
    compose: ShadowComposeFile,
) -> None:
    assert set(compose.services) == {"app", "migrate", "postgres", "redis"}


def test_the_project_is_not_the_production_or_keycloak_project(
    compose: ShadowComposeFile,
) -> None:
    assert compose.name == SHADOW_PROJECT
    assert "keycloak" not in compose.name


def test_every_network_and_volume_is_namespaced_to_the_shadow_project(
    compose: ShadowComposeFile,
) -> None:
    for network in compose.networks.values():
        assert network.name.startswith(SHADOW_PROJECT)
    for volume in compose.volumes.values():
        assert volume.name.startswith(SHADOW_PROJECT)


# ── Exact image digests ─────────────────────────────────────────────────────


def test_every_image_is_pinned_to_its_exact_reviewed_digest(
    compose: ShadowComposeFile,
) -> None:
    for name, service in compose.services.items():
        assert service.image.endswith(f"@{PINNED_IMAGES[name]}"), (
            f"{name} does not pin the reviewed digest"
        )


def test_the_baseline_app_and_migrate_run_the_same_image(
    compose: ShadowComposeFile,
) -> None:
    """A migration applied by different bytes than the app runs is not a rehearsal."""
    assert compose.services["app"].image == compose.services["migrate"].image


def test_a_mutable_tag_is_refused(compose: ShadowComposeFile) -> None:
    problems = _violations_for(
        compose, "app", image="ghcr.io/michaelayoade/dotmac_sub:latest"
    )
    assert "not digest-pinned" in problems


def test_a_correctly_shaped_but_unreviewed_digest_is_refused(
    compose: ShadowComposeFile,
) -> None:
    """Pinned is not the same claim as pinned to what we reviewed."""
    problems = _violations_for(
        compose, "app", image="ghcr.io/michaelayoade/dotmac_sub@sha256:" + "0" * 64
    )
    assert "expected" in problems


# ── Host privilege ──────────────────────────────────────────────────────────


def test_privileged_mode_is_refused(compose: ShadowComposeFile) -> None:
    assert "privileged" in _violations_for(compose, "app", privileged=True)


def test_host_pid_namespace_is_refused(compose: ShadowComposeFile) -> None:
    assert "host PID" in _violations_for(compose, "app", pid="host")


def test_host_network_mode_is_refused(compose: ShadowComposeFile) -> None:
    assert "network_mode" in _violations_for(compose, "app", network_mode="host")


def test_added_capabilities_are_refused(compose: ShadowComposeFile) -> None:
    assert "capabilities" in _violations_for(compose, "app", cap_add=("NET_ADMIN",))


def test_device_maps_and_sysctls_are_refused(compose: ShadowComposeFile) -> None:
    assert "devices" in _violations_for(compose, "app", devices=("/dev/net/tun",))
    assert "sysctls" in _violations_for(
        compose, "app", sysctls={"net.ipv4.ip_forward": "1"}
    )


def test_no_new_privileges_is_required(compose: ShadowComposeFile) -> None:
    assert "no-new-privileges" in _violations_for(compose, "app", security_opt=())


def test_the_production_stacks_host_privileges_are_all_absent(
    compose: ShadowComposeFile,
) -> None:
    """Production Sub is privileged, pid: host and NET_ADMIN. Shadow is none of it."""
    for name, service in compose.services.items():
        assert not service.privileged, name
        assert service.pid is None, name
        assert service.network_mode is None, name
        assert service.cap_add == (), name
        assert service.devices == (), name


# ── Filesystem ──────────────────────────────────────────────────────────────


def test_host_bind_mounts_are_refused(compose: ShadowComposeFile) -> None:
    problems = _violations_for(
        compose, "app", volumes=("/etc/wireguard:/etc/wireguard",)
    )
    assert "bind-mounts host path" in problems


def test_the_docker_socket_is_refused(compose: ShadowComposeFile) -> None:
    problems = _violations_for(
        compose, "app", volumes=("/var/run/docker.sock:/var/run/docker.sock:ro",)
    )
    assert "Docker socket" in problems


def test_every_shipped_mount_is_a_named_volume(compose: ShadowComposeFile) -> None:
    for service in compose.services.values():
        for mount in service.volumes:
            assert not mount.startswith(("/", "."))


# ── Network exposure and egress ─────────────────────────────────────────────


def test_the_app_publishes_only_on_loopback(compose: ShadowComposeFile) -> None:
    assert compose.services["app"].ports == (
        f"{SHADOW_BIND_HOST}:{SHADOW_BIND_PORT}:8001",
    )
    assert compose.services["postgres"].ports == ()
    assert compose.services["redis"].ports == ()


def test_a_public_port_binding_is_refused(compose: ShadowComposeFile) -> None:
    assert "not '127.0.0.1'" in _violations_for(
        compose, "app", ports=("0.0.0.0:18001:8001",)
    )


def test_a_port_with_no_bind_address_is_refused(compose: ShadowComposeFile) -> None:
    """`- "18001:8001"` binds every interface, which is the easy mistake."""
    assert "without an explicit bind address" in _violations_for(
        compose, "app", ports=("18001:8001",)
    )


def test_state_holding_services_are_on_the_internal_network_only(
    compose: ShadowComposeFile,
) -> None:
    assert compose.networks["shadow_internal"].internal
    for name in ("postgres", "redis", "migrate"):
        assert compose.services[name].networks == ("shadow_internal",), name


def test_the_internal_network_losing_its_internal_flag_is_refused(
    compose: ShadowComposeFile,
) -> None:
    networks = dict(compose.networks)
    networks["shadow_internal"] = networks["shadow_internal"].model_copy(
        update={"internal": False}
    )
    problems = contract_violations(compose.model_copy(update={"networks": networks}))
    assert any("not internal" in problem for problem in problems)


def test_the_edge_network_denies_egress_by_disabling_masquerade(
    compose: ShadowComposeFile,
) -> None:
    """It cannot be `internal` — a publish would be ignored — so it denies
    egress by leaving container packets with an unroutable RFC1918 source."""
    edge = compose.networks["shadow_edge"]
    assert edge.internal is False
    assert edge.driver_opts == EDGE_DRIVER_OPTS


@pytest.mark.parametrize("option", sorted(EDGE_DRIVER_OPTS))
def test_dropping_an_edge_driver_option_is_refused(
    compose: ShadowComposeFile, option: str
) -> None:
    opts = dict(compose.networks["shadow_edge"].driver_opts)
    opts.pop(option)
    networks = dict(compose.networks)
    networks["shadow_edge"] = networks["shadow_edge"].model_copy(
        update={"driver_opts": opts}
    )
    problems = contract_violations(compose.model_copy(update={"networks": networks}))
    assert any(option in problem for problem in problems)


def test_enabling_masquerade_on_the_edge_is_refused(
    compose: ShadowComposeFile,
) -> None:
    opts = dict(compose.networks["shadow_edge"].driver_opts)
    opts["com.docker.network.bridge.enable_ip_masquerade"] = "true"
    networks = dict(compose.networks)
    networks["shadow_edge"] = networks["shadow_edge"].model_copy(
        update={"driver_opts": opts}
    )
    problems = contract_violations(compose.model_copy(update={"networks": networks}))
    assert any("real egress" in problem for problem in problems)


def test_publishing_from_an_internal_only_service_is_refused(
    compose: ShadowComposeFile,
) -> None:
    """The bug this rule exists for.

    Docker accepts `ports:` on a container whose every network is internal and
    then never publishes it: no error, an empty mapping in `docker ps`, and a
    bind that looks configured until something tries to connect. The shipped
    file had exactly this shape and passed a declaration-only check.
    """
    problems = _violations_for(compose, "app", networks=("shadow_internal",))
    assert "Docker will ignore the publish" in problems


def test_the_app_actually_joins_the_edge_network(
    compose: ShadowComposeFile,
) -> None:
    """Sensitivity's companion: the shipped file has the shape that works."""
    assert set(compose.services["app"].networks) == {"shadow_internal", "shadow_edge"}


def test_a_state_service_joining_the_edge_network_is_refused(
    compose: ShadowComposeFile,
) -> None:
    problems = _violations_for(
        compose, "postgres", networks=("shadow_internal", "shadow_edge")
    )
    assert "without publishing" in problems or "internal-only" in problems


def test_an_undeclared_network_is_refused(compose: ShadowComposeFile) -> None:
    assert "undeclared network" in _violations_for(
        compose, "app", networks=("shadow_internal", "shadow_elsewhere")
    )


def test_an_external_network_or_volume_is_refused(
    compose: ShadowComposeFile,
) -> None:
    networks = dict(compose.networks)
    networks["borrowed"] = ShadowNetwork(
        name=f"{SHADOW_PROJECT}_borrowed", internal=True, external=True
    )
    problems = contract_violations(compose.model_copy(update={"networks": networks}))
    assert any("external" in problem for problem in problems)

    volumes = dict(compose.volumes)
    volumes["borrowed"] = ShadowVolume(name=f"{SHADOW_PROJECT}_borrowed", external=True)
    problems = contract_violations(compose.model_copy(update={"volumes": volumes}))
    assert any("external" in problem for problem in problems)


def test_reusing_a_keycloak_network_or_volume_is_refused(
    compose: ShadowComposeFile,
) -> None:
    """Keycloak co-tenants the target host and must not be touched."""
    networks = dict(compose.networks)
    networks["kc"] = ShadowNetwork(name="keycloak_default", internal=True)
    problems = contract_violations(compose.model_copy(update={"networks": networks}))
    assert any("Keycloak" in problem for problem in problems)

    volumes = dict(compose.volumes)
    volumes["kc"] = ShadowVolume(name="keycloak_keycloak_db")
    problems = contract_violations(compose.model_copy(update={"volumes": volumes}))
    assert any("Keycloak" in problem for problem in problems)


def test_reusing_the_keycloak_project_name_is_refused(
    compose: ShadowComposeFile,
) -> None:
    problems = contract_violations(compose.model_copy(update={"name": "keycloak"}))
    assert any("Keycloak" in problem for problem in problems)


# ── Credentials and live endpoints ──────────────────────────────────────────


def test_an_openbao_token_is_refused(compose: ShadowComposeFile) -> None:
    environment = dict(compose.services["app"].environment)
    environment["OPENBAO_TOKEN"] = "s.abcdef"  # noqa: S105 - constructed violation
    assert "OPENBAO_TOKEN" in _violations_for(compose, "app", environment=environment)


def test_provider_credentials_are_refused(compose: ShadowComposeFile) -> None:
    for key in ("S3_SECRET_KEY", "PAYSTACK_SECRET_KEY", "TWILIO_AUTH_TOKEN"):
        environment = dict(compose.services["app"].environment)
        environment[key] = "value"
        assert key in _violations_for(compose, "app", environment=environment)


def test_the_shipped_file_declares_no_credential_variables(
    compose: ShadowComposeFile,
) -> None:
    """Sensitivity's companion: the guard above is checked against real data too."""
    from app.shadow.compose_contract import FORBIDDEN_ENV_KEYS

    for name, service in compose.services.items():
        for key, value in service.environment.items():
            assert not (key in FORBIDDEN_ENV_KEYS and value.strip()), f"{name}.{key}"


def test_a_live_database_endpoint_is_refused(compose: ShadowComposeFile) -> None:
    environment = dict(compose.services["app"].environment)
    environment["DATABASE_URL"] = (
        "postgresql+psycopg://sub:pw@db-primary.dotmac.local:5432/dotmac_sub"
    )
    problems = _violations_for(compose, "app", environment=environment)
    assert "db-primary.dotmac.local" in problems


def test_a_real_delivery_path_is_refused(compose: ShadowComposeFile) -> None:
    """No shadow output may address a real external system."""
    environment = dict(compose.services["app"].environment)
    environment["WEBHOOK_URL"] = "https://hooks.example.com/notify"
    problems = _violations_for(compose, "app", environment=environment)
    assert "hooks.example.com" in problems


def test_every_shipped_url_addresses_an_internal_shadow_service(
    compose: ShadowComposeFile,
) -> None:
    from urllib.parse import urlsplit

    from app.shadow.compose_contract import INTERNAL_HOSTS

    for name, service in compose.services.items():
        for key, value in service.environment.items():
            if "://" not in value:
                continue
            host = urlsplit(value).hostname
            assert host is None or host in INTERNAL_HOSTS, f"{name}.{key} -> {host}"


def test_an_env_file_is_refused(compose: ShadowComposeFile) -> None:
    """Production Sub uses `env_file: .env`; shadow names its variables instead."""
    assert "env_file" in _violations_for(compose, "app", env_file=(".env",))


# ── Workers, beat and public routers ────────────────────────────────────────


def test_no_worker_scheduler_or_public_router_service_is_present(
    compose: ShadowComposeFile,
) -> None:
    for name in compose.services:
        assert not any(
            fragment in name for fragment in ("celery", "worker", "beat", "nginx")
        )


@pytest.mark.parametrize(
    "service_name", ["celery-worker", "celery-beat", "nginx", "traefik"]
)
def test_adding_a_worker_beat_or_router_service_is_refused(
    compose: ShadowComposeFile, service_name: str
) -> None:
    services = dict(compose.services)
    services[service_name] = ShadowService(
        image=f"anything@sha256:{'0' * 64}",
        networks=("shadow_internal",),
        security_opt=("no-new-privileges:true",),
    )
    problems = contract_violations(compose.model_copy(update={"services": services}))
    assert any(service_name in problem for problem in problems)


# ── The one-shot migration ──────────────────────────────────────────────────


def test_the_migration_service_is_one_shot(compose: ShadowComposeFile) -> None:
    assert compose.services["migrate"].restart == "no"
    assert compose.services["migrate"].command == ("alembic", "upgrade", "head")


def test_a_restarting_migration_service_is_refused(
    compose: ShadowComposeFile,
) -> None:
    assert "one-shot migration" in _violations_for(
        compose, "migrate", restart="unless-stopped"
    )


def test_the_app_waits_for_a_successful_migration(
    compose: ShadowComposeFile,
) -> None:
    assert (
        compose.services["app"].depends_on["migrate"].condition
        == "service_completed_successfully"
    )


def test_an_app_that_does_not_wait_for_migration_is_refused(
    compose: ShadowComposeFile,
) -> None:
    depends = dict(compose.services["app"].depends_on)
    depends.pop("migrate")
    problems = _violations_for(compose, "app", depends_on=depends)
    assert "does not wait for migrate" in problems


# ── Parsing is fail-closed ──────────────────────────────────────────────────


def test_an_unmodelled_compose_key_fails_to_parse() -> None:
    """An unknown key is refused rather than passing unchecked."""
    from pydantic import ValidationError

    text = COMPOSE_PATH.read_text(encoding="utf-8").replace(
        "    container_name: dotmac_sub_thin_shadow_app",
        "    container_name: dotmac_sub_thin_shadow_app\n    userns_mode_typo: host",
    )
    with pytest.raises(ValidationError):
        parse_compose(text)


# ── External name resolution ────────────────────────────────────────────────


def test_the_sub_image_services_cannot_resolve_a_name_off_host(
    compose: ShadowComposeFile,
) -> None:
    """TCP egress denial is not enough on its own.

    The deployed stack refused every TCP connection out and still resolved
    `github.com`, because Docker's embedded resolver forwards unknown names to
    the host's resolvers regardless of masquerade. A query that leaves is a
    channel for anything willing to encode data in it, so the forwarder is
    pointed at the container's own loopback where nothing listens.
    """
    for name in ("app", "migrate"):
        assert compose.services[name].dns == SHADOW_DNS, name


def test_a_service_that_can_reach_the_edge_may_not_use_a_real_resolver(
    compose: ShadowComposeFile,
) -> None:
    assert "external name resolution must fail" in _violations_for(
        compose, "app", dns=("1.1.1.1",)
    )


def test_dropping_the_dns_pin_entirely_is_refused(
    compose: ShadowComposeFile,
) -> None:
    assert "external name resolution must fail" in _violations_for(
        compose, "app", dns=()
    )


def test_the_state_services_are_not_forced_to_pin_dns(
    compose: ShadowComposeFile,
) -> None:
    """Sensitivity: the rule is scoped to the services running the Sub image.

    postgres and redis never reach the edge network, so their resolver cannot
    leave the host anyway; requiring the pin there would be cargo-culting.
    """
    assert compose.services["postgres"].dns == ()
    assert contract_violations(compose) == ()
