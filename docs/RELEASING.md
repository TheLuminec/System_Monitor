# Releasing and deploying

A release goes: change + regression test → `tests/run.sh` → commit and push to `main` → sign that pushed commit's
agent files → commit and push the `.sig` files → `deploy/deploy-hub.sh <ref>` on AVALON. The hub restarts on the new code and every agent updates itself from it on
its next report.

## Versions

| What | Where | Bump when |
|------|-------|-----------|
| hub | `__version__` in `server/avalon_monitor/__init__.py` | every deploy (it busts the browsers' cached JS; deploy-hub warns when it is unchanged and refuses a lower one) |
| agent | `AGENT_VERSION` in `agent/avalon_agent.py` | the agent changes |
| avm | `AVM_VERSION` in `agent/avm.py` | avm changes |

Agents don't compare version numbers. They update whenever the sha256 of the script the hub serves differs from
their own, and only if the new script is signed.

## Tests

One entry point, the same one deploy-hub.sh runs:

```bash
tests/run.sh
```

It runs `pytest tests/`, `bash -n` on every `*.sh`, `node --check` on the dashboard JS and `py_compile` on the agent
and the backup scripts. It runs every check, then exits non-zero if any failed. It needs the repo's `.venv` (or
`AVM_TEST_PYTHON=/path/to/python`) and `node`. `AVM_TEST_SKIP_PYTEST=1` runs only the static checks (seconds). Create the venv once per checkout:

```bash
python3 -m venv .venv && .venv/bin/pip install -r server/requirements.txt -r agent/requirements.txt -c tests/constraints-prod.txt pytest httpx
```

`tests/constraints-prod.txt` pins the versions the hub runs in production. A single file: `.venv/bin/python -m pytest tests/test_auth.py -q`.

| File | Covers |
|------|--------|
| `test_auth.py` | read-only trusted network, `AVM_OPERATORS`, Host allowlist, Origin checks on writes and on `/api/v1/live`; every write route is checked against the operator rule |
| `test_agent.py` | agent local policy (job-mode cap, actions, root refusal), signed self-update, content roots, transcript levels, remote config surviving a restart, prompt passed after `--` |
| `test_avm.py` | `avm` against a real hub on a loopback port: run/watch/task, untracked runs when the hub is down, exit codes and signals, task-log switches, the queue dir's safety rules (an unsafe dir never stops a job: it runs without admission, F-07), a second user's job on a shared host, a shared `--gpu` job beside an `--exclusive` one, the `--beat` help vs the hub's stall rule |
| `test_checks.py` | check state across agent restarts, partial `PUT .../checks`, `manage.py checks`, adoption, responder guardrails (rolling cap, held alerts, fork fallback) |
| `test_db.py` | rollups with late samples, beats reviving lost tasks, one-time legacy check import |
| `test_backup.py` | `restic-backup.py` against throwaway restic repos (skipped without `restic`): failed on stop/crash, no false "failed" after a saved snapshot, partial, a missing source path keeps `last_success`, seed |
| `test_deploy_hub.py` | `deploy-hub.sh` against a temp repo, a fake `systemctl`/`git`/`rsync` and a fake hub: origin/main gate (fetch failing or hanging), downgrade refusal, test gate (timeout, one retry of failed tests, static checks under `--skip-tests`), rollback and its provenance, agent/ only after a healthy restart and `.sig` before code, the stale backup-avm warning |
| `test_release.py` | the agent files carry valid release signatures |
| `test_sign_release.py` | `sign-release.py` against a temp repo with a bare origin and a temp key: signs only a pushed commit's bytes, refuses a dirty `agent/`, an unpushed commit, a tampered object store, a non-isolated / venv interpreter; its inlined release format equals the agent's |
| `test_ui.py` | dashboard logic run with node (read-only mode, per-host prompt caps, escaping) and Windows installer ACLs |

**Every fix lands with a regression test** that fails without it. Name the audit note or the bug in the test's docstring.

## Signing a release

