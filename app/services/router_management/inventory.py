from __future__ import annotations

import builtins
import logging
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.router_management import (
    JumpHost,
    Router,
    RouterAccessMethod,
    RouterInterface,
    RouterStatus,
)
from app.schemas.router_management import (
    JumpHostCreate,
    JumpHostUpdate,
    RouterCreate,
    RouterUpdate,
)
from app.services.common import apply_ordering, apply_pagination
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)

# Both are encrypted at rest. Reads go through decrypt_credential(), which
# passes legacy plaintext values through unchanged, so existing rows keep
# working until their next write re-encrypts them.
ROUTER_CREDENTIAL_FIELDS = ("rest_api_username", "rest_api_password")
JUMP_HOST_CREDENTIAL_FIELDS = ("ssh_key", "ssh_password")


def _sync_router_monitoring_best_effort(db: Session, router: Router) -> None:
    """Refresh the router's NetworkDevice mirror without blocking inventory writes."""
    try:
        from app.services.monitoring_metrics import sync_router_to_monitoring

        sync_router_to_monitoring(db, str(router.id))
        db.commit()
        db.refresh(router)
    except Exception:
        db.rollback()
        logger.warning(
            "router_monitoring_sync_failed",
            extra={"router_id": str(router.id), "router_name": router.name},
            exc_info=True,
        )


