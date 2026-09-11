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

## Miami (`feng-MS-7B51`, Ubuntu, RTX 4060 Ti) — `/etc/avalon-agent/agent.conf`

```ini
AVM_SERVER_URL=http://avalon:8787
AVM_TOKEN=avm_…
AVM_HOSTNAME=miami
AVM_INTERVAL=5
AVM_PROCESSES=8
AVM_TAGS=sm_89 via PTX compat (build arch list stops at 8.6/9.0; device is 8.9)

# queue runner (XRSEC_QUEUE_ROOT). 120 s is the runner's own staleness threshold,
# so the chip and the runner's self-check agree.
AVM_WATCH_PROCESSES=queue_runner.sh
AVM_WATCH_FILES=/run/media/feng/Data/CalebProject/scratch/queue/runner.heartbeat:120
AVM_WATCH_CONTENT=/run/media/feng/Data/CalebProject/scratch/queue/current.txt
# the queue lives on a removable volume: if the mount drops, every other meter still looks healthy
AVM_WATCH_MOUNTS=/run/media/feng/Data
```

Install (from a copy of `agent/` on Miami; agree timing with whoever owns the
running jobs first — the unit runs at `Nice=10` and only *reads* `nvidia-smi`):

```bash
sudo ./install/install-linux.sh --server http://avalon:8787 --token avm_… --name miami
sudo nano /etc/avalon-agent/agent.conf     # paste the block above
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