Agents (1.9+) install a self-update only if it carries an Ed25519 signature by a key they already trust:
`RELEASE_PUBKEYS` in `agent/avalon_agent.py`, plus any `AVM_UPDATE_PUBKEYS` in a host's `agent.conf` (added to the
built-in list, never replacing it). This covers `avalon_agent.py` and `avm.py`. The hub only passes the `.sig` /
`.sig2` files along and can't make one, so control of the hub or of `/opt/avalon-monitor` doesn't give control of the
fleet ([SECURITY.md](SECURITY.md#what-stays-trusted)).

**Two signatures per file (audit F-12).** `agent/<file>.sig2` (v2) covers the file's name, the version it declares
(`AGENT_VERSION` / `AVM_VERSION`, read from the signed bytes) and its sha256: `avalon-release:v2:<name>:<version>:<sha256>`.
Agents 1.10+ accept only this, and refuse a release whose version is lower than the one installed (avm.py: lower than
the `avm.py` on disk), so whoever controls the hub can't replay an older signed release onto them. Equal versions are
accepted (a rebuild under the same number). A deliberate rollback needs `AVM_ALLOW_DOWNGRADE=1` in that host's own
`agent.conf`; the hub can't set it. `agent/<file>.sig` (v1: name and sha256 only) is still written because agents
<= 1.9.x verify nothing else and must be able to take the release that upgrades them. The hub sends v1 as
`sig`/`file_sigs` and v2 as `sig2`/`file_sigs2`. Once `avalon-monitor-manage agents` shows no host below 1.10, v1 can
go: drop `release_message_v1` from `SIGNATURES` in the signer, the `.sig` files and the `sig`/`file_sigs` offer fields.
Until then a 1.9.x host can still be handed an older v1-signed release (that is the hole v2 closes), so upgrade them.

Each agent file must declare its version exactly once as dotted integers (`AGENT_VERSION = "1.10.0"`); the signer
refuses otherwise. Bump the version with every agent release: equal versions are accepted, so an earlier signed build
under the same number could still be replayed. Agents compare versions numerically (1.10 > 1.9).

`deploy/sign-release.py` signs a **pushed commit**, never the working tree (audit F-02). Run the copy of the script
that is on `origin/main`, with the system Python, from the checkout:

```bash
git fetch origin && git show origin/main:deploy/sign-release.py | /usr/bin/python3 -I -                # sign origin/main
git fetch origin && git show origin/main:deploy/sign-release.py | /usr/bin/python3 -I - --commit <sha> # a commit on origin/main
git fetch origin && git show origin/main:deploy/sign-release.py | /usr/bin/python3 -I - --init         # once: create the key
.venv/bin/python deploy/sign-release.py --check [--commit <ref>]   # verify .sig and .sig2 (no passphrase, any Python)
```

`--repo <path>` names the checkout when you run it from elsewhere (default: the git toplevel of the current
directory). The system Python needs apt's `python3-cryptography` (41.x on Ubuntu 24.04). Not the repo's `.venv`: the
script refuses to touch the key unless it runs isolated (`-I`), outside a virtualenv, with the interpreter,
`sys.path` and `cryptography` outside the repository and `$HOME` and root-owned. When it refuses, it prints the exact
command above. Piping the committed copy keeps a modified `deploy/sign-release.py` in the checkout from running;
`-I` keeps `PYTHON*` variables, the user site and the current directory off `sys.path`; and the signer never imports
the agent (it carries its own copy of the release format, which `tests/test_sign_release.py` holds byte-identical to
`release_version` / `release_message` in `avalon_agent.py`).

A signing run:

1. Fetches `main` from `git@github.com:TheLuminec/avalon-monitor.git` (`ORIGIN_URL` in the script; `--origin-url`
   overrides it) into a fresh scratch repository in a private temp directory, removed afterwards (no prompts, 60 s
   timeout; a failed fetch refuses). The checkout's own `.git/config` (its remotes, `url.*.insteadOf`, `core.sshCommand`,
   hooks) plays no part in the fetch (audit G-05). Without `--commit` it signs that fetched `main`; `--commit` is
   resolved in the checkout (then in the scratch repository) and refused unless it is on the fetched `main`.
2. Reads `agent/avalon_agent.py` and `agent/avm.py` from that commit in the scratch repository, re-hashing every git
   object from the commit down to each file itself. `RELEASE_PUBKEYS` and the versions are read from that text with a
   regex; a file without exactly one dotted-integer version line is refused.
3. Refuses if any `agent/*.py` in the working tree differs from the commit. The `.sig` commit you make next must
   carry exactly the signed bytes.
4. Prints, **before asking for the passphrase**: the full commit sha and subject, the URL it fetched, the agent and
   avm versions, each file's sha256, and the `agent/` diffstat and commits since the last signed release (the last
   commit that changed `agent/*.sig` or `agent/*.sig2`).
5. **Compare that sha with the head of `main` on GitHub** (on another device if you can) before typing the
   passphrase. Ctrl-C stops without signing.
6. Signs, checks each signature, writes `agent/*.sig` (v1) and `agent/*.sig2` (v2), and reminds you to commit and
   push them.