class RouterInventory(ListResponseMixin):
    ALLOWED_ORDER_COLUMNS = {
        "name": Router.name,
        "hostname": Router.hostname,
        "management_ip": Router.management_ip,
        "status": Router.status,
        "created_at": Router.created_at,
    }

    @staticmethod
    def create(db: Session, payload: RouterCreate) -> Router:
        data = payload.model_dump(exclude_unset=True)

        for field in ROUTER_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])

        if data.get("access_method"):
            data["access_method"] = RouterAccessMethod(data["access_method"])

        if data.get("jump_host_id"):
            jh = db.get(JumpHost, data["jump_host_id"])
            if not jh:
                raise HTTPException(status_code=404, detail="Jump host not found")

        router = Router(**data)
        try:
            db.add(router)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Router with name '{payload.name}' already exists",
            )
        db.refresh(router)
        _sync_router_monitoring_best_effort(db, router)
        logger.info("Router created: %s (%s)", router.name, router.id)
        return router

    @staticmethod
    def get(db: Session, router_id: uuid.UUID) -> Router:
        router = db.execute(
            select(Router).where(Router.id == router_id, Router.is_active.is_(True))
        ).scalar_one_or_none()
        if not router:
            raise HTTPException(status_code=404, detail="Router not found")
        return router

    @staticmethod
    def list(
        db: Session,
        status: str | None = None,
        access_method: str | None = None,
        jump_host_id: uuid.UUID | None = None,
        search: str | None = None,
        order_by: str = "name",
        order_dir: str = "asc",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Router]:
        query = select(Router).where(Router.is_active.is_(True))

        if status:
            query = query.where(Router.status == RouterStatus(status))
        if access_method:
            query = query.where(
                Router.access_method == RouterAccessMethod(access_method)
            )
        if jump_host_id:
            query = query.where(Router.jump_host_id == jump_host_id)
        if search:
            pattern = f"%{search}%"
            query = query.where(
                Router.name.ilike(pattern)
                | Router.hostname.ilike(pattern)
                | Router.management_ip.ilike(pattern)
                | Router.location.ilike(pattern)
            )

        query = apply_ordering(
            query, order_by, order_dir, RouterInventory.ALLOWED_ORDER_COLUMNS
        )
        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def count(
        db: Session,
        status: str | None = None,
        search: str | None = None,
    ) -> int:
        query = select(func.count(Router.id)).where(Router.is_active.is_(True))
        if status:
            query = query.where(Router.status == RouterStatus(status))
        if search:
            pattern = f"%{search}%"
            query = query.where(
                Router.name.ilike(pattern)
                | Router.hostname.ilike(pattern)
                | Router.management_ip.ilike(pattern)
                | Router.location.ilike(pattern)
            )
        return db.execute(query).scalar_one()

    @staticmethod
    def update(db: Session, router_id: uuid.UUID, payload: RouterUpdate) -> Router:
        router = RouterInventory.get(db, router_id)
        data = payload.model_dump(exclude_unset=True)

        for field in ROUTER_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])

        if "access_method" in data and data["access_method"]:
            data["access_method"] = RouterAccessMethod(data["access_method"])
        if "status" in data and data["status"]:
            data["status"] = RouterStatus(data["status"])

        for key, value in data.items():
            setattr(router, key, value)

        db.commit()
        db.refresh(router)
        _sync_router_monitoring_best_effort(db, router)
        logger.info("Router updated: %s (%s)", router.name, router.id)
        return router

    @staticmethod
    def delete(db: Session, router_id: uuid.UUID) -> None:
        router = RouterInventory.get(db, router_id)
        router.is_active = False
        if router.network_device_id:
            from app.models.network_monitoring import NetworkDevice
            from app.services.network_monitoring import set_network_device_active

            device = db.get(NetworkDevice, router.network_device_id)
            if device:
                # Cross-domain: router inventory is an authoritative input to
                # the linked monitoring device's admission, but it does not get
                # to write the flag. It requests the transition from the owner
                # (network.monitoring_inventory) so the reachability-cache decay
                # happens here exactly as it does for a manual soft-delete.
                set_network_device_active(
                    db, device, False, reason="router_delete_cascade"
                )
        db.commit()
        logger.info("Router soft-deleted: %s (%s)", router.name, router.id)

    @staticmethod
    def sync_system_info(db: Session, router: Router) -> None:
        """Fetch /system/resource and /system/routerboard then persist to the router row."""
        from app.services.router_management.connection import RouterConnectionService

        sys_data = RouterConnectionService.require_dict_response(
            RouterConnectionService.execute(router, "GET", "/system/resource"),
            "/system/resource",
        )
        rb_data = RouterConnectionService.require_dict_response(
            RouterConnectionService.execute(router, "GET", "/system/routerboard"),
            "/system/routerboard",
        )

        router.routeros_version = sys_data.get("version")
        router.board_name = sys_data.get("board-name") or rb_data.get("model")
        router.architecture = sys_data.get("architecture-name")
        router.serial_number = rb_data.get("serial-number")
        router.firmware_type = rb_data.get("firmware-type")
        router.status = RouterStatus.online
        router.last_seen_at = datetime.now(UTC)
        db.commit()
        logger.info("System info synced for router %s (%s)", router.name, router.id)

    @staticmethod
    def sync_interfaces(db: Session, router: Router) -> None:
        """Fetch /interface and upsert into the database."""
        from app.services.router_management.connection import RouterConnectionService

        iface_data = RouterConnectionService.require_list_response(
            RouterConnectionService.execute(router, "GET", "/interface"),
            "/interface",
        )
        interfaces = [
            {
                "name": i.get("name", ""),
                "type": i.get("type", "ether"),
                "mac_address": i.get("mac-address"),
                "is_running": i.get("running", "false") == "true",
                "is_disabled": i.get("disabled", "false") == "true",
                "rx_byte": int(i.get("rx-byte", 0)),
                "tx_byte": int(i.get("tx-byte", 0)),
                "rx_packet": int(i.get("rx-packet", 0)),
                "tx_packet": int(i.get("tx-packet", 0)),
                "last_link_up_time": i.get("last-link-up-time"),
                "speed": i.get("actual-mtu"),
                "comment": i.get("comment"),
            }
            for i in iface_data
        ]
        RouterInventory.upsert_interfaces(db, router, interfaces)
        logger.info("Interfaces synced for router %s (%s)", router.name, router.id)

    @staticmethod
    def list_interfaces(
        db: Session, router_id: uuid.UUID
    ) -> builtins.list[RouterInterface]:
        """Return all interfaces for the given router, ordered by name."""
        query = (
            select(RouterInterface)
            .where(RouterInterface.router_id == router_id)
            .order_by(RouterInterface.name)
        )
        return builtins.list(db.execute(query).scalars().all())

    @staticmethod
    def upsert_interfaces(
        db: Session, router: Router, interfaces_data: builtins.list[dict]
    ) -> builtins.list[RouterInterface]:
        now = datetime.now(UTC)
        existing = {
            iface.name: iface
            for iface in db.execute(
                select(RouterInterface).where(RouterInterface.router_id == router.id)
            )
            .scalars()
            .all()
        }

        seen_names: set[str] = set()
        results: list[RouterInterface] = []

        for data in interfaces_data:
            name = data.get("name", "")
            seen_names.add(name)

            if name in existing:
                iface = existing[name]
                for key, value in data.items():
                    if key != "name":
                        setattr(iface, key, value)
                iface.synced_at = now
            else:
                iface = RouterInterface(router_id=router.id, synced_at=now, **data)
                db.add(iface)
            results.append(iface)

        for name, iface in existing.items():
            if name not in seen_names:
                db.delete(iface)

        db.commit()
        return results


