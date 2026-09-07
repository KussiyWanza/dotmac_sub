from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

import httpx
from sshtunnel import SSHTunnelForwarder

from app.models.router_management import JumpHost, Router
from app.schemas.router_management import ConnectionTestResult
from app.services.credential_crypto import decrypt_credential

if TYPE_CHECKING:
    from app.services.topology.lldp_contracts import LldpJumpHost, LldpRouter

logger = logging.getLogger(__name__)

DANGEROUS_COMMANDS = [
    "/system/reset-configuration",
    "/system/shutdown",
    "/system/reboot",
    "/disk/format-drive",
    "/file/remove",
    "/user/remove",
    "/user/set",
    "/ip/service/set",
    "/interface/disable",
    "/interface/remove",
    "/routing/bgp/remove",
    "/routing/bgp/set",
    "/routing/ospf/remove",
    "/routing/ospf/set",
    "/ip/route/remove",
    "/ip/route/set",
    "/ipv6/route/remove",
    "/ipv6/route/set",
    "/ip/firewall/filter/remove",
    "/ip/firewall/filter/set",
    "/ipv6/firewall/filter/remove",
    "/ipv6/firewall/filter/set",
    "/ip/firewall/nat/remove",
    "/ip/firewall/nat/set",
]

# Fallback defaults — the live values come from SettingDomain.network via
# _rest_tunables() so operators can tune them for WAN/high-latency plant without
# a code change. These remain the defaults (and the safety net if settings/DB
# are unavailable).
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0
RouterResponse = dict | list | str


class RouterTransportError(RuntimeError):
    """Router transport failed with an outcome that may be ambiguous for writes."""


def _rest_tunables() -> tuple[float, float, int, float]:
    """Resolve (connect_timeout, read_timeout, max_retries, backoff_base) from
    network settings, falling back to the module defaults. Best-effort: a
    settings/DB hiccup must never break the router connection layer."""
    ct, rt, mr, bb = CONNECT_TIMEOUT, READ_TIMEOUT, MAX_RETRIES, RETRY_BACKOFF_BASE
    try:
        from app.db import SessionLocal
        from app.models.domain_settings import SettingDomain
        from app.services import settings_spec

        with SessionLocal() as session:

            def _num(key: str, fallback: float) -> float:
                value = settings_spec.resolve_value(session, SettingDomain.network, key)
                return float(value) if value is not None else fallback

            ct = _num("router_rest_connect_timeout_seconds", ct)
            rt = _num("router_rest_read_timeout_seconds", rt)
            mr = max(1, int(_num("router_rest_max_retries", mr)))
            bb = _num("router_rest_retry_backoff_base", bb)
    except Exception:
        logger.debug("router REST tunables: using defaults", exc_info=True)
    return ct, rt, mr, bb


def check_dangerous_commands(commands: list[str]) -> None:
    for cmd in commands:
        cmd_lower = cmd.lower().strip()
        for dangerous in DANGEROUS_COMMANDS:
            if cmd_lower.startswith(dangerous):
                raise ValueError(
                    f"Dangerous command blocked: {cmd}. "
                    f"Commands matching {dangerous} are not allowed."
                )