Then `git add agent/*.sig agent/*.sig2 && git commit -m "Release signatures ..." && git push`, and deploy.

* `--init` writes the private key, passphrase-encrypted, to `~/.config/avalon-release/release-key.pem` (or
  `$AVM_RELEASE_KEY`) and the public key beside it as `release-key.pub`, and prints the public key for
  `RELEASE_PUBKEYS`. It never overwrites an existing key. Back up the key and its passphrase somewhere other than AVALON.
* A signing run warns if the key isn't in the commit's `RELEASE_PUBKEYS`: agents running that release would refuse the next one.
* `tests/test_release.py` fails while a `.sig` or `.sig2` is missing or stale, so deploy-hub.sh won't ship an
  unsigned agent change.

Sign last, after the final change to either agent file is pushed: any change makes the signature stale. What this
protects against, and what it doesn't, is in [SECURITY.md](SECURITY.md#what-stays-trusted).

### Rotating the release key

A new key reaches the fleet in a release signed by the old key:

1. Create the new key under another path (`-I` ignores only `PYTHON*` variables, so `AVM_RELEASE_KEY` still applies):
   `git show origin/main:deploy/sign-release.py | AVM_RELEASE_KEY=~/.config/avalon-release/release-key-2.pem /usr/bin/python3 -I - --init`.
2. Add its public key to `RELEASE_PUBKEYS` and keep the old one there too. Push, sign with the **old** key and deploy.
3. Wait until `avalon-monitor-manage agents` shows every host up to date. A host that is offline now still trusts
   only the old key, so it needs the old key in the list when it comes back.
4. From then on sign with the new key (`... | AVM_RELEASE_KEY=...release-key-2.pem /usr/bin/python3 -I -`). Drop the old key from
   `RELEASE_PUBKEYS` in a later release.

`AVM_UPDATE_PUBKEYS` only adds keys, so a rotation shipped in a release reaches every host.

## Deploying: `deploy/deploy-hub.sh` (routine, no sudo)

```bash
deploy/deploy-hub.sh main            # or a tag / sha that is on origin/main
deploy/deploy-hub.sh --status        # live version, deployed commit, last log lines
deploy/deploy-hub.sh --rollback      # put back what was live before the last deploy (agent/ too)
```

`--rollback` restores the copy taken before the last **successful** deploy, server/ first: restart, health check,
and only then `agent/`, like a deploy. It refuses (unless `--rollback --force`) when that copy doesn't belong to what is
live now: the last deploy-hub.sh deploy isn't the one it undoes (it was already rolled back), or the version on disk or
on `/healthz` isn't that deploy's (a `sudo upgrade.sh` since). A deploy that fails, or is interrupted, keeps the
previous copy, so `--rollback` still undoes the last good deploy. Redeploying the commit that is already live (the
re-run after a failed `agent/` copy) keeps it too. A copy that holds the same commit it undoes (left by an older
deploy-hub.sh) is refused even with `--force`. Like a deploy, `--rollback` smoke-tests the restored hub before it puts
`agent/` back. `--status` shows what the copy holds.

Rolling back the agent: agents 1.10+ refuse a release older than the one they run (audit F-12). After a `--rollback`
that put back an older `agent/`, hosts already on the newer agent stay on it and show a self-update error until a
release with a higher agent version ships (`--rollback` prints a note). To take one host down on purpose, set
`AVM_ALLOW_DOWNGRADE=1` in that host's own `agent.conf`. Usually the better way back for the fleet is a new release: the
old code under a new, higher version number, signed and deployed as usual.

What it does, in order:

1. Fetches `origin` and refuses a commit that isn't on `origin/main`. Unpushed work, side branches and a stale local
   `main` are never deployed. The fetch never prompts and gets 30 s; if it fails, the check uses the last fetched
   `origin/main` (with a warning).
2. Takes the commit with `git archive`, so only committed code ships, never the working tree.
3. Refuses (and points to `sudo ./deploy/upgrade.sh`) when the release needs root: a changed
   `server/requirements.txt`, a changed systemd unit, or a `server/` dir it can't write.
4. Refuses a lower `__version__` than the deployed one unless `--force` (with a downgrade, every host would
   self-update down too). For a mistake, `--rollback` is the usual way back.
