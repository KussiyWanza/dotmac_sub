from __future__ import annotations

import importlib
import logging
import os
import shutil
from datetime import UTC, datetime
from typing import Any

try:
    _resource: Any | None = importlib.import_module("resource")
except ModuleNotFoundError:
    _resource = None

logger = logging.getLogger(__name__)


def _read_first_line(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.readline().strip()
    except OSError:
        return None


def _parse_meminfo() -> dict[str, int]:
    meminfo = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                parts = value.strip().split()
                if not parts:
                    continue
                meminfo[key] = int(parts[0])
    except OSError:
        return {}
    return meminfo


def _format_bytes(value: float | int | None) -> str:
    if value is None:
        return "--"
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    return f"{size:.1f} {units[unit_index]}"


def _format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "--"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_system_health() -> dict[str, Any]:
    now = datetime.now(UTC)

    uptime_seconds = None
    uptime_line = _read_first_line("/proc/uptime")
    if uptime_line:
        try:
            uptime_seconds = float(uptime_line.split()[0])
        except (ValueError, IndexError):
            uptime_seconds = None

    meminfo = _parse_meminfo()
    mem_total_kb = meminfo.get("MemTotal")
    mem_avail_kb = meminfo.get("MemAvailable")
    mem_used_kb = None
    mem_used_pct = None
    if mem_total_kb is not None and mem_avail_kb is not None:
        mem_used_kb = max(mem_total_kb - mem_avail_kb, 0)
        mem_used_pct = (mem_used_kb / mem_total_kb) * 100 if mem_total_kb else None

    disk = shutil.disk_usage("/")
    disk_used_pct = (disk.used / disk.total) * 100 if disk.total else None

    load_avg = None
    try:
        getloadavg = getattr(os, "getloadavg", None)
        load_avg = getloadavg() if callable(getloadavg) else None
    except OSError:
        load_avg = None

    process_rss_kb = None
    if _resource is not None:
        try:
            process_rss_kb = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
        except (ValueError, OSError):
            process_rss_kb = None

    return {
        "generated_at": now,
        "uptime_seconds": uptime_seconds,
        "uptime_display": _format_duration(uptime_seconds),
        "cpu_count": os.cpu_count() or 0,
        "load_avg": load_avg,
        "load_avg_1": load_avg[0] if load_avg else None,
        "memory": {
            "total": _format_bytes(mem_total_kb * 1024 if mem_total_kb else None),
            "used": _format_bytes(mem_used_kb * 1024 if mem_used_kb else None),
            "available": _format_bytes(mem_avail_kb * 1024 if mem_avail_kb else None),
            "used_pct": f"{mem_used_pct:.1f}%" if mem_used_pct is not None else "--",
            "used_pct_value": mem_used_pct,
        },
        "disk": {
            "total": _format_bytes(disk.total),
            "used": _format_bytes(disk.used),
            "free": _format_bytes(disk.free),
            "used_pct": f"{disk_used_pct:.1f}%" if disk_used_pct is not None else "--",
            "used_pct_value": disk_used_pct,
        },
        "process": {
            "rss": _format_bytes(process_rss_kb * 1024 if process_rss_kb else None),
        },
    }


def evaluate_health(
    health: dict[str, Any],
    thresholds: dict[str, float | None],
) -> dict[str, Any]:
    status = "ok"
    issues: list[dict[str, str]] = []

    def register(level: str, message: str) -> None:
        nonlocal status
        if level == "critical":
            status = "critical"
        elif level == "warning" and status != "critical":
            status = "warning"
        issues.append({"level": level, "message": message})

    disk_pct = health.get("disk", {}).get("used_pct_value")
    mem_pct = health.get("memory", {}).get("used_pct_value")
    load_avg = health.get("load_avg_1")
    cpu_count = health.get("cpu_count") or 1

    disk_warn = thresholds.get("disk_warn_pct")
    disk_crit = thresholds.get("disk_crit_pct")
    mem_warn = thresholds.get("mem_warn_pct")
    mem_crit = thresholds.get("mem_crit_pct")
    load_warn = thresholds.get("load_warn")
    load_crit = thresholds.get("load_crit")

    if disk_pct is not None:
        if disk_crit is not None and disk_pct >= disk_crit:
            register("critical", f"Disk usage at {disk_pct:.1f}%")
        elif disk_warn is not None and disk_pct >= disk_warn:
            register("warning", f"Disk usage at {disk_pct:.1f}%")

    if mem_pct is not None:
        if mem_crit is not None and mem_pct >= mem_crit:
            register("critical", f"Memory usage at {mem_pct:.1f}%")
        elif mem_warn is not None and mem_pct >= mem_warn:
            register("warning", f"Memory usage at {mem_pct:.1f}%")

    if load_avg is not None:
        load_per_core = load_avg / cpu_count
        if load_crit is not None and load_per_core >= load_crit:
            register("critical", f"Load per core at {load_per_core:.2f}")
        elif load_warn is not None and load_per_core >= load_warn:
            register("warning", f"Load per core at {load_per_core:.2f}")

    return {"status": status, "issues": issues}