class RouterConnectionService:
    _tunnels: dict[str, SSHTunnelForwarder] = {}
    _lock = threading.Lock()

    @staticmethod
    def _build_base_url(management_ip: str, port: int, use_ssl: bool) -> str:
        scheme = "https" if use_ssl else "http"
        return f"{scheme}://{management_ip}:{port}"

    @classmethod
    def _get_or_create_tunnel(
        cls, router: Router | LldpRouter, jump_host: JumpHost | LldpJumpHost
    ) -> SSHTunnelForwarder:
        with cls._lock:
            tunnel_key = f"{jump_host.id}:{router.management_ip}:{router.rest_api_port}"

            if tunnel_key in cls._tunnels:
                tunnel = cls._tunnels[tunnel_key]
                if tunnel.is_active:
                    return tunnel
                del cls._tunnels[tunnel_key]

            ssh_key = decrypt_credential(jump_host.ssh_key)
            ssh_password = decrypt_credential(jump_host.ssh_password)

            kwargs: dict = {
                "ssh_username": jump_host.username,
                "remote_bind_address": (
                    router.management_ip,
                    router.rest_api_port,
                ),
            }
            if ssh_key:
                kwargs["ssh_pkey"] = ssh_key
            elif ssh_password:
                kwargs["ssh_password"] = ssh_password

            tunnel = SSHTunnelForwarder(
                (jump_host.hostname, jump_host.port),
                **kwargs,
            )
            tunnel.start()
            cls._tunnels[tunnel_key] = tunnel
            logger.info(
                "SSH tunnel opened: %s:%d -> localhost:%d via %s",
                router.management_ip,
                router.rest_api_port,
                tunnel.local_bind_port,
                jump_host.hostname,
            )
            return tunnel

    @classmethod
    def cleanup_idle_tunnels(cls) -> int:
        with cls._lock:
            closed = 0
            dead_keys = []
            for key, tunnel in cls._tunnels.items():
                if not tunnel.is_active:
                    dead_keys.append(key)
                    closed += 1
                    continue
            for key in dead_keys:
                try:
                    cls._tunnels[key].stop()
                except Exception:
                    logger.debug(
                        "Failed to stop dead tunnel during cleanup", exc_info=True
                    )
                del cls._tunnels[key]
            return closed

    @classmethod
    def close_all_tunnels(cls) -> None:
        with cls._lock:
            for tunnel in cls._tunnels.values():
                try:
                    tunnel.stop()
                except Exception:
                    logger.debug("Failed to stop tunnel during shutdown", exc_info=True)
            cls._tunnels.clear()

    @classmethod
    def get_client(
        cls, router: Router | LldpRouter, *, timeout: httpx.Timeout | None = None
    ) -> httpx.Client:
        username = (
            decrypt_credential(router.rest_api_username) or router.rest_api_username
        )
        password = (
            decrypt_credential(router.rest_api_password) or router.rest_api_password
        )

        if router.access_method.value == "jump_host" and router.jump_host:
            tunnel = cls._get_or_create_tunnel(router, router.jump_host)
            base_url = cls._build_base_url(
                "127.0.0.1", tunnel.local_bind_port, router.use_ssl
            )
        else:
            base_url = cls._build_base_url(
                router.management_ip, router.rest_api_port, router.use_ssl
            )

        return httpx.Client(
            base_url=base_url,
            auth=(username, password),
            verify=router.verify_tls,
            timeout=timeout or httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
        )

    @classmethod
    def execute_once(
        cls,
        router: Router | LldpRouter,
        *,
        method: str,
        path: str,
        payload: dict | None = None,
        connect_timeout: float,
        read_timeout: float,
    ) -> RouterResponse:
        """Issue one REST request without resolving database-backed retry settings."""
        timeout = httpx.Timeout(connect_timeout, read=read_timeout)
        with cls.get_client(router, timeout=timeout) as client:
            response = client.request(
                method=method,
                url=f"/rest{path}",
                json=payload,
            )
            response.raise_for_status()
            if not response.text:
                return {}
            content_type = response.headers.get("content-type", "")
            if "json" in content_type.lower():
                try:
                    return response.json()
                except ValueError:
                    return response.text
            return response.text

    @classmethod
    def execute(
        cls,
        router: Router,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> RouterResponse:
        """Issue one REST call to the router.

        ``connect_timeout``/``read_timeout``/``max_retries`` override the
        settings-resolved tunables for callers with a different latency budget
        (e.g. the hourly LLDP discovery poll fails fast with a single attempt
        while config snapshots keep the patient defaults). ``None`` keeps the
        existing settings/default behavior for current callers.
        """
        last_error: Exception | None = None
        d_connect, d_read, d_retries, backoff_base = _rest_tunables()
        if connect_timeout is None:
            connect_timeout = d_connect
        if read_timeout is None:
            read_timeout = d_read
        max_retries = d_retries if max_retries is None else max(1, max_retries)
        timeout = httpx.Timeout(connect_timeout, read=read_timeout)

        for attempt in range(max_retries):
            try:
                with cls.get_client(router, timeout=timeout) as client:
                    response = client.request(
                        method=method,
                        url=f"/rest{path}",
                        json=payload,
                    )
                    response.raise_for_status()
                    if not response.text:
                        return {}
                    content_type = response.headers.get("content-type", "")
                    if "json" in content_type.lower():
                        try:
                            return response.json()
                        except ValueError:
                            return response.text
                    # RouterOS ``/export`` (and similar) return plain text, not
                    # JSON — returning ``.json()`` unconditionally would raise an
                    # uncaught JSONDecodeError on every config snapshot.
                    return response.text
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    wait = backoff_base**attempt
                    logger.warning(
                        "Router %s attempt %d failed: %s. Retrying in %.1fs",
                        router.name,
                        attempt + 1,
                        str(exc),
                        wait,
                    )
                    time.sleep(wait)
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Router {router.name} returned {exc.response.status_code}: "
                    f"{exc.response.text[:200]}"
                ) from exc

        raise RouterTransportError(
            f"Router {router.name} unreachable after {max_retries} attempts: {last_error}"
        )

    @staticmethod
    def require_dict_response(response: RouterResponse, path: str) -> dict:
        if not isinstance(response, dict):
            raise RuntimeError(
                f"RouterOS endpoint {path} returned {type(response).__name__}; expected object"
            )
        return response

    @staticmethod
    def require_list_response(response: RouterResponse, path: str) -> list:
        if not isinstance(response, list):
            raise RuntimeError(
                f"RouterOS endpoint {path} returned {type(response).__name__}; expected list"
            )
        return response

    @classmethod
    def execute_batch(
        cls, router: Router, commands: list[dict]
    ) -> list[RouterResponse]:
        results = []
        for cmd in commands:
            result = cls.execute(
                router,
                method=cmd.get("method", "POST"),
                path=cmd["path"],
                payload=cmd.get("payload"),
            )
            results.append(result)
        return results

    @classmethod
    def test_connection(cls, router: Router) -> ConnectionTestResult:
        start = time.time()
        try:
            data = cls.require_dict_response(
                cls.execute(router, "GET", "/system/resource"),
                "/system/resource",
            )
            elapsed_ms = int((time.time() - start) * 1000)
            version = data.get("version", "unknown")
            return ConnectionTestResult(
                success=True,
                message=f"Connected. RouterOS {version}",
                response_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.time() - start) * 1000)
            return ConnectionTestResult(
                success=False,
                message=str(exc),
                response_time_ms=elapsed_ms,
            )
