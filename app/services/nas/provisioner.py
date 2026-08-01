"""NAS device provisioning execution engine."""

import contextlib
import logging
import time
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services.nas import local_secret_policy


class _SshSession:
    """Thin wrapper around an open paramiko SSHClient.

    Created via ``DeviceProvisioner.ssh_session(device)``. Holds the
    connection open across multiple ``execute()`` calls so callers don't
    pay TCP+KEX+auth on every command.
    """

    def __init__(self, client) -> None:
        self._client = client

    def execute(self, command: str, *, timeout_seconds: int = 60) -> str:
        stdin, stdout, stderr = self._client.exec_command(  # nosec B601
            command, timeout=timeout_seconds
        )
        output: str = stdout.read().decode()
        error: str = stderr.read().decode()
        if error and not output:
            raise Exception(f"SSH error: {error}")
        return output or error


from app.models.catalog import (
    ConfigBackupMethod,
    ConnectionType,
    ExecutionMethod,
    NasConfigBackup,
    NasDevice,
    ProvisioningAction,
    ProvisioningLog,
    ProvisioningLogStatus,
)
from app.schemas.catalog import NasConfigBackupCreate, ProvisioningLogCreate
from app.services.credential_crypto import decrypt_credential
from app.services.nas._helpers import _emit_nas_event, _redact_sensitive

logger = logging.getLogger(__name__)


def _provision_extra(
    *,
    device: NasDevice,
    action: ProvisioningAction,
    triggered_by: str,
    connection_type: ConnectionType | None = None,
    execution_method: ExecutionMethod | str | None = None,
    template_id: object | None = None,
    log_id: object | None = None,
    execution_time_ms: int | None = None,
    error: str | None = None,
) -> dict[str, object]:
    extra: dict[str, object] = {
        "event": "nas_provisioning",
        "device_id": str(device.id),
        "device_name": device.name,
        "vendor": device.vendor.value if getattr(device, "vendor", None) else None,
        "action": action.value,
        "triggered_by": triggered_by,
    }
    if connection_type is not None:
        extra["connection_type"] = connection_type.value
    if execution_method is not None:
        extra["execution_method"] = (
            execution_method.value
            if isinstance(execution_method, ExecutionMethod)
            else execution_method
        )
    if template_id is not None:
        extra["template_id"] = str(template_id)
    if log_id is not None:
        extra["provisioning_log_id"] = str(log_id)
    if execution_time_ms is not None:
        extra["duration_ms"] = execution_time_ms
    if error is not None:
        extra["error"] = error
    return extra


