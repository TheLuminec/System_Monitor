#!/usr/bin/env python3
"""Avalon Monitor agent - collects system metrics and pushes them to the hub.

Single file, Python 3.8+, only dependency: psutil.
Runs on Linux (x86/ARM incl. Raspberry Pi), Windows and macOS.

Configuration (env vars override the config file; file is KEY=VALUE lines):

    AVM_SERVER_URL      http://100.x.y.z:8787   (hub over Tailscale)
    AVM_TOKEN           avm_...                 (from `manage.py add-host`)
    AVM_INTERVAL        5                       seconds between samples
    AVM_HOSTNAME        override reported hostname
    AVM_TAGS            comma list, e.g. "gpu,training"
    AVM_PROCESSES       8      top-N processes by CPU to report (0 disables)
    AVM_DISK_EXCLUDE_FS squashfs,tmpfs,devtmpfs,overlay,...   fs types to ignore
    AVM_WATCH_PROCESSES comma list of process names/cmdline substrings to check
    AVM_WATCH_FILES     comma list of path[:max_age_sec] heartbeat files to check
    AVM_WATCH_TCP       comma list of host:port to check
    AVM_WATCH_MOUNTS    comma list of paths that must be mount points (removable/network volumes)
    AVM_WATCH_CONTENT   comma list of small text files whose first line is shown (e.g. a "current job" file)
    AVM_WATCH_MARKERS   comma list of globs that must match NOTHING (e.g. a queue's *.failed marker files);
                        `glob::regex` instead counts only files whose content matches the regex as failures
                        (e.g. ~/markers/*.done::rc=(?!0\b)|oom_kill=(?!0\b)); no commas inside the regex
    AVM_WATCH_UNITS     comma list of systemd units to show the state of: [system:|user:|user@NAME:]name-or-glob
    AVM_WATCH_JOBS      like AVM_WATCH_UNITS, but an active match means "a job is running" - the hub uses it
                        for its GPU-stall rule (job active but GPU idle)
    AVM_MEM_AVAILABLE_WARN_GB  amber chip when MemAvailable drops below this many GB
    AVM_WATCH_NONEMPTY  comma list of files that should have at least one non-blank line (e.g. queue.txt);
                        an empty one raises a warning chip - silence from a queue is not success
    AVM_LHM_URL         http://localhost:8085/data.json  (LibreHardwareMonitor on Windows)
    AVM_LOG_LEVEL       info

The hub may override the AVM_WATCH_* / AVM_PROCESSES / AVM_TAGS / AVM_MEM_AVAILABLE_WARN_GB
settings remotely (set from the dashboard); they arrive in the reply to each push and
take effect on the next sample. A "respawn" command in the reply makes the agent re-exec
itself (picking up an updated script and config file).

Usage:
    avalon_agent.py --config /etc/avalon-agent/agent.conf
    avalon_agent.py --once          # print one sample as JSON and exit
    avalon_agent.py --check         # verify connectivity to the hub
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

try:
    import psutil
except ImportError:  # pragma: no cover
    sys.stderr.write("psutil is required: pip install psutil\n")
    sys.exit(2)

AGENT_VERSION = "1.1.0"
IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"

log = logging.getLogger("avalon-agent")


# ------------------------------------------------------------------ config
DEFAULTS = {
    "AVM_SERVER_URL": "",
    "AVM_TOKEN": "",
    "AVM_INTERVAL": "5",
    "AVM_HOSTNAME": "",
    "AVM_TAGS": "",
    "AVM_PROCESSES": "8",
    "AVM_DISK_EXCLUDE_FS": "squashfs,tmpfs,devtmpfs,overlay,autofs,fuse.portal,fuse.gvfsd-fuse,nsfs,proc,sysfs,cgroup,cgroup2,efivarfs,iso9660,udf,vfat",
    "AVM_WATCH_PROCESSES": "",
    "AVM_WATCH_FILES": "",
    "AVM_WATCH_TCP": "",
    "AVM_WATCH_MOUNTS": "",
    "AVM_WATCH_CONTENT": "",
    "AVM_WATCH_MARKERS": "",
    "AVM_WATCH_NONEMPTY": "",
    "AVM_WATCH_UNITS": "",
    "AVM_WATCH_JOBS": "",
    "AVM_MEM_AVAILABLE_WARN_GB": "",
    "AVM_LHM_URL": "",
    "AVM_LOG_LEVEL": "info",
}


def load_config(path: Optional[str]) -> Dict[str, str]:
    cfg = dict(DEFAULTS)
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in DEFAULTS:
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def _csv(value: str) -> List[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _run(cmd: List[str], timeout: float = 5.0) -> Optional[str]:
    """Run a command and return stdout, or None if it fails/is missing."""
    try:
        kwargs: Dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
        if out.returncode != 0:
            return None
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _num(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, str):
            x = x.strip()
            if x in ("", "N/A", "[N/A]", "[Not Supported]", "n/a"):
                return None
            x = re.sub(r"[^0-9.+-]", "", x)
            if x in ("", ".", "-", "+"):
                return None
        return float(x)
    except (TypeError, ValueError):
        return None


# -------------------------------------------------------------- host info
def _os_pretty() -> str:
    if IS_LINUX:
        try:
            with open("/etc/os-release") as f:
                kv = dict(l.rstrip("\n").split("=", 1) for l in f if "=" in l)
            name = kv.get("PRETTY_NAME", "").strip('"')
            if name:
                return name
        except OSError:
            pass
        return "Linux"
    if IS_WINDOWS:
        rel = platform.release()
        ver = platform.version()
        try:
            build = int(ver.split(".")[-1])
            if rel == "10" and build >= 22000:
                rel = "11"
        except ValueError:
            pass
        return "Windows " + rel
    if IS_MAC:
        return "macOS " + platform.mac_ver()[0]
    return platform.system()


def _cpu_model() -> str:
    if IS_LINUX:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.lower().startswith(("model name", "hardware", "model")) and ":" in line:
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        # Raspberry Pi
        try:
            with open("/proc/device-tree/model") as f:
                return f.read().strip("\x00 \n")
        except OSError:
            pass
    if IS_WINDOWS:
        return os.environ.get("PROCESSOR_IDENTIFIER", platform.processor())
    if IS_MAC:
        out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if out:
            return out.strip()
    return platform.processor() or platform.machine()


_IS_RPI = False
if IS_LINUX:
    try:
        with open("/proc/device-tree/model") as _f:
            _IS_RPI = "raspberry" in _f.read().lower()
    except OSError:
        pass

_HOST_STATIC: Dict[str, Any] = {}


def host_info(cfg: Dict[str, str]) -> Dict[str, Any]:
    global _HOST_STATIC
    if not _HOST_STATIC:
        boot = psutil.boot_time()
        _HOST_STATIC = {
            "hostname": cfg["AVM_HOSTNAME"] or socket.gethostname(),
            "os": platform.system(),
            "os_version": _os_pretty() + (" (Raspberry Pi)" if _IS_RPI else ""),
            "platform": platform.platform(),
            "arch": platform.machine(),
            "kernel": platform.release(),
            "boot_time": boot,
            "agent_version": AGENT_VERSION,
            "cpu_model": _cpu_model(),
            "cpu_count": psutil.cpu_count(logical=True),
            "cpu_count_physical": psutil.cpu_count(logical=False),
            "tags": _csv(cfg["AVM_TAGS"]),
        }
    info = dict(_HOST_STATIC)
    info["uptime_sec"] = time.time() - info["boot_time"]
    return info


# ------------------------------------------------------------ collectors
class RateTracker:
    """Turns monotonically increasing counters into per-second rates."""

    def __init__(self) -> None:
        self.prev: Dict[str, float] = {}
        self.prev_t: Optional[float] = None

    def update(self, counters: Dict[str, float]) -> Dict[str, Optional[float]]:
        now = time.monotonic()
        rates: Dict[str, Optional[float]] = {}
        if self.prev_t is not None:
            dt = max(now - self.prev_t, 1e-3)
            for k, v in counters.items():
                p = self.prev.get(k)
                rates[k] = max(0.0, (v - p) / dt) if p is not None and v >= p else (0.0 if p is not None else None)
        else:
            rates = {k: None for k in counters}
        self.prev = dict(counters)
        self.prev_t = now
        return rates


_disk_rates = RateTracker()
_net_rates = RateTracker()


def collect_cpu() -> Dict[str, Any]:
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    out: Dict[str, Any] = {
        "percent": round(sum(per_core) / len(per_core), 1) if per_core else psutil.cpu_percent(interval=None),
        "per_core": [round(c, 1) for c in per_core],
    }
    try:
        if hasattr(psutil, "getloadavg"):
            out["load"] = [round(x, 2) for x in psutil.getloadavg()]
    except (OSError, AttributeError):
        pass
    try:
        f = psutil.cpu_freq()
        if f and f.current:
            out["freq_mhz"] = round(f.current, 0)
    except Exception:
        pass
    return out


def collect_memory() -> Dict[str, Any]:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    out = {
        "total": vm.total, "used": vm.total - vm.available, "available": vm.available,
        "percent": round(100.0 * (vm.total - vm.available) / vm.total, 1) if vm.total else None,
        "swap_total": sw.total, "swap_used": sw.used, "swap_percent": sw.percent,
    }
    if IS_LINUX:
        try:
            with open("/proc/meminfo") as f:
                mi = {}
                for line in f:
                    k, _, v = line.partition(":")
                    mi[k.strip()] = int(v.strip().split()[0]) * 1024
            out["committed"] = mi.get("Committed_AS")
            out["commit_limit"] = mi.get("CommitLimit")
        except (OSError, ValueError):
            pass
    elif IS_WINDOWS:
        # On Windows psutil's swap_memory() reports the system commit charge.
        out["committed"] = sw.used
        out["commit_limit"] = sw.total
    return out


def collect_disks(exclude_fs: List[str]) -> List[Dict[str, Any]]:
    disks = []
    seen = set()
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception:
        return disks
    for p in parts:
        if p.fstype.lower() in exclude_fs or not p.fstype:
            continue
        if IS_LINUX and (p.mountpoint.startswith(("/snap/", "/boot/efi", "/run/", "/var/lib/docker/")) or "/snap/" in p.mountpoint):
            continue
        if IS_WINDOWS and "cdrom" in p.opts:
            continue
        if p.device in seen:  # bind mounts / btrfs subvolumes share a device
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except (PermissionError, OSError):
            continue
        if u.total == 0:
            continue
        seen.add(p.device)
        disks.append({
            "device": p.device, "mountpoint": p.mountpoint, "fstype": p.fstype,
            "total": u.total, "used": u.used, "free": u.free,
            "percent": round(100.0 * u.used / (u.used + u.free), 1) if (u.used + u.free) else u.percent,
        })
    return disks


def collect_disk_io() -> Dict[str, Any]:
    try:
        io = psutil.disk_io_counters()
    except Exception:
        return {}
    if not io:
        return {}
    r = _disk_rates.update({"rb": io.read_bytes, "wb": io.write_bytes, "rc": io.read_count, "wc": io.write_count})
    return {"read_bps": r["rb"], "write_bps": r["wb"], "read_iops": r["rc"], "write_iops": r["wc"]}


_NET_SKIP = re.compile(r"^(lo|docker\d*|br-|veth|virbr|vmnet|vboxnet|tailscale|ts\d|wg\d|Loopback|isatap|Teredo|tun\d)", re.I)


def collect_network() -> Dict[str, Any]:
    try:
        pernic = psutil.net_io_counters(pernic=True)
        stats = psutil.net_if_stats()
    except Exception:
        return {}
    counters: Dict[str, float] = {}
    names = []
    for name, c in pernic.items():
        if _NET_SKIP.match(name):
            continue
        st = stats.get(name)
        if st and not st.isup:
            continue
        names.append(name)
        counters[name + "|rx"] = c.bytes_recv
        counters[name + "|tx"] = c.bytes_sent
    rates = _net_rates.update(counters)
    ifaces = []
    total_rx = total_tx = 0.0
    any_rate = False
    for name in names:
        rx, tx = rates.get(name + "|rx"), rates.get(name + "|tx")
        if rx is not None:
            any_rate = True
            total_rx += rx
            total_tx += tx or 0.0
        st = stats.get(name)
        ifaces.append({"name": name, "rx_bps": rx, "tx_bps": tx, "up": bool(st.isup) if st else None,
                       "speed_mbps": st.speed if st and st.speed > 0 else None})
    ifaces.sort(key=lambda i: -((i["rx_bps"] or 0) + (i["tx_bps"] or 0)))
    return {"rx_bps": total_rx if any_rate else None, "tx_bps": total_tx if any_rate else None, "interfaces": ifaces[:8]}


# ---- temperatures --------------------------------------------------------
_CPU_TEMP_KEYS = ("k10temp", "coretemp", "cpu_thermal", "cpu-thermal", "zenpower", "acpitz", "soc_thermal", "x86_pkg_temp")
_CPU_TEMP_LABELS = ("tctl", "tdie", "package id 0", "package", "cpu", "core 0", "physical id 0")


def collect_temps() -> Dict[str, Any]:
    """Returns {"sensors": [...], "cpu_temp": float|None}."""
    sensors: List[Dict[str, Any]] = []
    cpu_temp: Optional[float] = None
    if hasattr(psutil, "sensors_temperatures"):
        try:
            raw = psutil.sensors_temperatures() or {}
        except Exception:
            raw = {}
        for chip, entries in raw.items():
            for e in entries:
                if e.current is None or e.current <= 0 or e.current > 200:
                    continue
                label = e.label or chip
                if e.label and e.label.lower() != chip.lower():
                    label = "%s %s" % (chip, e.label)
                sensors.append({"label": label, "current": round(e.current, 1),
                                "high": e.high if e.high and e.high > 0 else None,
                                "critical": e.critical if e.critical and e.critical > 0 else None})
                if cpu_temp is None and chip.lower() in _CPU_TEMP_KEYS:
                    if not e.label or any(k in e.label.lower() for k in _CPU_TEMP_LABELS):
                        cpu_temp = round(e.current, 1)
        if cpu_temp is None:
            for chip, entries in raw.items():
                if chip.lower() in _CPU_TEMP_KEYS and entries and entries[0].current:
                    cpu_temp = round(entries[0].current, 1)
                    break
    if cpu_temp is None and IS_LINUX:
        # thermal_zone fallback (Raspberry Pi and many ARM boards)
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                t = int(f.read().strip()) / 1000.0
            if 0 < t < 200:
                cpu_temp = round(t, 1)
                if not sensors:
                    sensors.append({"label": "cpu", "current": cpu_temp})
        except (OSError, ValueError):
            pass
    return {"sensors": sensors[:24], "cpu_temp": cpu_temp}


# ---- GPUs ----------------------------------------------------------------
def _find_nvidia_smi() -> Optional[str]:
    p = shutil.which("nvidia-smi")
    if p:
        return p
    if IS_WINDOWS:
        for cand in (r"C:\Windows\System32\nvidia-smi.exe",
                     r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if os.path.isfile(cand):
                return cand
    return None


_NVIDIA_SMI = _find_nvidia_smi()
_ROCM_SMI = shutil.which("rocm-smi") if IS_LINUX else None
_NV_FIELDS = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed,clocks.sm"


def gpus_nvidia() -> List[Dict[str, Any]]:
    if not _NVIDIA_SMI:
        return []
    out = _run([_NVIDIA_SMI, "--query-gpu=" + _NV_FIELDS, "--format=csv,noheader,nounits"], timeout=6)
    if not out:
        return []
    gpus = []
    for line in out.strip().splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 6:
            continue
        mu, mt = _num(cols[3]), _num(cols[4])
        gpus.append({
            "index": int(_num(cols[0]) or 0), "vendor": "nvidia", "name": cols[1], "source": "nvidia-smi",
            "util_percent": _num(cols[2]),
            "mem_used": int(mu * 1024 * 1024) if mu is not None else None,
            "mem_total": int(mt * 1024 * 1024) if mt is not None else None,
            "mem_percent": round(100.0 * mu / mt, 1) if mu is not None and mt else None,
            "temp_c": _num(cols[5]),
            "power_w": _num(cols[6]) if len(cols) > 6 else None,
            "power_limit_w": _num(cols[7]) if len(cols) > 7 else None,
            "fan_percent": _num(cols[8]) if len(cols) > 8 else None,
            "clock_mhz": _num(cols[9]) if len(cols) > 9 else None,
        })
    return gpus


_LSPCI_NAMES: Dict[str, str] = {}


def _lspci_name(pci_addr: str) -> Optional[str]:
    """Marketing name of a PCI device from `lspci -mm` (prefers the board's subsystem name)."""
    if pci_addr in _LSPCI_NAMES:
        return _LSPCI_NAMES[pci_addr]
    name = None
    out = _run(["lspci", "-mm", "-s", pci_addr], timeout=3)
    if out:
        import shlex
        try:
            fields = shlex.split(out.strip().splitlines()[0])
        except ValueError:
            fields = []
        fields = [f for f in fields if not f.startswith("-")]
        # slot, class, vendor, device, [subvendor, subdevice]
        for cand in (fields[5] if len(fields) > 5 else "", fields[3] if len(fields) > 3 else ""):
            m = re.search(r"\[([^\]]+)\]", cand)
            if m:
                name = m.group(1)
                break
            if cand:
                name = cand
                break
    _LSPCI_NAMES[pci_addr] = name
    return name


def gpus_amd_sysfs() -> List[Dict[str, Any]]:
    """AMD GPUs via the amdgpu driver's sysfs (no rocm needed). Linux only."""
    gpus = []
    base = "/sys/class/drm"
    if not os.path.isdir(base):
        return gpus
    for entry in sorted(os.listdir(base)):
        if not re.fullmatch(r"card\d+", entry):
            continue
        dev = os.path.join(base, entry, "device")
        busy = os.path.join(dev, "gpu_busy_percent")
        if not os.path.isfile(busy):
            continue

        def rd(path: str) -> Optional[str]:
            try:
                with open(path) as f:
                    return f.read().strip()
            except OSError:
                return None

        vendor_id = rd(os.path.join(dev, "vendor")) or ""
        if vendor_id.lower() not in ("0x1002", "0x1022"):
            continue
        used, total = _num(rd(os.path.join(dev, "mem_info_vram_used"))), _num(rd(os.path.join(dev, "mem_info_vram_total")))
        temp = power = fan = clock = None
        hw = os.path.join(dev, "hwmon")
        if os.path.isdir(hw):
            for h in os.listdir(hw):
                hp = os.path.join(hw, h)
                for tf in ("temp2_input", "temp1_input"):  # temp2 is usually "junction"; prefer edge=temp1? use first that exists
                    t = _num(rd(os.path.join(hp, tf)))
                    if t is not None:
                        temp = t / 1000.0
                        break
                p = _num(rd(os.path.join(hp, "power1_average"))) or _num(rd(os.path.join(hp, "power1_input")))
                if p is not None:
                    power = p / 1_000_000.0
                f1 = _num(rd(os.path.join(hp, "fan1_input")))
                fmax = _num(rd(os.path.join(hp, "fan1_max")))
                if f1 is not None and fmax:
                    fan = round(100.0 * f1 / fmax, 1)
                c = _num(rd(os.path.join(hp, "freq1_input")))
                if c is not None:
                    clock = c / 1_000_000.0
        name = rd(os.path.join(dev, "product_name")) or _lspci_name(os.path.basename(os.path.realpath(dev))) or "AMD GPU"
        state = rd(os.path.join(dev, "power", "runtime_status")) or ""
        util = _num(rd(busy))
        if util is None and state == "suspended":
            util = 0.0  # power-gated: nothing can be running on it
        gpus.append({
            "index": len(gpus), "vendor": "amd", "name": name, "source": "sysfs", "state": state,
            "util_percent": util,
            "mem_used": int(used) if used is not None else None,
            "mem_total": int(total) if total is not None else None,
            "mem_percent": round(100.0 * used / total, 1) if used is not None and total else None,
            "temp_c": round(temp, 1) if temp is not None else None,
            "power_w": round(power, 1) if power is not None else None,
            "fan_percent": fan, "clock_mhz": clock,
        })
    return gpus


def gpus_rocm() -> List[Dict[str, Any]]:
    if not _ROCM_SMI:
        return []
    out = _run([_ROCM_SMI, "--showuse", "--showmemuse", "--showtemp", "--showpower", "--showproductname", "--json"], timeout=8)
    if not out:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    gpus = []
    for i, (card, v) in enumerate(sorted(data.items())):
        if not isinstance(v, dict):
            continue
        name = v.get("Card Series") or v.get("Card series") or v.get("Card model") or v.get("Card Model") or card
        temp = None
        for k in ("Temperature (Sensor junction) (C)", "Temperature (Sensor edge) (C)", "Temperature (Sensor memory) (C)"):
            temp = _num(v.get(k))
            if temp is not None:
                break
        power = _num(v.get("Average Graphics Package Power (W)")) or _num(v.get("Current Socket Graphics Package Power (W)"))
        gpus.append({
            "index": i, "vendor": "amd", "name": str(name), "source": "rocm-smi",
            "util_percent": _num(v.get("GPU use (%)")),
            "mem_percent": _num(v.get("GPU Memory Allocated (VRAM%)")),
            "temp_c": temp, "power_w": power,
        })
    return gpus


def gpus_lhm(url: str) -> Dict[str, Any]:
    """LibreHardwareMonitor web-server JSON (Windows): GPUs + CPU temperature."""
    result: Dict[str, Any] = {"gpus": [], "cpu_temp": None}
    if not url:
        return result
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            tree = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return result

    def walk(node, path):
        yield node, path
        for ch in node.get("Children", []) or []:
            for x in walk(ch, path + [node]):
                yield x

    def value_of(n) -> Optional[float]:
        return _num(n.get("Value"))

    gpu_idx = 0
    for node, path in walk(tree, []):
        img = (node.get("ImageURL") or "").lower()
        text = node.get("Text") or ""
        if "cpu.png" in img and node.get("Children"):
            for sub in node["Children"]:
                if (sub.get("Text") or "").lower() == "temperatures":
                    for s in sub.get("Children", []):
                        if any(k in (s.get("Text") or "").lower() for k in ("package", "tctl", "core (tctl", "cpu core")):
                            result["cpu_temp"] = value_of(s)
                            break
                    if result["cpu_temp"] is None and sub.get("Children"):
                        result["cpu_temp"] = value_of(sub["Children"][0])
        elif any(v in img for v in ("nvidia.png", "amd.png", "ati.png", "intel.png")) and node.get("Children"):
            vendor = "nvidia" if "nvidia" in img else ("intel" if "intel" in img else "amd")
            g: Dict[str, Any] = {"index": gpu_idx, "vendor": vendor, "name": text, "source": "lhm"}
            for sub in node["Children"]:
                cat = (sub.get("Text") or "").lower()
                for s in sub.get("Children", []):
                    st = (s.get("Text") or "").lower()
                    v = value_of(s)
                    if cat == "load" and st == "gpu core":
                        g["util_percent"] = v
                    elif cat == "temperatures" and st in ("gpu core", "gpu hot spot") and g.get("temp_c") is None:
                        g["temp_c"] = v
                    elif cat == "data" and v is not None:
                        if st == "gpu memory used":
                            g["mem_used"] = int(v * 1024 * 1024)
                        elif st == "gpu memory total":
                            g["mem_total"] = int(v * 1024 * 1024)
                    elif cat == "powers" and st in ("gpu package", "gpu power", "gpu core") and g.get("power_w") is None:
                        g["power_w"] = v
                    elif cat == "clocks" and st == "gpu core":
                        g["clock_mhz"] = v
                    elif cat == "controls" and st == "gpu fan":
                        g["fan_percent"] = v
            if g.get("mem_used") is not None and g.get("mem_total"):
                g["mem_percent"] = round(100.0 * g["mem_used"] / g["mem_total"], 1)
            result["gpus"].append(g)
            gpu_idx += 1
    return result


_ROCM_LAST = [0.0]


def _rocm_throttle(min_gap: float = 60.0) -> bool:
    now = time.monotonic()
    if now - _ROCM_LAST[0] < min_gap:
        return False
    _ROCM_LAST[0] = now
    return True


def collect_gpus(cfg: Dict[str, str], lhm: Dict[str, Any]) -> List[Dict[str, Any]]:
    gpus = gpus_nvidia()
    if IS_LINUX:
        amd = gpus_amd_sysfs()
        if amd and any(g.get("util_percent") is None for g in amd) and _rocm_throttle():
            # sysfs can refuse reads (EBUSY) while the card is changing power state; fill from rocm-smi
            # (throttled: rocm-smi is slow and wakes a power-gated card)
            for g, r in zip(amd, gpus_rocm()):
                for k in ("util_percent", "mem_percent", "temp_c", "power_w"):
                    if g.get(k) is None and r.get(k) is not None:
                        g[k] = r[k]
        amd = amd or gpus_rocm()
        for g in amd:
            g["index"] = len(gpus)
            gpus.append(g)
    if not gpus and lhm.get("gpus"):
        gpus = lhm["gpus"]
    elif lhm.get("gpus"):
        # nvidia-smi already covered NVIDIA cards; add non-NVIDIA cards LHM knows about
        for g in lhm["gpus"]:
            if g["vendor"] != "nvidia":
                g["index"] = len(gpus)
                gpus.append(g)
    return gpus


# ---- processes -----------------------------------------------------------
_proc_primed = False


def collect_processes(top_n: int) -> List[Dict[str, Any]]:
    global _proc_primed
    if top_n <= 0:
        return []
    ncpu = psutil.cpu_count(logical=True) or 1
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "memory_info", "username"]):
        try:
            info = p.info
            if info["pid"] in (0,) or info["name"] in ("System Idle Process", "Idle"):
                continue
            cpu = info["cpu_percent"]
            procs.append({
                "pid": info["pid"], "name": (info["name"] or "")[:64],
                "cpu_percent": round(cpu / ncpu, 1) if cpu is not None else None,  # normalise to 0-100 of the machine
                "mem_percent": round(info["memory_percent"], 2) if info["memory_percent"] is not None else None,
                "mem_rss": info["memory_info"].rss if info["memory_info"] else None,
                "user": (info["username"] or "").split("\\")[-1][:32],
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    if not _proc_primed:
        _proc_primed = True  # first call returns 0.0 for cpu_percent; still useful for memory
    procs.sort(key=lambda x: ((x["cpu_percent"] or 0), (x["mem_percent"] or 0)), reverse=True)
    return procs[:top_n]


# ---- checks --------------------------------------------------------------
def collect_checks(cfg: Dict[str, str]) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    watch_procs = _csv(cfg["AVM_WATCH_PROCESSES"])
    if watch_procs:
        running: Dict[str, List[int]] = {w: [] for w in watch_procs}
        ignore = {os.getpid(), os.getppid()}
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            if p.info["pid"] in ignore:
                continue
            try:
                name = (p.info["name"] or "").lower()
                cmd = " ".join(p.info["cmdline"] or []).lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            for w in watch_procs:
                wl = w.lower()
                if wl == name or wl in cmd:
                    running[w].append(p.info["pid"])
        for w in watch_procs:
            pids = running[w]
            checks.append({"name": w, "kind": "process", "ok": bool(pids),
                           "detail": ("pid " + ",".join(str(x) for x in pids[:3])) if pids else "not running",
                           "value": float(len(pids))})
    for item in _csv(cfg["AVM_WATCH_FILES"]):
        path, max_age = item, 300.0
        # Windows paths contain ':' after the drive letter; only split on a trailing :<digits>
        m = re.match(r"^(.*):(\d+(?:\.\d+)?)$", item)
        if m:
            path, max_age = m.group(1), float(m.group(2))
        try:
            age = time.time() - os.stat(path).st_mtime
            checks.append({"name": os.path.basename(path) or path, "kind": "file", "ok": age <= max_age,
                           "detail": "updated %ds ago (max %ds)" % (age, max_age), "value": round(age, 1)})
        except OSError:
            checks.append({"name": os.path.basename(path) or path, "kind": "file", "ok": False, "detail": "missing"})
    for path in _csv(cfg["AVM_WATCH_MOUNTS"]):
        try:
            mounted = os.path.ismount(path)
        except OSError:
            mounted = False
        checks.append({"name": os.path.basename(path.rstrip("/\\")) or path, "kind": "mount", "ok": mounted,
                       "detail": ("mounted at " + path) if mounted else ("NOT mounted: " + path)})
    for path in _csv(cfg["AVM_WATCH_CONTENT"]):
        name = os.path.basename(path) or path
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                line = f.readline().strip()
            age = time.time() - os.stat(path).st_mtime
            checks.append({"name": name, "kind": "content", "ok": True, "detail": line[:160] or "(empty)", "value": round(age, 1)})
        except OSError as e:
            checks.append({"name": name, "kind": "content", "ok": False, "detail": "unreadable: %s" % e.strerror})
    for item in _csv(cfg["AVM_WATCH_MARKERS"]):
        import glob as _glob
        pattern, _, regex = item.partition("::")
        try:
            matches = sorted(_glob.glob(os.path.expanduser(pattern)), key=lambda f: -os.stat(f).st_mtime)
        except OSError:
            matches = []
        if regex:
            try:
                rx = re.compile(regex)
            except re.error as e:
                checks.append({"name": os.path.basename(pattern), "kind": "marker", "ok": False, "detail": "bad regex: %s" % e})
                continue
            failed = []
            for f in matches[:200]:
                try:
                    with open(f, "r", encoding="utf-8", errors="replace") as fh:
                        if rx.search(fh.read(4096)):
                            failed.append(f)
                except OSError:
                    continue
            matches = failed
        label = os.path.basename(pattern) or pattern
        if matches:
            newest = matches[0]
            age = time.time() - os.stat(newest).st_mtime
            names = ", ".join(os.path.basename(m) for m in matches[:3]) + (" ..." if len(matches) > 3 else "")
            checks.append({"name": label, "kind": "marker", "ok": False, "value": float(len(matches)),
                           "detail": "%d file%s: %s (newest %s ago)" % (len(matches), "s" if len(matches) != 1 else "", names, _fmt_age(age))})
        else:
            checks.append({"name": label, "kind": "marker", "ok": True, "value": 0.0, "detail": "none"})
    for kind, key in (("unit", "AVM_WATCH_UNITS"), ("job", "AVM_WATCH_JOBS")):
        for item in _csv(cfg[key]):
            checks.append(_systemd_check(item, kind))
    warn_gb = _num(cfg["AVM_MEM_AVAILABLE_WARN_GB"])
    if warn_gb:
        avail = psutil.virtual_memory().available
        checks.append({"name": "mem available", "kind": "memory", "ok": avail >= warn_gb * 1024 ** 3, "level": "warning",
                       "value": round(avail / 1024 ** 3, 2), "detail": "%.1f GB available (warn below %g GB)" % (avail / 1024 ** 3, warn_gb)})
    for path in _csv(cfg["AVM_WATCH_NONEMPTY"]):
        name = os.path.basename(path) or path
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                n = sum(1 for line in f if line.strip() and not line.lstrip().startswith("#"))
            checks.append({"name": name, "kind": "queue", "ok": n > 0, "value": float(n), "level": "warning",
                           "detail": ("%d pending" % n) if n else "EMPTY - nothing queued"})
        except OSError as e:
            checks.append({"name": name, "kind": "queue", "ok": False, "detail": "unreadable: %s" % e.strerror})
    for item in _csv(cfg["AVM_WATCH_TCP"]):
        host, _, port = item.rpartition(":")
        t0 = time.monotonic()
        try:
            with socket.create_connection((host or "127.0.0.1", int(port)), timeout=2):
                pass
            checks.append({"name": item, "kind": "tcp", "ok": True,
                           "detail": "open (%.0f ms)" % ((time.monotonic() - t0) * 1000), "value": round((time.monotonic() - t0) * 1000, 1)})
        except (OSError, ValueError) as e:
            checks.append({"name": item, "kind": "tcp", "ok": False, "detail": str(e)[:80]})
    return checks


def _systemd_check(item: str, kind: str) -> Dict[str, Any]:
    """State of systemd unit(s): item = "[system:|user:|user@NAME:]name-or-glob"."""
    scope, _, pattern = item.rpartition(":")
    if not pattern:
        scope, pattern = "", item
    if not scope:
        scope = "system" if os.geteuid() == 0 else "user"
    base = ["systemctl", "--no-pager", "--plain", "--no-legend"]
    if scope == "system":
        pass
    elif scope == "user":
        base.append("--user")
    elif scope.startswith("user@"):
        base += ["--user", "-M", scope[5:] + "@"]
    else:
        return {"name": pattern, "kind": kind, "ok": False, "detail": "bad scope %r" % scope}
    name = pattern
    out = _run(base + ["list-units", "--all", "--state=active,failed,activating", pattern], timeout=5)
    if out is None:
        return {"name": name, "kind": kind, "ok": False, "detail": "systemctl unavailable"}
    active, failed = [], []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        unit, state = cols[0], cols[2]
        (failed if state == "failed" else active).append(unit)
    enabled = ""
    if not any(ch in pattern for ch in "*?["):
        en = _run(base + ["is-enabled", pattern], timeout=5)
        enabled = (en or "").strip().splitlines()[0] if (en or "").strip() else ""
    if failed:
        detail = "FAILED: " + ", ".join(failed[:3])
    elif active:
        detail = "active: " + ", ".join(u.replace(".scope", "").replace(".service", "") for u in active[:3]) + (" ..." if len(active) > 3 else "")
    else:
        detail = "inactive" + (" (%s)" % enabled if enabled else "")
    return {"name": name, "kind": kind, "ok": not failed, "value": float(len(active)), "detail": detail,
            "level": "info" if kind == "job" else ""}


def _fmt_age(sec: float) -> str:
    sec = int(sec)
    if sec < 90:
        return "%ds" % sec
    if sec < 5400:
        return "%dm" % (sec // 60)
    if sec < 172800:
        return "%dh" % (sec // 3600)
    return "%dd" % (sec // 86400)


def collect_battery() -> Optional[Dict[str, Any]]:
    try:
        b = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    except Exception:
        return None
    if not b:
        return None
    secs = b.secsleft if isinstance(b.secsleft, int) and b.secsleft >= 0 else None
    return {"percent": round(b.percent, 1), "plugged": bool(b.power_plugged), "secs_left": secs}


# ---------------------------------------------------------------- sample
def _safe(fn, *args, default=None):
    try:
        return fn(*args)
    except Exception as e:  # never let one collector kill the sample
        log.debug("collector %s failed: %s", getattr(fn, "__name__", fn), e)
        return default


def collect_sample(cfg: Dict[str, str], interval: float) -> Dict[str, Any]:
    exclude_fs = [x.lower() for x in _csv(cfg["AVM_DISK_EXCLUDE_FS"])]
    lhm = _safe(gpus_lhm, cfg["AVM_LHM_URL"], default={"gpus": [], "cpu_temp": None})
    temps = _safe(collect_temps, default={"sensors": [], "cpu_temp": None})
    cpu = _safe(collect_cpu, default={})
    cpu["temp_c"] = temps.get("cpu_temp") if temps.get("cpu_temp") is not None else lhm.get("cpu_temp")
    return {
        "ts": time.time(),
        "interval": interval,
        "host": host_info(cfg),
        "cpu": cpu,
        "memory": _safe(collect_memory, default={}),
        "disks": _safe(collect_disks, exclude_fs, default=[]),
        "disk_io": _safe(collect_disk_io, default={}),
        "network": _safe(collect_network, default={}),
        "gpus": _safe(collect_gpus, cfg, lhm, default=[]),
        "temps": temps.get("sensors", []),
        "processes": _safe(collect_processes, int(cfg["AVM_PROCESSES"] or 0), default=[]),
        "battery": _safe(collect_battery),
        "checks": _safe(collect_checks, cfg, default=[]),
    }


# ------------------------------------------------------------------ push
def push(cfg: Dict[str, str], sample: Dict[str, Any], timeout: float = 10.0) -> None:
    url = cfg["AVM_SERVER_URL"].rstrip("/") + "/api/v1/ingest"
    body = json.dumps(sample, separators=(",", ":")).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + cfg["AVM_TOKEN"],
        "User-Agent": "avalon-agent/" + AGENT_VERSION,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return {}


REMOTE_KEYS = (
    "AVM_WATCH_PROCESSES", "AVM_WATCH_FILES", "AVM_WATCH_TCP", "AVM_WATCH_MOUNTS", "AVM_WATCH_CONTENT",
    "AVM_WATCH_MARKERS", "AVM_WATCH_NONEMPTY", "AVM_WATCH_UNITS", "AVM_WATCH_JOBS",
    "AVM_MEM_AVAILABLE_WARN_GB", "AVM_PROCESSES", "AVM_TAGS",
)


class RemoteConfig:
    """Hub-pushed settings layered over the local config file."""

    def __init__(self, base: Dict[str, str]):
        self.base = dict(base)
        self.rev: Optional[int] = None
        self.overrides: Dict[str, str] = {}

    def apply(self, reply: Dict[str, Any]) -> bool:
        cfg = reply.get("config")
        if not isinstance(cfg, dict):
            if self.rev is not None:  # hub cleared the remote settings
                self.rev, self.overrides = None, {}
                return True
            return False
        rev = cfg.get("rev")
        if rev == self.rev:
            return False
        self.rev = rev
        self.overrides = {k: str(v) for k, v in (cfg.get("settings") or {}).items() if k in REMOTE_KEYS}
        return True

    def effective(self) -> Dict[str, str]:
        out = dict(self.base)
        out.update(self.overrides)
        return out


def respawn() -> None:
    """Replace this process with a fresh copy of itself (new script/config, same supervisor)."""
    log.info("respawning on hub request")
    sys.stdout.flush(); sys.stderr.flush()
    args = [sys.executable] + sys.argv
    if IS_WINDOWS:
        # execv on Windows starts a new process and exits this one; quote to survive spaces in paths.
        args = ['"%s"' % a if " " in a else a for a in args]
    os.execv(sys.executable, args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Avalon Monitor agent")
    ap.add_argument("--config", "-c", default=os.environ.get("AVM_CONFIG", ""), help="path to agent.conf")
    ap.add_argument("--once", action="store_true", help="collect one sample, print JSON, exit")
    ap.add_argument("--check", action="store_true", help="verify hub connectivity and token, exit")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    logging.basicConfig(level=cfg["AVM_LOG_LEVEL"].upper(), format="%(asctime)s %(levelname)s %(message)s")
    interval = max(1.0, float(cfg["AVM_INTERVAL"] or 5))

    if args.once:
        psutil.cpu_percent(interval=None, percpu=True)
        collect_processes(1)
        time.sleep(1.0)
        s = collect_sample(cfg, interval)
        # second pass so rates are populated
        s = collect_sample(cfg, interval)
        print(json.dumps(s, indent=2))
        return 0

    if not cfg["AVM_SERVER_URL"] or not cfg["AVM_TOKEN"]:
        log.error("AVM_SERVER_URL and AVM_TOKEN must be set (config file or environment)")
        return 2

    if args.check:
        try:
            push(cfg, collect_sample(cfg, interval))
            print("OK: hub accepted a sample from", host_info(cfg)["hostname"])
            return 0
        except urllib.error.HTTPError as e:
            print("hub rejected the request: HTTP", e.code, e.read().decode(errors="replace")[:200])
            return 1
        except Exception as e:
            print("cannot reach hub:", e)
            return 1

    stop = {"flag": False}

    def _stop(signum, frame):
        stop["flag"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    log.info("avalon-agent %s on %s -> %s every %.0fs", AGENT_VERSION, host_info(cfg)["hostname"], cfg["AVM_SERVER_URL"], interval)
    psutil.cpu_percent(interval=None, percpu=True)  # prime
    remote = RemoteConfig(cfg)
    failures = 0
    next_t = time.monotonic() + 1.0
    while not stop["flag"]:
        delay = next_t - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, 1.0))
            continue
        next_t += interval
        if next_t < time.monotonic():  # we fell behind (sleep/suspend) - resync
            next_t = time.monotonic() + interval
        try:
            eff = remote.effective()
            if remote.overrides and "AVM_TAGS" in remote.overrides:
                _HOST_STATIC.clear()  # tags are part of the cached host info
            sample = collect_sample(eff, interval)
            sample["config_rev"] = remote.rev if remote.rev is not None else 0
            reply = push(cfg, sample)
            if failures:
                log.info("hub reachable again")
            failures = 0
            if remote.apply(reply):
                log.info("applied remote settings rev %s: %s", remote.rev, ", ".join(sorted(remote.overrides)) or "(cleared)")
            if reply.get("command") == "respawn":
                respawn()
        except urllib.error.HTTPError as e:
            failures += 1
            if failures in (1, 10) or failures % 100 == 0:
                log.warning("hub rejected sample: HTTP %s %s", e.code, e.read().decode(errors="replace")[:200])
        except Exception as e:
            failures += 1
            if failures in (1, 10) or failures % 100 == 0:
                log.warning("push failed (%d in a row): %s", failures, e)
    log.info("agent stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