5. Runs `tests/run.sh` on exactly that tree, with the repo's `.venv`, for at most 600 s. A failure stops the deploy
   (full output in `~/.local/state/avalon-monitor-deploy/tests.log`). When only pytest failed, the files of the failed
   tests get one retry first, whole, so a test that fails because of an earlier test in its file fails again. A pass
   then is logged as flaky in `deploy.log` and the deploy ends with a `FLAKY:` line on stdout. Both attempts are in
   `tests.log`. A timeout or a failed static check is not retried. A checkout without `.venv` fails this gate: create
   it as in [Tests](#tests). `--skip-tests` skips pytest, loudly, and is logged; the static checks (`bash -n`,
   `node --check`, `py_compile`) still run.
6. Copies `server/` into `/opt/avalon-monitor`, restarts the service (allowed without sudo by
   `deploy/polkit/50-avalon-monitor-deploy.rules`), waits for `/healthz` to report the new version, and smoke-tests
   `GET /`, `/api/v1/hosts` and `/api/v1/stats` from localhost. If any of that fails, it restores the previous
   `server/` and restarts it. A copy that fails part-way is restored too, so the code on disk is never a mix of old and new.
   An interrupt in that window (Ctrl-C, SIGTERM, a dropped ssh session) does the same: the previous `server/` goes
   back and is restarted, and the deployed commit and the rollback copy stay as they were.
7. Only then copies `agent/`, if `/opt/avalon-monitor/agent` is writable by you. The hub serves it to every host on
   its next report, so a release that gets rolled back never reaches the fleet. The `.sig` and `.sig2` files go in before
   the code, so the hub never serves new code with an old signature. A failed agent copy is restored.
8. Warns when the backup service's own root-owned copy of avm (`/usr/local/lib/avalon-backup/avm.py`, from
   `deploy/backup/install-restic-backup.sh`) has another `AVM_VERSION` than this release, or when its copy of the
   runner (`/usr/local/sbin/<unit>`, the installed config's `unit`) differs from `deploy/backup/restic-backup.py`.
   Deploys can't refresh them and they don't self-update. Refresh it from a checkout of the deployed commit with
   `sudo deploy/backup/install-restic-backup.sh /etc/avalon-backup/config.json`: that re-reads the installed config,
   keeps the repository, password, schedule and the last run's status, and only re-installs the runner, the config,
   avm and the units (no `--now`, so no backup starts).

One-time setup: the polkit rule
(`sudo install -m 644 deploy/polkit/50-avalon-monitor-deploy.rules /etc/polkit-1/rules.d/`; it names the operator's
user), `/opt/avalon-monitor/server` (and optionally `agent/`) owned by that user, and the `.venv`. State and the
rollback copy live in `~/.local/state/avalon-monitor-deploy/`.

## With sudo: `upgrade.sh` / `install-server.sh`

```bash
sudo ./deploy/upgrade.sh                    # runs install-server.sh; for new dependencies or a changed unit
sudo ./deploy/upgrade.sh --rootless-agent   # ...and move AVALON's own agent to a systemd --user install
sudo ./deploy/install-server.sh             # first install, or the same upgrade without the agent step
```

`install-server.sh` is the installer, not a release tool. It rsyncs the **working tree** (uncommitted and untracked
files included) into `/opt/avalon-monitor`, reinstalls the Python dependencies and restarts the service. It doesn't
check origin/main or the version, runs no tests, has no health check and no rollback, and it copies `agent/` at the
same time as `server/`. So the fleet sees the new agent before the hub is known to work. Use it only from a clean
checkout of a pushed commit (`git status` empty), run `tests/run.sh` first, and check the hub afterwards
(`curl -s http://127.0.0.1:8787/healthz`, the dashboard). Agents still refuse unsigned agent code.

`upgrade.sh` then tells deploy-hub.sh what it installed (`deploy-hub.sh --record-install`, run as `$SUDO_USER`). That
is the checkout's HEAD when `server/`, `agent/` and the unit file are clean and the installed version is the
checkout's; otherwise the deployed commit becomes unknown. So `--status` and the next deploy's rollback copy name the
right commit, and an older rollback copy no longer matches what is live (`--rollback` refuses it).

## After a deploy

```bash
avalon-monitor-manage agents     # hub's agent version vs each host; self-update errors
```

Each agent downloads the new script on its next report, checks the sha256 the hub announced, the release signature
and that it compiles, replaces itself and re-execs. `avm.py` follows the same way. A refused agent update shows on the
host card as a self-update error (e.g. "bad signature", "the hub sent no signature") and the agent keeps running its
current code; a refused `avm.py` only goes to the agent's log. To pause updates:
`AVM_AGENT_AUTO_UPDATE=false` on the hub (fleet-wide) or `AVM_AUTO_UPDATE=false` in one host's `agent.conf`.