class JumpHostInventory:
    @staticmethod
    def create(db: Session, payload: JumpHostCreate) -> JumpHost:
        data = payload.model_dump(exclude_unset=True)
        for field in JUMP_HOST_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])

        jh = JumpHost(**data)
        try:
            db.add(jh)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Jump host with name '{payload.name}' already exists",
            )
        db.refresh(jh)
        logger.info("Jump host created: %s (%s)", jh.name, jh.id)
        return jh

    @staticmethod
    def get(db: Session, jh_id: uuid.UUID) -> JumpHost:
        jh = db.execute(
            select(JumpHost).where(JumpHost.id == jh_id, JumpHost.is_active.is_(True))
        ).scalar_one_or_none()
        if not jh:
            raise HTTPException(status_code=404, detail="Jump host not found")
        return jh

    @staticmethod
    def list(db: Session, limit: int = 50, offset: int = 0) -> list[JumpHost]:
        query = (
            select(JumpHost)
            .where(JumpHost.is_active.is_(True))
            .order_by(JumpHost.name)
            .limit(limit)
            .offset(offset)
        )
        return list(db.execute(query).scalars().all())

    @staticmethod
    def update(db: Session, jh_id: uuid.UUID, payload: JumpHostUpdate) -> JumpHost:
        jh = JumpHostInventory.get(db, jh_id)
        data = payload.model_dump(exclude_unset=True)
        for field in JUMP_HOST_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])

        for key, value in data.items():
            setattr(jh, key, value)

        db.commit()
        db.refresh(jh)
        logger.info("Jump host updated: %s (%s)", jh.name, jh.id)
        return jh

    @staticmethod
    def delete(db: Session, jh_id: uuid.UUID) -> None:
        jh = JumpHostInventory.get(db, jh_id)
        jh.is_active = False
        db.commit()
        logger.info("Jump host soft-deleted: %s (%s)", jh.name, jh.id)

    @staticmethod
    def test_connection(db: Session, jh_id: uuid.UUID) -> dict:
        """Open a test SSH tunnel to the jump host and immediately close it."""
        from sshtunnel import SSHTunnelForwarder

        jh = JumpHostInventory.get(db, jh_id)
        try:
            ssh_key = decrypt_credential(jh.ssh_key)
            ssh_password = decrypt_credential(jh.ssh_password)
            kwargs: dict = {"ssh_username": jh.username}
            if ssh_key:
                kwargs["ssh_pkey"] = ssh_key
            elif ssh_password:
                kwargs["ssh_password"] = ssh_password

            tunnel = SSHTunnelForwarder(
                (jh.hostname, jh.port),
                remote_bind_address=("127.0.0.1", 22),
                **kwargs,
            )
            tunnel.start()
            tunnel.stop()
            logger.info("Jump host connection test succeeded: %s (%s)", jh.name, jh.id)
            return {"success": True, "message": "SSH connection successful"}
        except Exception as exc:
            logger.warning(
                "Jump host connection test failed: %s (%s): %s", jh.name, jh.id, exc
            )
            return {"success": False, "message": str(exc)}
