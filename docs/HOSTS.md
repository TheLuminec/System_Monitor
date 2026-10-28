# Per-host agent configuration

Ready-to-use `agent.conf` contents for the current fleet. Tokens come from
`avalon-monitor-manage add-host <name>` on AVALON. Every key, with its default, is in
[`agent/agent.conf.example`](../agent/agent.conf.example).

> **The hub owns the check lists.** On its first report with checks (agent 1.7+), the hub adopts a host's
> `AVM_WATCH_*` / `AVM_MEM_AVAILABLE_WARN_GB` entries as check rows and from then on pushes every list in each reply,
> empty ones included. Editing those lines in `agent.conf` afterwards has **no effect**. Change checks on the host
> page (**Checks**), with `avm check add/rm` on the host, or with
> `avalon-monitor-manage checks <host> --set AVM_WATCH_TCP=127.0.0.1:11434` (only the keys given change). The
> `AVM_WATCH_*` lines below are what seeds a freshly enrolled host.
>
> **Local policy stays local.** `AVM_MAX_JOB_MODE` (default `plan`), `AVM_ALLOW_ACTIONS`, `AVM_ALLOW_ROOT_JOBS`,
> `AVM_JOB_TRANSCRIPT`, `AVM_SESSION_SNIPPETS`, `AVM_TASK_LOG_OFF_DIRS`, `AVM_CONTENT_ROOTS`, `AVM_UPDATE_PUBKEYS`,
> `AVM_ALLOW_DOWNGRADE` and `AVM_STATE_DIR` are read only from the host's own
> `agent.conf` (or the agent's environment); the hub can't set them ([SECURITY.md](SECURITY.md#agent-local-policy)). An agent running as root or
> SYSTEM refuses dashboard prompts, responder alerts and session restart/stop unless `AVM_ALLOW_ROOT_JOBS=true`.
>
> **Alert mode.** A check's responder alert is a `plan` prompt unless an operator sets the check's *Alert mode*
> (Checks panel, or `PATCH /api/v1/checklist/<id>` with `{"alert_mode": "auto"}`; modes rank `plan` < `acceptEdits` <
> `auto` < `full`, and `full` also needs
> `"confirm_full": true`). It is still capped by the host's own `AVM_MAX_JOB_MODE`: a host that allows less holds
> the alert (the reason shows on the check) instead of sending it in a lower mode, so raise both for it to fire.
> `avm check add/set` can't raise it (403), and on a check an operator raised it can't change the target, the
> responder or its working dir either (send `alert_mode=plan` to lower it first).

> **Interpreter isolation.** Both installers create a venv **owned by the agent**
> (`/opt/avalon-agent/venv` on Linux, `%ProgramData%\AvalonAgent\venv` on Windows)
> and install `psutil` only there. They never touch a project's virtualenv or the
> system site-packages. On Miami in particular, do **not** point the agent at the
> Python 3.13 pipeline env or the Python 3.8.20 reproduction env — their exact
> dependency sets are recorded in reproduction certificates. If you must avoid a
> new venv entirely, use the distro interpreter with `apt install python3-psutil`
> and edit `ExecStart=` in `avalon-agent.service` to `/usr/bin/python3`.

## Miami (Ubuntu, RTX 4060 Ti) — `~/.config/avalon-agent/agent.conf`

Jobs on Miami are launched one at a time by a launcher script inside a `systemd --user` scope `train-<job>.scope`
with a hard `MemoryMax`, and each writes `~/job_markers/<job>.done` containing `rc=… oom_kill=… peak_mb=…` on exit;
the old queue runner (`train-queue.service`) is stopped and disabled. The config below watches *that* design.
(Once the hub has adopted these checks, change them on the host page, not here: see the note at the top.)
Names and paths here are examples.

```ini
AVM_SERVER_URL=http://avalon:8787
AVM_TOKEN=avm_…
AVM_HOSTNAME=miami
AVM_INTERVAL=5
AVM_PROCESSES=8
AVM_TAGS=sm_89 via PTX compat (build arch list stops at 8.6/9.0; device is 8.9)

# what is running: active train-*.scope units show as a "job: <name>" chip and
# feed the hub's GPU-stall rule (job active >=10 min with GPU never above 5% -> red "gpu stalled")
AVM_WATCH_JOBS=user:train-*.scope
# job failure signal: a .done marker with a non-zero rc or oom_kill -> red chip with a count
AVM_WATCH_MARKERS=/home/<user>/job_markers/*.done::rc=(?!0\b)|oom_kill=(?!0\b)
# RAM pressure: MemAvailable is the number that matters (RSS lies); amber below 8 GB
AVM_MEM_AVAILABLE_WARN_GB=8
# the datasets live on a removable volume
AVM_WATCH_MOUNTS=/run/media/<user>/Data
# the retired queue runner: shows "train-queue: inactive" (grey); red only if the unit is failed
AVM_WATCH_UNITS=user:train-queue.service
```

Notes:
* Claude prompting from the Agents tab runs `claude -p` as the agent's user, so
  the rootless install is required for it on Miami (that user's Claude login). Add
  `AVM_CLAUDE=false` to opt the machine out.
* `user:` scopes query the login user's manager, which is why the **rootless
  install** below is the right one here (a root/system-unit agent would need
  `user@<user>:` prefixes and a recent systemd to reach it).
* `rc=137 oom_kill=1` in a marker means the memory cap did its job — the chip
  is still red because the *job* failed, which is the thing to notice.
* If the queue runner is ever re-enabled, it needs
  `RequiresMountsFor=/run/media/<user>/Data` in the unit (it raced the mount at
  boot). The agent can't check unit file contents; the chip only
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

A root agent refuses Claude prompts and actions (`AVM_ALLOW_ROOT_JOBS=false`). Its unit runs with
`ProtectSystem=strict`, so `/etc/avalon-agent` is read-only to it: it keeps `remote-config.json` beside the agent
script in `/opt/avalon-agent` instead (set `AVM_STATE_DIR` to choose another writable dir).

The root install also writes `/etc/tmpfiles.d/avm-queue.conf` (`d /run/avm-queue 1777 root root -`) and runs
`systemd-tmpfiles --create`, so `avm run --gpu/--wait` has a root-owned queue dir shared by every user, recreated at
each boot. Without it avm uses `/tmp/avm-queue`, which belongs to whoever queued first after a reboot; every other
user's queued jobs then still run, but **without admission control** (`queue disabled: ... owned by uid N` on stderr
and in the task's note). A host with only rootless (`--user`) installs and more than one user of the queue needs that
line once:

```bash
echo 'd /run/avm-queue 1777 root root -' | sudo tee /etc/tmpfiles.d/avm-queue.conf && sudo systemd-tmpfiles --create
```

### Windows agents and self-update (1.2.x → 1.3+)

Agents older than 1.3.1 running under `pythonw.exe` (the scheduled task) write
the new script to disk but fail to re-exec it (`'NoneType' object has no
attribute 'flush'` shows on the host card). The on-disk copy *is* already
current, so one restart loads it — either the next reboot (the task starts at
boot) or, in an elevated PowerShell:

```powershell
Restart-ScheduledTask AvalonMonitorAgent   # or: Stop-ScheduledTask ...; Start-ScheduledTask ...
```

Do this after the hub serves 1.3.1 or later, so the restarted agent is one
that can respawn on its own from then on.

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
elevated PowerShell. By default the Scheduled Task runs as SYSTEM at boot.

**Windows: SYSTEM vs. your user.** A SYSTEM install (the default; LAPTOP-C runs this way) reports metrics and
checks, but refuses prompts and session restart/stop from the dashboard (`AVM_ALLOW_ROOT_JOBS=false`). For prompts,
install with `-AsCurrentUser` (or switch with `.\install\install-windows.ps1 -Upgrade -AsCurrentUser`): the agent then
runs as you at logon, with your Claude login. Settings pushed by the hub are kept in
`C:\ProgramData\AvalonAgent\remote-config.json`.

## AVALON (this hub) — `~/.config/avalon-agent/agent.conf`

AVALON's agent is a **rootless user service** (`systemctl --user status avalon-agent`, linger enabled), not the root
system unit: prompts run as the operator, with their Claude login. `deploy/backup/install-restic-backup.sh` points the
backup unit's `avm` at this file (`AVM_CONFIG`, found via `$SUDO_USER`). To move an old root install over:
`sudo ./deploy/upgrade.sh --rootless-agent` (copies `/etc/avalon-agent/agent.conf`, disables the root unit). Fresh
install:

```bash
./agent/install/install-linux.sh --user --server http://127.0.0.1:8787 --token avm_… --name avalon
```

```ini
AVM_SERVER_URL=http://127.0.0.1:8787
AVM_TOKEN=avm_…
AVM_HOSTNAME=avalon
AVM_INTERVAL=5
AVM_TAGS=hub, data + coordination
# shared host: keep task output, session snippets and full prompt transcripts from data-use-agreement projects on
# this machine (see below)
#AVM_TASK_LOG_OFF_DIRS=<project dir>
```

Checks it was seeded with (now held by the hub: change them on AVALON's host page):

```ini
AVM_WATCH_PROCESSES=nginx,cloudflared,tailscaled
AVM_WATCH_TCP=127.0.0.1:11434
AVM_WATCH_MOUNTS=/mnt/DataHoarder
```

AVALON's GPU is a **GTX 1080 Ti** (Pascal), reported through `nvidia-smi` like any NVIDIA card.

## Raspberry Pi 5 / other Linux

The Linux installer is distro-agnostic (`python3` + `venv` module required;
`sudo apt install python3-venv` on Raspberry Pi OS). CPU temperature comes
from `cpu_thermal` / `thermal_zone0`; there is no GPU reporting on the Pi.

## Backups (Backups card, agent 1.8+)

A host shows a **Backups** card (host page), a backup chip on its Overview tile, and an Alerts entry when a backup
fails or nothing has succeeded within its `max_age_sec` (default 26 h) - whenever its agent finds a backup status file.
The agent reads `/var/lib/avalon-backup/status.json` (`C:\ProgramData\avalon-backup\status.json` on Windows), or the
comma list in `AVM_BACKUP_STATUS`.

Linux with restic: write a config like [`deploy/backup/avalon.json`](../deploy/backup/avalon.json) and run

```bash
sudo deploy/backup/install-restic-backup.sh deploy/backup/<host>.json [--now]
```

It installs restic, creates the repository and its password if they don't exist (the password is printed once, as
soon as it is created - keep it somewhere other than that machine), installs `deploy/backup/restic-backup.py` as a
nightly systemd timer wrapped in `avm run` (each run is a task on the dashboard; `TimeoutStartSec` is the config's
`max_run_sec`), and seeds the status file from an existing repository. The backup runs even when the hub is down
(`avm run` then runs it untracked); only the dashboard task is missing.

What the card shows, from the last run's `state`:

* **ok**: the snapshot has everything.
* **partial** (amber): a snapshot was saved, but restic skipped unreadable files or a missing source path (restic exit
  code 3), or a SQLite copy failed, so the snapshot holds the previous copy of that database. `error` says what is
  missing. Unreadable files or a failed SQLite copy still advance `last_success` (everything else in the snapshot is
  restorable). A missing source path does not, and the success stamp is not touched either: that path isn't backed up
  at all, so the stale alert goes red `max_age_sec` after the last run that had every path.
* **failed** (red): no snapshot, or the run was stopped (SIGTERM from `systemctl stop` or the timeout, SIGINT,
  SIGHUP) or crashed before its snapshot was saved and pruned. `restic-backup.py` writes `failed` before it exits. A
  stop after that point only skips the repository statistics; the run stays `ok`/`partial`.
* **running**: a run is in progress. A `running` state older than `max_run_sec` shows red: only SIGKILL or power
  loss can leave it behind.

Re-seeding: `sudo /usr/local/sbin/<unit> --seed` keeps a status written by a real run (`ok`, `partial`, `failed` or
`running`), so re-running the installer never turns a failure into "ok"; add `--force` to overwrite it.

Any other backup tool can feed the card by writing the same JSON (all keys optional except `name`; times are Unix
seconds, sizes bytes):

| key | meaning |
|---|---|
| `name`, `tool`, `repository`, `paths`, `keep` | what is backed up where, and the retention (`{"daily": 7, ...}`) |
| `state` | `running`, `ok`, `partial` (amber: a snapshot was saved, but without some files or source paths, or with an old copy of a database) or `failed` (the last run) |
| `started`, `finished`, `duration_sec`, `next_run` | the last run and the next scheduled one |
| `last_success`, `max_age_sec` | drives the chip colour and the stale alert (a `partial` run counts as a success here, unless a configured source path was missing) |
| `max_run_sec` | a `running` state older than this (default 43200 = the unit's 12 h `TimeoutStartSec`) shows red: the run was killed without writing its end |
| `snapshot`, `files`, `bytes_scanned`, `bytes_added`, `files_new`, `files_changed`, `warnings` | the last run's result (`warnings`: unreadable files + missing paths + failed SQLite copies) |
| `snapshots`, `repo_bytes`, `target_free_bytes`, `checked` | the repository: snapshots kept, size, free space on its disk, last verify |
| `error` | why the last run failed, or for `partial` what is missing (counts and the first few paths, with why) |

## Hosts with data-use agreements

For a host whose data must not leave it, add to its `agent.conf`:

```ini
# avm tasks send name, status, exit code, timing and note only: no output lines, no command line, no working dir
AVM_TASK_LOG=off
# prompt transcripts carry the final answer and tool names only: no tool inputs, tool results or intermediate text
AVM_JOB_TRANSCRIPT=summary
```

and **no** `AVM_CONTENT_ROOTS`, so the hub can't add a content check that reads a file there. With a transcript
level below `full`, session snippets (the first/last lines of every Claude session on the host, and claude
processes' arguments) stay on the host too (`AVM_SESSION_SNIPPETS=auto`).

**Hub-side safety net:** also set *Data-use host* (`AVM_DATA_USE=restricted`) in the host's settings on the
dashboard. The hub then drops task output lines, commands, working directories and notes (also the note and queue
reason inside a task's meta: only resources, queue timing and the code sha are kept), prompt tool inputs/results and
intermediate text, session snippets, tracked sessions' text (their working directory keeps only its last part) and
content-check text before storing anything - even if the agent.conf lines above are missing. A host whose agent
reports a transcript level below `full` (or session snippets off) is treated the same way.

The restriction is **sticky**. The first report with such a policy from a host whose *Data-use host* setting is
empty sets it to `restricted` for you (the hub logs it) and clears what the hub stored from the host before: task
command lines, working directories, notes and output, prompt transcripts (cut to tool names and final answers) and
tracked sessions' text. If the agent later reports `full` again - say its
agent.conf lost the lines above - the host stays restricted, and its card shows a failing **data-use policy**
check until the agent.conf is fixed. Only you can lift it: set *Data-use host* to `off` (`normal` and `none` work
too). The hub never overwrites an explicit `off`; while the agent itself still reports a restricted policy the host
stays restricted through that anyway. Clearing the field (empty) puts it back on automatic.

Notes:

* `summary` still sends the final answer, which could quote what Claude read. `AVM_JOB_TRANSCRIPT=off` keeps the
  answer on the host too (the hub sees status only).
* A content check listed in `agent.conf` itself still sends its file's first line: don't point one at agreement data.
* Don't create checks with `--with-log` there (it adds task log tails to responder alerts).
* `avm` takes the switch from every `agent.conf` it can read (`AVM_CONFIG`, `~/.config/avalon-agent/`,
  `/etc/avalon-agent/`). If other users run `avm` on the host, put it where they read it too, or have them use
  `avm run --no-log`.

On a shared host like AVALON, where only one project is covered, keep the log on and list that project instead:

```ini
AVM_TASK_LOG_OFF_DIRS=<project dir>
```

Runs whose working directory is inside it send no output, command line, cwd, `--note` text or queue reasons, and
pass `AVM_TASK_LOG=off` to their children so nested `avm` calls stay quiet too. `avm run --log` overrides this per
run. Use it only for runs that print nothing covered.

The agent applies the list to Claude too, and fails closed. A session sends no snippets, no name and no `claude`
process arguments when *any* directory its transcript recorded is inside the list (one started in `~` that later
works there counts), and also when its directory can't be read at all. A dashboard prompt run there sends at most a
summary transcript; one that moves there while it runs (a tool input naming a path in the list, or a cwd its
transcript records) drops to summary from that event on. Events already sent before that point stay on the hub.

Whatever does reach the hub lands in `monitor.db`, which AVALON's nightly backup keeps for months. See
[SECURITY.md](SECURITY.md#hosts-with-data-use-agreements).
