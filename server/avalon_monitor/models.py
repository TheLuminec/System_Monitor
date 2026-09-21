"""Pydantic schemas for the agent -> hub payload.

Everything except `ts` and `host.hostname` is optional so agents on exotic
platforms can send whatever they manage to collect.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class HostInfo(BaseModel):
    hostname: str = Field(min_length=1, max_length=128)
    os: str = ""            # "Linux", "Windows", "Darwin"
    os_version: str = ""    # "Ubuntu 24.04", "Windows 11", "Debian 12 (rpi)"
    platform: str = ""      # e.g. "Linux-6.8.0-rpi-aarch64"
    arch: str = ""          # x86_64 / aarch64 / AMD64
    kernel: str = ""
    boot_time: Optional[float] = None
    uptime_sec: Optional[float] = None
    agent_version: str = ""
    cpu_model: str = ""
    cpu_count: Optional[int] = None
    cpu_count_physical: Optional[int] = None
    tags: list[str] = Field(default_factory=list)


class CpuMetrics(BaseModel):
    percent: Optional[float] = None
    per_core: list[float] = Field(default_factory=list)
    load: Optional[list[float]] = None          # [1, 5, 15]
    freq_mhz: Optional[float] = None
    temp_c: Optional[float] = None


class MemoryMetrics(BaseModel):
    total: Optional[int] = None
    used: Optional[int] = None
    available: Optional[int] = None
    percent: Optional[float] = None
    swap_total: Optional[int] = None
    swap_used: Optional[int] = None
    swap_percent: Optional[float] = None
    committed: Optional[int] = None          # Linux Committed_AS / Windows commit charge
    commit_limit: Optional[int] = None


class DiskUsage(BaseModel):
    device: str = ""
    mountpoint: str = ""
    fstype: str = ""
    total: Optional[int] = None
    used: Optional[int] = None
    free: Optional[int] = None
    percent: Optional[float] = None


class DiskIo(BaseModel):
    read_bps: Optional[float] = None
    write_bps: Optional[float] = None
    read_iops: Optional[float] = None
    write_iops: Optional[float] = None


class NetInterface(BaseModel):
    name: str = ""
    rx_bps: Optional[float] = None
    tx_bps: Optional[float] = None
    up: Optional[bool] = None
    speed_mbps: Optional[int] = None


class NetMetrics(BaseModel):
    rx_bps: Optional[float] = None
    tx_bps: Optional[float] = None
    interfaces: list[NetInterface] = Field(default_factory=list)


class GpuMetrics(BaseModel):
    index: int = 0
    vendor: str = ""        # nvidia / amd / intel / apple
    name: str = ""
    util_percent: Optional[float] = None
    mem_used: Optional[int] = None
    mem_total: Optional[int] = None
    mem_percent: Optional[float] = None
    temp_c: Optional[float] = None
    power_w: Optional[float] = None
    power_limit_w: Optional[float] = None
    fan_percent: Optional[float] = None
    clock_mhz: Optional[float] = None
    source: str = ""        # nvidia-smi / rocm-smi / sysfs / lhm
    state: str = ""         # e.g. "active" / "suspended" (runtime PM)


class TempSensor(BaseModel):
    label: str
    current: float
    high: Optional[float] = None
    critical: Optional[float] = None


class ProcessInfo(BaseModel):
    pid: int
    name: str = ""
    cpu_percent: Optional[float] = None
    mem_percent: Optional[float] = None
    mem_rss: Optional[int] = None
    user: str = ""


class Battery(BaseModel):
    percent: Optional[float] = None
    plugged: Optional[bool] = None
    secs_left: Optional[int] = None


class Check(BaseModel):
    """Result of a user-configured liveness check (process alive, heartbeat fresh...)."""
    name: str
    kind: str = ""          # process | file | tcp | http
    ok: bool
    detail: str = ""
    value: Optional[float] = None   # e.g. heartbeat age in seconds
    level: str = ""                 # "" (failure is red) | "warning" (amber) | "info" (never alerts)


class Sample(BaseModel):
    ts: float
    interval: Optional[float] = None
    config_rev: Optional[int] = None    # revision of hub-pushed check config the agent is running (None: unsupported)
    agent_sha256: Optional[str] = None  # sha256 of the agent's own script file (for auto-update)
    host: HostInfo
    cpu: CpuMetrics = Field(default_factory=CpuMetrics)
    memory: MemoryMetrics = Field(default_factory=MemoryMetrics)
    disks: list[DiskUsage] = Field(default_factory=list)
    disk_io: DiskIo = Field(default_factory=DiskIo)
    network: NetMetrics = Field(default_factory=NetMetrics)
    gpus: list[GpuMetrics] = Field(default_factory=list)
    temps: list[TempSensor] = Field(default_factory=list)
    processes: list[ProcessInfo] = Field(default_factory=list)
    battery: Optional[Battery] = None
    checks: list[Check] = Field(default_factory=list)
