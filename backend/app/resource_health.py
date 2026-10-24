"""Container-aware resource samples and visible, bounded alert notifications.

No cloud billing API is inferred from operating-system measurements. This module
does not query Neon, send mail, register external monitors, or keep Neon awake.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import shutil
import time

log = logging.getLogger(__name__)


def _read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return None


def _number(path):
    try:
        value = _read(path)
        return int(value) if value and value != "max" else None
    except ValueError:
        return None


@dataclass(frozen=True)
class ResourceConfig:
    data_dir: str = "/app/data"
    cpu_warning: float = 75
    cpu_critical: float = 90
    ram_warning: float = 75
    ram_critical: float = 90
    disk_warning: float = 70
    disk_critical: float = 85
    interval_seconds: int = 30

    @classmethod
    def from_env(cls):
        values = {"data_dir": os.getenv("DATA_DIR", "/app/data"),
                  "interval_seconds": max(5, int(os.getenv("RESOURCE_SAMPLE_SECONDS", "30")))}
        for metric in ("cpu", "ram", "disk"):
            default = (70, 85) if metric == "disk" else (75, 90)
            warning = float(os.getenv(f"RESOURCE_{metric.upper()}_WARNING_PERCENT", str(default[0])))
            critical = float(os.getenv(f"RESOURCE_{metric.upper()}_CRITICAL_PERCENT", str(default[1])))
            if not 0 < warning <= critical <= 100:
                raise ValueError(f"Invalid {metric} resource thresholds")
            values[f"{metric}_warning"], values[f"{metric}_critical"] = warning, critical
        return cls(**values)


class ResourceMonitor:
    def __init__(self, config: ResourceConfig | None = None, cgroup_root="/sys/fs/cgroup", proc_root="/proc"):
        self.config = config or ResourceConfig.from_env()
        self.cgroup, self.proc = Path(cgroup_root), Path(proc_root)
        self.started = time.monotonic()
        self._previous = None
        self.latest = None
        self.alerts = {}
        self.notifications = deque(maxlen=100)

    def _cpu_capacity(self):
        capacity = float(os.cpu_count() or 1)
        try:
            quota = _read(self.cgroup / "cpu.max")
            if quota:
                maximum, period = quota.split()
                if maximum != "max" and int(period) > 0:
                    capacity = min(capacity, int(maximum) / int(period))
            else:
                maximum = _number(self.cgroup / "cpu/cpu.cfs_quota_us")
                period = _number(self.cgroup / "cpu/cpu.cfs_period_us")
                if maximum and maximum > 0 and period:
                    capacity = min(capacity, maximum / period)
        except (ValueError, ZeroDivisionError):
            pass
        return max(.01, capacity)

    def _cpu_seconds(self):
        # Container CPU includes Gunicorn, ASGI worker, and background tasks.
        data = _read(self.cgroup / "cpu.stat")
        if data:
            rows = dict(line.split() for line in data.splitlines() if len(line.split()) == 2)
            try:
                return int(rows["usage_usec"]) / 1_000_000, "container_cgroup"
            except (ValueError, KeyError):
                pass
        legacy = _number(self.cgroup / "cpuacct/cpuacct.usage")
        if legacy is not None:
            return legacy / 1_000_000_000, "container_cgroup_v1"
        return time.process_time(), "application_process"

    def _memory(self):
        current, maximum = _number(self.cgroup / "memory.current"), _number(self.cgroup / "memory.max")
        if current is None:
            current, maximum = _number(self.cgroup / "memory/memory.usage_in_bytes"), _number(self.cgroup / "memory/memory.limit_in_bytes")
        # cgroup v1 uses a very large integer to represent an unlimited limit.
        if current is not None and maximum and maximum < 2 ** 60:
            return current, maximum, "container_cgroup"
        status = _read(self.proc / "self/status")
        if status:
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    try:
                        return int(line.split()[1]) * 1024, None, "application_process"
                    except (ValueError, IndexError):
                        break
        return None, None, "unavailable"

    def sample(self):
        now, cpu = time.monotonic(), self._cpu_seconds()
        capacity = self._cpu_capacity()
        cpu_percent = None
        if self._previous and self._previous[2] == cpu[1] and now > self._previous[0]:
            cpu_percent = min(100., max(0., (cpu[0] - self._previous[1]) / (now - self._previous[0]) / capacity * 100))
        self._previous = (now, cpu[0], cpu[1])
        used_ram, max_ram, memory_source = self._memory()
        ram_percent = used_ram / max_ram * 100 if used_ram is not None and max_ram else None
        try:
            disk = shutil.disk_usage(Path(self.config.data_dir))
            disk_used, disk_total, disk_free = disk.used, disk.total, disk.free
            disk_percent = disk_used / disk_total * 100 if disk_total else None
        except OSError:
            disk_used = disk_total = disk_free = disk_percent = None
        database_bytes=sum(p.stat().st_size for p in Path(self.config.data_dir).glob('*.db*') if p.is_file())
        # A container bind mount can expose the host disk size rather than its
        # plan allowance. Report both and use the tighter remaining capacity.
        plan_bytes=max(1,int(os.getenv('PERSISTENT_STORAGE_LIMIT_GB','15')))*1024**3
        local_bytes=0
        for parent,dirs,files in os.walk(self.config.data_dir,followlinks=False):
            dirs[:]=[d for d in dirs if not (Path(parent)/d).is_symlink()]
            for name in files:
                try:
                    path=Path(parent)/name
                    if not path.is_symlink():local_bytes+=path.stat().st_size
                except OSError:pass
        storage_percent=local_bytes/plan_bytes*100
        thresholds=[60,75,85,95]
        level=max((n for n in thresholds if storage_percent>=n),default=0)
        if level:
            self.alerts['storage']={'metric':'storage','severity':'critical' if level>=85 else 'warning','value_percent':round(storage_percent,2),'threshold':level,'message':f'Persistent app data has reached the {level}% storage threshold. Preserve records and download verified archives.'}
        else:self.alerts.pop('storage',None)
        self.latest = {"sampled_at": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(now - self.started, 2),
            "cpu": {"percent": round(cpu_percent, 2) if cpu_percent is not None else None,
                    "capacity_vcpu": capacity, "source": cpu[1]},
            "ram": {"used_bytes": used_ram, "limit_bytes": max_ram,
                    "percent": round(ram_percent, 2) if ram_percent is not None else None, "source": memory_source},
            "disk": {"used_bytes": disk_used, "total_bytes": disk_total, "free_bytes": disk_free,
                    "percent": round(disk_percent, 2) if disk_percent is not None else None,
                    "source": "filesystem_containing_data_dir", "path": self.config.data_dir},
            "storage": {"application_bytes":local_bytes,"plan_limit_bytes":plan_bytes,"percent":round(storage_percent,2),"warning_thresholds":[60,75,85,95],"sqlite_bytes":database_bytes},
            "database": {"type":"SQLite","external_subscription_required":False}}
        for metric, value in (("cpu", cpu_percent), ("ram", ram_percent), ("disk", disk_percent)):
            previous = self.alerts.get(metric)
            if value is None:
                if previous:
                    self.alerts[metric] = {**previous, "sample_unavailable": True}
                continue
            warning, critical = getattr(self.config, f"{metric}_warning"), getattr(self.config, f"{metric}_critical")
            severity = "critical" if value >= critical else "warning" if value >= warning else None
            if severity:
                self.alerts[metric] = {"metric": metric, "severity": severity, "value_percent": round(value, 2),
                    "started_at": previous["started_at"] if previous else self.latest["sampled_at"],
                    "updated_at": self.latest["sampled_at"], "sample_unavailable": False,
                    "message": f"{metric.upper()} usage is {value:.1f}% ({severity})."}
            else:
                self.alerts.pop(metric, None)
            if (previous or {}).get("severity") != severity:
                notification = {"metric": metric, "severity": severity or "recovered", "timestamp": self.latest["sampled_at"],
                    "message": self.alerts[metric]["message"] if severity else f"{metric.upper()} usage has returned below its warning threshold."}
                self.notifications.append(notification)
                log.warning("resource_alert metric=%s severity=%s usage_percent=%.1f", metric, severity or "recovered", value)
        return self.status()

    def status(self):
        return {"sample": self.latest, "active_alerts": list(self.alerts.values()),
            "notifications": list(self.notifications), "notifications_delivery": "owner_dashboard_and_server_logs",
            "thresholds": {m: {"warning": getattr(self.config, f"{m}_warning"),
                              "critical": getattr(self.config, f"{m}_critical")} for m in ("cpu", "ram", "disk")}}

    async def run(self, stop_event: asyncio.Event):
        while not stop_event.is_set():
            self.sample()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.config.interval_seconds)
            except asyncio.TimeoutError:
                pass
