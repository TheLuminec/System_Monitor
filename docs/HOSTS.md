# Per-host agent configuration

Ready-to-use `agent.conf` contents for the current fleet. Tokens come from
`avalon-monitor-manage add-host <name>` on AVALON.

> **Interpreter isolation.** Both installers create a venv **owned by the agent**
> (`/opt/avalon-agent/venv` on Linux, `%ProgramData%\AvalonAgent\venv` on Windows)
> and install `psutil` only there. They never touch a project's virtualenv or the
> system site-packages. On Miami in particular, do **not** point the agent at the
> Python 3.13 pipeline env or the Python 3.8.20 reproduction env — their exact
> dependency sets are recorded in reproduction certificates. If you must avoid a
> new venv entirely, use the distro interpreter with `apt install python3-psutil`
> and edit `ExecStart=` in `avalon-agent.service` to `/usr/bin/python3`.

## Miami (`feng-MS-7B51`, Ubuntu, RTX 4060 Ti) — `~/.config/avalon-agent/agent.conf`

Since 2026-09-21 Miami no longer uses the queue runner (`xrsec-queue.service`
is stopped and disabled after a 100%-RAM incident). Jobs are launched one at a
time by `gated_launch.sh` inside a `systemd --user` scope `xrsec-<job>.scope`
with a hard `MemoryMax`, and each writes `~/xrsec_markers/<job>.done`
containing `rc=… oom_kill=… peak_mb=…` on exit. The config below watches
*that* design; the old runner heartbeat / `current.txt` lines would only show
red forever. (These same checks can be set from the dashboard's host page
instead of the file — the hub pushes them to the agent.)

```ini
AVM_SERVER_URL=http://avalon:8787
AVM_TOKEN=avm_…
AVM_HOSTNAME=miami
AVM_INTERVAL=5
AVM_PROCESSES=8
AVM_TAGS=sm_89 via PTX compat (build arch list stops at 8.6/9.0; device is 8.9)

# what is running: active xrsec-*.scope units show as a "job: <name>" chip and
# feed the hub's GPU-stall rule (job active >=10 min with GPU never above 5% -> red "gpu stalled")
AVM_WATCH_JOBS=user:xrsec-*.scope
# job failure signal: a .done marker with a non-zero rc or oom_kill -> red chip with a count
AVM_WATCH_MARKERS=/home/feng/xrsec_markers/*.done::rc=(?!0\b)|oom_kill=(?!0\b)
# RAM pressure: MemAvailable is the number that matters (RSS lies); amber below 8 GB
AVM_MEM_AVAILABLE_WARN_GB=8
# the datasets live on a removable volume
AVM_WATCH_MOUNTS=/run/media/feng/Data
# the retired queue runner: shows "xrsec-queue: inactive" (grey); red only if the unit is failed
AVM_WATCH_UNITS=user:xrsec-queue.service
```

Notes:
* `user:` scopes query the login user's manager, which is why the **rootless
  install** below is the right one here (a root/system-unit agent would need
  `user@feng:` prefixes and a recent systemd to reach it).
* `rc=137 oom_kill=1` in a marker means the memory cap did its job — the chip
  is still red because the *job* failed, which is the thing to notice.
* If `xrsec-queue.service` is ever re-enabled, it needs
  `RequiresMountsFor=/run/media/feng/Data` in the unit (it raced the mount at
  boot on 2026-09-20). The agent can't check unit file contents; the chip only
  tells you it is active again.

Install (from a copy of `agent/` on Miami; agree timing with whoever owns the
running jobs first — the unit runs at `Nice=10` and only *reads* `nvidia-smi`).

**Recommended for Miami — rootless, matching how its jobs already run**
(`systemd --user` with linger). No sudo, nothing system-wide, venv under
`~/.local/share/avalon-agent`, config at `~/.config/avalon-agent/agent.conf`:

```bash
./install/install-linux.sh --user --server http://avalon:8787 --token avm_… --name miami
nano ~/.config/avalon-agent/agent.conf     # paste the block above
systemctl --user restart avalon-agent
# linger is already enabled on Miami; elsewhere: sudo loginctl enable-linger $USER
```

Running as the login user is enough: disk usage, sensors, `nvidia-smi`, and
`/proc` process names/cmdlines are all readable without root.

*Generic root install* (system unit, `/opt/avalon-agent`, `/etc/avalon-agent`):

```bash
sudo ./install/install-linux.sh --server http://avalon:8787 --token avm_… --name miami
sudo nano /etc/avalon-agent/agent.conf
sudo systemctl restart avalon-agent
```

## DESKTOP-C (Windows 11, RTX 5060 Ti) — `C:\ProgramData\AvalonAgent\agent.conf`

```ini
AVM_SERVER_URL=http://avalon:8787
AVM_TOKEN=avm_…
AVM_HOSTNAME=desktop-c
AVM_INTERVAL=5
AVM_PROCESSES=8
AVM_TAGS=RTX 5060 Ti, torch 2.10.0+cu130, numpy 2.4.2
# optional, for CPU temperature: run LibreHardwareMonitor with "Remote web server" enabled
#AVM_LHM_URL=http://localhost:8085/data.json
```

The box is a tagged Tailscale device without SSH, so copy `agent\` over by
whatever route works (git, USB) and run `install\install-windows.ps1` from an
elevated PowerShell. The Scheduled Task runs as SYSTEM at boot.

## AVALON (this hub) — `/etc/avalon-agent/agent.conf`

```ini
AVM_SERVER_URL=http://127.0.0.1:8787
AVM_TOKEN=avm_…
AVM_HOSTNAME=avalon
AVM_INTERVAL=5
AVM_TAGS=no GPU - data + coordination
AVM_WATCH_PROCESSES=nginx,cloudflared,tailscaled
AVM_WATCH_TCP=127.0.0.1:11434
AVM_WATCH_MOUNTS=/mnt/DataHoarder
```

(AVALON's RX 5700 XT is detected via sysfs and will show as a GPU; the tag
text is the coordinator's wording for the training role, not the hardware.)

## Raspberry Pi 5 / other Linux

The Linux installer is distro-agnostic (`python3` + `venv` module required;
`sudo apt install python3-venv` on Raspberry Pi OS). CPU temperature comes
from `cpu_thermal` / `thermal_zone0`; there is no GPU reporting on the Pi.