class DeviceProvisioner:
    """
    Execute provisioning commands on NAS devices.

    Supports multiple execution methods:
    - SSH: Direct SSH command execution
    - API: REST API calls (MikroTik REST API, Huawei NCE, etc.)
    - RADIUS CoA: Change of Authorization packets
    """

    @staticmethod
    def provision_user(
        db: Session,
        nas_device_id: UUID,
        action: ProvisioningAction,
        variables: dict[str, Any],
        triggered_by: str = "system",
    ) -> ProvisioningLog:
        """
        Execute a provisioning action on a NAS device.

        Args:
            db: Database session
            nas_device_id: Target NAS device ID
            action: The provisioning action to execute
            variables: Variables to substitute in the template
            triggered_by: Who triggered this action

        Returns:
            ProvisioningLog with execution results
        """
        from app.services.nas.devices import NasDevices
        from app.services.nas.logs import ProvisioningLogs
        from app.services.nas.templates import ProvisioningTemplates

        device = NasDevices.get(db, nas_device_id)

        # Determine connection type
        connection_type = device.default_connection_type or ConnectionType.pppoe
        logger.info(
            "nas_provisioning_start",
            extra=_provision_extra(
                device=device,
                action=action,
                triggered_by=triggered_by,
                connection_type=connection_type,
            ),
        )

        # Boundary check BEFORE template lookup. Template bodies live in an
        # operator-editable database row, so a PPPoE `/ppp secret` template
        # would otherwise reintroduce the parallel authority with no code
        # change. Refusing here means the prohibition cannot be edited around.
        ruling = local_secret_policy.decide_for_provisioning_action(
            vendor=device.vendor,
            connection_type=connection_type,
            action=action,
        )
        if not ruling.emits_commands:
            logger.warning(
                "nas_provisioning_refused_local_secret",
                extra={
                    **_provision_extra(
                        device=device,
                        action=action,
                        triggered_by=triggered_by,
                        connection_type=connection_type,
                    ),
                    **ruling.as_log_extra(),
                },
            )
            raise HTTPException(status_code=409, detail=ruling.reason)

        # Find appropriate template
        template = ProvisioningTemplates.find_template(
            db, device.vendor, connection_type, action
        )

        if not template:
            raise HTTPException(
                status_code=404,
                detail=f"No provisioning template found for {device.vendor.value}/{connection_type.value}/{action.value}",
            )

        # Render the command
        command = ProvisioningTemplates.render(template, variables)

        # Post-render guard. The action label alone is not a boundary: a
        # template filed under reset_session or backup_config can still contain
        # /ppp secret text, and template bodies are operator-editable data. This
        # inspects what will actually be sent, so a benign label cannot smuggle
        # a local-secret mutation onto the device.
        try:
            local_secret_policy.assert_command_text_allowed(
                command,
                context=f"Provisioning template {template.id}",
            )
        except local_secret_policy.LocalSecretCommandRejected as rejected:
            logger.warning(
                "nas_provisioning_refused_local_secret_command_text",
                extra={
                    **_provision_extra(
                        device=device,
                        action=action,
                        triggered_by=triggered_by,
                        connection_type=connection_type,
                    ),
                    "template_id": str(template.id),
                    "rejected_matches": rejected.details.get("matches"),
                },
            )
            raise HTTPException(status_code=409, detail=rejected.message) from rejected

        # Create log entry
        log = ProvisioningLogs.create(
            db,
            ProvisioningLogCreate(
                nas_device_id=device.id,
                subscriber_id=variables.get("subscriber_id"),
                template_id=template.id,
                action=action,
                command_sent=command,
                status=ProvisioningLogStatus.running,
                triggered_by=triggered_by,
                request_data=_redact_sensitive(variables),
            ),
        )

        # Execute the command with template-defined timeout
        timeout_secs = template.timeout_seconds or 60
        start_time = time.time()
        execution_method = template.execution_method or "ssh"
        try:
            if execution_method == "ssh":
                response = DeviceProvisioner._execute_ssh(
                    device, command, timeout_seconds=timeout_secs
                )
            elif execution_method == "api":
                response = DeviceProvisioner._execute_api(
                    device, command, variables, timeout_seconds=timeout_secs
                )
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported execution method: {execution_method}",
                )

            execution_time = int((time.time() - start_time) * 1000)

            # Update log with success
            ProvisioningLogs.update_status(
                db,
                log.id,
                ProvisioningLogStatus.success,
                response=response,
                execution_time_ms=execution_time,
            )
            logger.info(
                "nas_provisioning_success",
                extra=_provision_extra(
                    device=device,
                    action=action,
                    triggered_by=triggered_by,
                    connection_type=connection_type,
                    execution_method=execution_method,
                    template_id=template.id,
                    log_id=log.id,
                    execution_time_ms=execution_time,
                ),
            )

            # Handle queue mapping for bandwidth monitoring
            DeviceProvisioner._handle_queue_mapping(db, device, action, variables)

            _emit_nas_event(
                db,
                "nas_provisioning_completed",
                {
                    "device_id": str(device.id),
                    "action": action.value,
                    "execution_time_ms": execution_time,
                },
            )

        except TimeoutError:
            execution_time = int((time.time() - start_time) * 1000)
            ProvisioningLogs.update_status(
                db,
                log.id,
                ProvisioningLogStatus.timeout,
                error=f"Command timed out after {timeout_secs}s",
                execution_time_ms=execution_time,
            )
            logger.warning(
                "nas_provisioning_timeout",
                extra=_provision_extra(
                    device=device,
                    action=action,
                    triggered_by=triggered_by,
                    connection_type=connection_type,
                    execution_method=execution_method,
                    template_id=template.id,
                    log_id=log.id,
                    execution_time_ms=execution_time,
                    error=f"Command timed out after {timeout_secs}s",
                ),
            )
            _emit_nas_event(
                db,
                "nas_provisioning_failed",
                {
                    "device_id": str(device.id),
                    "action": action.value,
                    "error": f"timeout ({timeout_secs}s)",
                },
            )
            raise
        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            ProvisioningLogs.update_status(
                db,
                log.id,
                ProvisioningLogStatus.failed,
                error=str(e),
                execution_time_ms=execution_time,
            )
            logger.error(
                "nas_provisioning_failed",
                extra=_provision_extra(
                    device=device,
                    action=action,
                    triggered_by=triggered_by,
                    connection_type=connection_type,
                    execution_method=execution_method,
                    template_id=template.id,
                    log_id=log.id,
                    execution_time_ms=execution_time,
                    error=str(e),
                ),
            )
            _emit_nas_event(
                db,
                "nas_provisioning_failed",
                {
                    "device_id": str(device.id),
                    "action": action.value,
                    "error": str(e),
                },
            )
            raise

        return ProvisioningLogs.get(db, log.id)

    @staticmethod
    def _handle_queue_mapping(
        db: Session,
        device: NasDevice,
        action: ProvisioningAction,
        variables: dict[str, Any],
    ) -> None:
        """
        Handle queue mapping creation/deactivation based on provisioning action.

        This integrates with the bandwidth monitoring system by maintaining
        the mapping between MikroTik queue names and subscriptions.
        """
        from app.services.queue_mapping import queue_mapping

        subscription_id = variables.get("subscription_id")
        if not subscription_id:
            return

        # Convert to UUID if string
        if isinstance(subscription_id, str):
            subscription_id = UUID(subscription_id)

        # Determine queue name from variables or generate from username
        queue_name = variables.get("queue_name")
        if not queue_name:
            username = variables.get("username")
            if username:
                queue_name = f"queue-{username}"
            else:
                queue_name = f"sub-{subscription_id}"

        if action == ProvisioningAction.create_user:
            # Create or update queue mapping for bandwidth monitoring
            queue_mapping.sync_from_provisioning(
                db,
                nas_device_id=device.id,
                queue_name=queue_name,
                subscription_id=subscription_id,
            )

        elif action in (
            ProvisioningAction.delete_user,
            ProvisioningAction.suspend_user,
        ):
            # Deactivate queue mappings when user is deleted or suspended
            queue_mapping.remove_subscription_mappings(db, subscription_id)

        elif action == ProvisioningAction.unsuspend_user:
            # Re-activate queue mapping when user is unsuspended
            queue_mapping.sync_from_provisioning(
                db,
                nas_device_id=device.id,
                queue_name=queue_name,
                subscription_id=subscription_id,
            )

    @staticmethod
    @contextlib.contextmanager
    def ssh_session(device: NasDevice):
        """Open one SSH session and yield it for many .execute() calls.

        Use this when a caller will run multiple commands on the same NAS
        in quick succession (firewall rule push, kick + address-list add).
        Paramiko connect is the dominant cost (~300-1000ms); chaining
        commands inside one session amortises it.
        """
        import paramiko

        if not device.management_ip and not device.ip_address:
            raise HTTPException(status_code=400, detail="Device has no management IP")
        if not device.ssh_username:
            raise HTTPException(status_code=400, detail="Device has no SSH credentials")

        host = device.management_ip or device.ip_address
        port = device.management_port or 120
        if host is None:
            raise HTTPException(
                status_code=400, detail="Device has no management IP or address"
            )

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if device.ssh_verify_host_key is False:

            class _AcceptUnknownHostKeyPolicy:
                def missing_host_key(self, client, hostname, key) -> None:
                    logger.warning(
                        "Accepting unknown SSH host key for %s because host-key verification is disabled",
                        hostname,
                    )
                    client._host_keys.add(hostname, key.get_name(), key)

            client.set_missing_host_key_policy(_AcceptUnknownHostKeyPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        try:
            if device.ssh_key:
                import io

                decrypted_key = decrypt_credential(device.ssh_key)
                key = paramiko.RSAKey.from_private_key(io.StringIO(decrypted_key))
                client.connect(
                    host, port=port, username=device.ssh_username, pkey=key, timeout=30
                )
            else:
                decrypted_password = decrypt_credential(device.ssh_password)
                client.connect(
                    host,
                    port=port,
                    username=device.ssh_username,
                    password=decrypted_password,
                    timeout=30,
                )
            yield _SshSession(client)
        finally:
            client.close()

    @staticmethod
    def _execute_ssh(device: NasDevice, command: str, timeout_seconds: int = 60) -> str:
        """Execute one command via SSH. For multiple commands on the same
        device, prefer ``ssh_session()`` to amortise the connect cost."""
        with DeviceProvisioner.ssh_session(device) as session:
            return session.execute(command, timeout_seconds=timeout_seconds)

    @staticmethod
    def _execute_api(
        device: NasDevice, command: str, variables: dict, timeout_seconds: int = 30
    ) -> str:
        """Execute command via REST API."""
        import requests

        if not device.api_url:
            raise HTTPException(
                status_code=400, detail="Device has no API URL configured"
            )

        # Build authentication - decrypt credentials before use
        auth = None
        headers = {}

        if device.api_token:
            decrypted_token = decrypt_credential(device.api_token)
            headers["Authorization"] = f"Bearer {decrypted_token}"
        elif device.api_username and device.api_password:
            decrypted_password = decrypt_credential(device.api_password)
            auth = (device.api_username, decrypted_password)

        # For MikroTik REST API, the command is the API path
        url = f"{device.api_url.rstrip('/')}/{command.lstrip('/')}"

        verify_tls = (
            device.api_verify_tls if device.api_verify_tls is not None else False
        )
        response = requests.post(
            url,
            json=variables,
            auth=auth,
            headers=headers,
            timeout=timeout_seconds,
            verify=verify_tls,
        )

        response.raise_for_status()
        return str(response.text)

    @staticmethod
    def backup_config(
        db: Session, nas_device_id: UUID, triggered_by: str = "system"
    ) -> NasConfigBackup:
        """
        Backup configuration from a NAS device.

        Args:
            db: Database session
            nas_device_id: Target NAS device ID
            triggered_by: Who triggered this backup

        Returns:
            NasConfigBackup with the configuration content
        """
        from app.services.nas.backups import NasConfigBackups
        from app.services.nas.devices import NasDevices

        device = NasDevices.get(db, nas_device_id)

        # Determine backup method
        backup_method = device.backup_method or ConfigBackupMethod.ssh

        if backup_method == ConfigBackupMethod.ssh:
            config_content = DeviceProvisioner._backup_via_ssh(device)
        elif backup_method == ConfigBackupMethod.api:
            config_content = DeviceProvisioner._backup_via_api(device)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Backup method {backup_method.value} not implemented",
            )

        # Determine config format based on vendor via adapter
        from app.services.nas.vendor_adapter import get_config_format

        config_format = get_config_format(device)

        # Create backup record
        backup = NasConfigBackups.create(
            db,
            NasConfigBackupCreate(
                nas_device_id=device.id,
                config_content=config_content,
                config_format=config_format,
                backup_method=backup_method,
                is_scheduled=False,
                is_manual=True,
            ),
        )

        _emit_nas_event(
            db,
            "nas_backup_completed",
            {
                "device_id": str(device.id),
                "device_name": device.name,
                "backup_id": str(backup.id),
            },
        )

        return backup

    @staticmethod
    def restore_config(
        db: Session,
        nas_device_id: UUID,
        backup_id: UUID,
        triggered_by: str = "system",
    ) -> ProvisioningLog:
        """Restore a configuration backup to a NAS device.

        Pushes the backup's config_content to the device via SSH or API,
        using vendor-specific import commands.

        Args:
            db: Database session.
            nas_device_id: Target NAS device ID.
            backup_id: Configuration backup ID to restore.
            triggered_by: Who triggered the restore.

        Returns:
            ProvisioningLog with execution results.
        """
        from app.services.nas.backups import NasConfigBackups
        from app.services.nas.devices import NasDevices
        from app.services.nas.logs import ProvisioningLogs

        device = NasDevices.get(db, nas_device_id)
        backup = NasConfigBackups.get(db, backup_id)

        if str(backup.nas_device_id) != str(device.id):
            raise HTTPException(
                status_code=400, detail="Backup does not belong to this device"
            )

        if not backup.config_content:
            raise HTTPException(
                status_code=400, detail="Backup has no configuration content"
            )

        # Get vendor-specific import command via adapter
        from app.services.nas.vendor_adapter import get_nas_vendor_adapter

        adapter = get_nas_vendor_adapter(device)
        vendor_cmd = adapter.get_restore_command(backup.config_content)
        command = vendor_cmd.command

        log = ProvisioningLogs.create(
            db,
            ProvisioningLogCreate(
                nas_device_id=device.id,
                action=ProvisioningAction.restore_config,
                command_sent=f"[restore backup {backup.id}]",
                status=ProvisioningLogStatus.running,
                triggered_by=triggered_by,
            ),
        )

        start_time = time.time()
        try:
            response = DeviceProvisioner._execute_ssh(device, command)
            execution_time = int((time.time() - start_time) * 1000)
            ProvisioningLogs.update_status(
                db,
                log.id,
                ProvisioningLogStatus.success,
                response=response,
                execution_time_ms=execution_time,
            )
        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            ProvisioningLogs.update_status(
                db,
                log.id,
                ProvisioningLogStatus.failed,
                error=str(e),
                execution_time_ms=execution_time,
            )
            raise

        return ProvisioningLogs.get(db, log.id)

    @staticmethod
    def _backup_via_ssh(device: NasDevice) -> str:
        """Backup configuration via SSH."""
        from app.services.nas.vendor_adapter import get_nas_vendor_adapter

        adapter = get_nas_vendor_adapter(device)
        vendor_cmd = adapter.get_backup_command()
        return DeviceProvisioner._execute_ssh(
            device, vendor_cmd.command, timeout_seconds=vendor_cmd.timeout_seconds
        )

    @staticmethod
    def _backup_via_api(device: NasDevice) -> str:
        """Backup configuration via REST API."""
        from app.services.nas.vendor_adapter import get_nas_vendor_adapter, supports_api

        if not supports_api(device):
            raise HTTPException(
                status_code=400,
                detail=f"API backup not supported for vendor {device.vendor.value}",
            )

        adapter = get_nas_vendor_adapter(device)
        if adapter.vendor_name == "mikrotik":
            # MikroTik REST API export endpoint
            return DeviceProvisioner._execute_api(device, "/rest/export", {})
        else:
            raise HTTPException(
                status_code=400,
                detail=f"API backup not implemented for vendor {device.vendor.value}",
            )
