"""Celery task for periodic OLT running-config backup.

Connects to each active OLT over SSH to retrieve the full running
configuration and stores it as a timestamped text file.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.models.network import OltConfigBackup, OltConfigBackupType, OLTDevice
from app.services import backup_alerts
from app.services.db_session_adapter import db_session_adapter

if TYPE_CHECKING:
    from app.services.network.olt_protocol_adapters import OltConnectionConfig

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/app/uploads/olt_config_backups")


@dataclass(frozen=True)
class _BackupTarget:
    """One OLT's detached values, safe to use with no transaction open.

    ``connection`` carries every attribute the SSH chain reads; ``serial``
    is the only additional value this task needs, and it is for the file
    header rather than for connecting.
    """

    connection: OltConnectionConfig
    serial: str | None

    @property
    def name(self) -> str:
        return self.connection.name

    @property
    def mgmt_ip(self) -> str | None:
        return self.connection.mgmt_ip


class _ConfigFetchError(RuntimeError):
    """A selected OLT did not yield a usable running configuration."""


class OltBackupFailure(TypedDict):
    olt: str
    mgmt_ip: str | None
    error: str


class OltBackupResult(TypedDict):
    backed_up: int
    errors: int
    skipped: int
    cleaned: int
    error_details: list[OltBackupFailure]
    status: Literal["completed", "partial", "failed", "skipped"]
    total_targets: int
    unprocessed: int
    timed_out: bool


def _load_backup_targets() -> list[_BackupTarget]:
    """Project the active OLTs into detached values and end the read.

    The session is closed before any SSH runs. Holding it open across the
    fleet's serial SSH is what exceeded ``idle_in_transaction_session_timeout``
    and lost the whole run's writes after the devices had already answered.
    """
    from sqlalchemy import select

    from app.services.network.olt_protocol_adapters import OltConnectionConfig

    db = db_session_adapter.create_session()
    try:
        olts = list(
            db.scalars(select(OLTDevice).where(OLTDevice.is_active.is_(True))).all()
        )
        return [
            _BackupTarget(
                connection=OltConnectionConfig.from_model(olt),
                serial=olt.serial_number,
            )
            for olt in olts
        ]
    finally:
        db.close()


def _fetch_running_config_via_ssh(target: _BackupTarget) -> str:
    """Fetch full running configuration from an OLT via SSH.

    Uses `display current-configuration` which returns the complete config
    (Tconts, GEM ports, service-ports, VLANs, interfaces, etc.).

    Takes detached values, not an ORM entity, so it cannot be called with a
    transaction open behind it.

    Returns usable configuration or raises a typed fetch failure. Soft task
    time limits propagate so the caller can persist already-fetched backups.
    """
    connection = target.connection
    try:
        from app.services.network.olt_protocol_adapters import (
            get_protocol_adapter_from_config,
        )

        result = get_protocol_adapter_from_config(connection).fetch_running_config()
        raw_config_text = result.data.get("config_text") if result.success else ""
        config_text = raw_config_text if isinstance(raw_config_text, str) else ""
        if result.success and config_text.strip():
            # Add metadata header
            header = (
                f"# OLT Full Running Config: {connection.name}\n"
                f"# IP: {connection.mgmt_ip}\n"
                f"# Vendor: {connection.vendor or 'unknown'}\n"
                f"# Model: {connection.model or 'unknown'}\n"
                f"# Serial: {target.serial or 'unknown'}\n"
                f"# Method: SSH (display current-configuration)\n"
                f"# Captured: {datetime.now(UTC).isoformat()}\n"
                f"#\n"
            )
            return header + config_text + "\n"
        reason = str(result.message or "Could not fetch running configuration")
        if result.success:
            reason = "Device returned an empty running configuration"
        raise _ConfigFetchError(reason)
    except (SoftTimeLimitExceeded, _ConfigFetchError):
        raise
    except Exception as exc:
        raise _ConfigFetchError(str(exc) or type(exc).__name__) from exc


def _cleanup_old_backups(db, max_age_days: int = 90, max_per_olt: int = 50) -> int:
    """Remove old backups beyond retention limits.

    Deletes backups older than max_age_days AND keeps at most max_per_olt
    backups per OLT (newest retained).
    """
    from sqlalchemy import select

    cleaned = 0
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)

    # 1. Delete by age
    old_backups = list(
        db.scalars(
            select(OltConfigBackup).where(OltConfigBackup.created_at < cutoff)
        ).all()
    )
    for backup in old_backups:
        try:
            filepath = BACKUP_DIR / backup.file_path
            if filepath.exists():
                filepath.unlink()
        except OSError as e:
            logger.warning("Failed to delete backup file %s: %s", backup.file_path, e)
        db.delete(backup)
        cleaned += 1

    # 2. Per-OLT cap: keep only the newest max_per_olt backups
    olt_ids = list(db.scalars(select(OltConfigBackup.olt_device_id).distinct()).all())
    for olt_id in olt_ids:
        backups = list(
            db.scalars(
                select(OltConfigBackup)
                .where(OltConfigBackup.olt_device_id == olt_id)
                .order_by(OltConfigBackup.created_at.desc())
                .offset(max_per_olt)
            ).all()
        )
        for backup in backups:
            try:
                filepath = BACKUP_DIR / backup.file_path
                if filepath.exists():
                    filepath.unlink()
            except OSError as e:
                logger.warning(
                    "Failed to delete backup file %s: %s", backup.file_path, e
                )
            db.delete(backup)
            cleaned += 1

    if cleaned:
        db.commit()
        logger.info("Cleaned up %d old OLT config backups", cleaned)

    return cleaned


@celery_app.task(
    name="app.tasks.olt_config_backup.backup_all_olts",
    # Serial SSH across the whole fleet can exceed the global 840/900s default
    # and be hard-killed mid-run. Give it generous-but-bounded limits and commit
    # whatever finished when the soft limit hits (handled below).
    soft_time_limit=3000,
    time_limit=3300,
)
def backup_all_olts() -> OltBackupResult:
    """Backup running config for all active OLTs."""
    logger.info("Starting OLT config backup run")
    backed_up = 0
    errors = 0
    skipped = 0
    cleaned = 0
    error_details: list[OltBackupFailure] = []
    timed_out = False

    # Phase 1 — read, then end the transaction.
    targets = _load_backup_targets()

    # Phase 2 — fleet SSH with no session bound. Failures are collected rather
    # than recorded, so nothing here needs a transaction.
    fetched: list[tuple[_BackupTarget, str]] = []
    failures: list[tuple[_BackupTarget, str]] = []
    try:
        for target in targets:
            try:
                config_text = _fetch_running_config_via_ssh(target)
            except SoftTimeLimitExceeded:
                raise
            except Exception as e:
                logger.error("Failed to fetch backup for OLT %s: %s", target.name, e)
                errors += 1
                error_details.append(
                    {"olt": target.name, "mgmt_ip": target.mgmt_ip, "error": str(e)}
                )
                failures.append((target, str(e)))
                continue
            fetched.append((target, config_text))
    except SoftTimeLimitExceeded:
        # Out of time mid-fleet — persist what was already fetched rather than
        # losing the run. Retention cleanup is skipped until the next run.
        timed_out = True
        logger.warning(
            "olt_config_backup soft time limit hit during SSH; persisting %d fetched "
            "backup(s)",
            len(fetched),
        )

    # Phase 3 — persist in a fresh, short transaction.
    db = db_session_adapter.create_session()
    try:
        for target, reason in failures:
            backup_alerts.queue_backup_failure_notification(
                db,
                device_kind="olt",
                device_name=target.name,
                device_ip=target.mgmt_ip,
                error_message=reason,
                run_type="scheduled",
            )

        for target, config_text in fetched:
            try:
                # Write to file
                timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
                safe_name = target.name.replace(" ", "_").replace("/", "_")[:60]
                filename = f"{safe_name}_{timestamp}.txt"
                olt_dir = BACKUP_DIR / str(target.connection.id)
                olt_dir.mkdir(parents=True, exist_ok=True)
                filepath = olt_dir / filename
                filepath.write_text(config_text)

                # Record in DB with integrity hash
                config_bytes = config_text.encode()
                file_hash = hashlib.sha256(config_bytes).hexdigest()
                backup = OltConfigBackup(
                    id=uuid.uuid4(),
                    olt_device_id=target.connection.id,
                    backup_type=OltConfigBackupType.auto,
                    file_path=str(filepath.relative_to(BACKUP_DIR)),
                    file_size_bytes=len(config_bytes),
                    file_hash=file_hash,
                )
                db.add(backup)
                backed_up += 1

            except SoftTimeLimitExceeded:
                raise
            except Exception as e:
                logger.error("Failed to save backup for OLT %s: %s", target.name, e)
                errors += 1
                error_details.append(
                    {"olt": target.name, "mgmt_ip": target.mgmt_ip, "error": str(e)}
                )
                backup_alerts.queue_backup_failure_notification(
                    db,
                    device_kind="olt",
                    device_name=target.name,
                    device_ip=target.mgmt_ip,
                    error_message=str(e),
                    run_type="scheduled",
                )

        db.commit()

        if not timed_out:
            # Retention cleanup: remove backups older than configured age
            cleaned = _cleanup_old_backups(db, max_age_days=90, max_per_olt=50)

    except SoftTimeLimitExceeded:
        timed_out = True
        logger.warning(
            "olt_config_backup soft time limit hit; committing %d backups", backed_up
        )
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    status: Literal["completed", "partial", "failed", "skipped"]
    if errors or timed_out:
        status = "partial" if backed_up else "failed"
    else:
        status = "completed" if targets else "skipped"
    unprocessed = max(0, len(targets) - backed_up - errors)
    level = (
        logging.ERROR
        if status == "failed"
        else logging.WARNING
        if status == "partial"
        else logging.INFO
    )
    logger.log(
        level,
        "OLT config backup complete: backed_up=%d, errors=%d, skipped=%d, cleaned=%d",
        backed_up,
        errors,
        skipped,
        cleaned,
        extra={
            "backup_status": status,
            "total_targets": len(targets),
            "unprocessed": unprocessed,
            "timed_out": timed_out,
        },
    )
    return {
        "backed_up": backed_up,
        "errors": errors,
        "skipped": skipped,
        "cleaned": cleaned,
        "error_details": error_details,
        "status": status,
        "total_targets": len(targets),
        "unprocessed": unprocessed,
        "timed_out": timed_out,
    }
