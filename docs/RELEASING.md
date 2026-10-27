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
`AVM_TEST_PYTHON=/path/to/python`) and `node`. Create the venv once per checkout:

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
| `test_backup.py` | `restic-backup.py` against throwaway restic repos (skipped without `restic`): failed on stop/crash, partial, seed |
| `test_deploy_hub.py` | `deploy-hub.sh` against a temp repo, a fake `systemctl` and a fake hub: origin/main gate, downgrade refusal, test gate, rollback, agent/ only after a healthy restart |
| `test_release.py` | the agent files carry valid release signatures |
| `test_sign_release.py` | `sign-release.py` against a temp repo with a bare origin and a temp key: signs only a pushed commit's bytes, refuses a dirty `agent/`, an unpushed commit, a tampered object store, a non-isolated / venv interpreter; its inlined release format equals the agent's |
| `test_ui.py` | dashboard logic run with node (read-only mode, per-host prompt caps, escaping) and Windows installer ACLs |

**Every fix lands with a regression test** that fails without it. Name the audit note or the bug in the test's docstring.

## Signing a release

Agents (1.9+) install a self-update only if it carries an Ed25519 signature by a key they already trust:
`RELEASE_PUBKEYS` in `agent/avalon_agent.py`, plus any `AVM_UPDATE_PUBKEYS` in a host's `agent.conf` (added to the
built-in list, never replacing it). This covers `avalon_agent.py` and `avm.py`. The hub only passes the `.sig` files along and can't make
one, so control of the hub or of `/opt/avalon-monitor` doesn't give control of the fleet
([SECURITY.md](SECURITY.md#what-stays-trusted)).

`deploy/sign-release.py` signs a **pushed commit**, never the working tree (audit F-02). Run the copy of the script
that is on `origin/main`, with the system Python, from the checkout:

```bash
git fetch origin && git show origin/main:deploy/sign-release.py | /usr/bin/python3 -I -                # sign origin/main
git fetch origin && git show origin/main:deploy/sign-release.py | /usr/bin/python3 -I - --commit <sha> # a commit on origin/main
git fetch origin && git show origin/main:deploy/sign-release.py | /usr/bin/python3 -I - --init         # once: create the key
.venv/bin/python deploy/sign-release.py --check [--commit <ref>]   # verify signatures (no passphrase, any Python)
```

`--repo <path>` names the checkout when you run it from elsewhere (default: the git toplevel of the current
directory). The system Python needs apt's `python3-cryptography` (41.x on Ubuntu 24.04). Not the repo's `.venv`: the
script refuses to touch the key unless it runs isolated (`-I`), outside a virtualenv, with the interpreter,
`sys.path` and `cryptography` outside the repository and `$HOME` and root-owned. When it refuses, it prints the exact
command above. Piping the committed copy keeps a modified `deploy/sign-release.py` in the checkout from running;
`-I` keeps `PYTHON*` variables, the user site and the current directory off `sys.path`; and the signer never imports
the agent (it carries its own copy of the release format, which `tests/test_sign_release.py` holds byte-identical to
`release_message` in `avalon_agent.py`).

A signing run:

1. `git fetch origin` (no prompts, 60 s timeout; a failed fetch refuses), resolves `--commit` (default `origin/main`)
   and refuses a commit that isn't on `origin/main`.
2. Reads `agent/avalon_agent.py` and `agent/avm.py` from that commit, re-hashing every git object from the commit
   down to each file itself, so a rewritten object in `.git/objects` is refused too. `RELEASE_PUBKEYS` and the
   versions are read from that text with a regex.
3. Refuses if any `agent/*.py` in the working tree differs from the commit. The `.sig` commit you make next must
   carry exactly the signed bytes.
4. Prints, **before asking for the passphrase**: the full commit sha and subject, `origin`'s URL, the agent and avm
   versions, each file's sha256, and the `agent/` diffstat and commits since the last signed release (the last commit
   that changed `agent/*.sig`).
5. **Compare that sha with the head of `main` on GitHub** (on another device if you can) before typing the
   passphrase. Ctrl-C stops without signing.
6. Signs, checks each signature, writes `agent/*.sig`, and reminds you to commit and push them.

Then `git add agent/*.sig && git commit -m "Release signatures ..." && git push`, and deploy.

* `--init` writes the private key, passphrase-encrypted, to `~/.config/avalon-release/release-key.pem` (or
  `$AVM_RELEASE_KEY`) and the public key beside it as `release-key.pub`, and prints the public key for
  `RELEASE_PUBKEYS`. It never overwrites an existing key. Back up the key and its passphrase somewhere other than AVALON.
* A signing run warns if the key isn't in the commit's `RELEASE_PUBKEYS`: agents running that release would refuse the next one.
* `tests/test_release.py` fails while a signature is missing or stale, so deploy-hub.sh won't ship an unsigned
  agent change.

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

What it does, in order:

1. Fetches `origin` and refuses a commit that isn't on `origin/main`. Unpushed work, side branches and a stale local
   `main` are never deployed.
2. Takes the commit with `git archive`, so only committed code ships, never the working tree.
3. Refuses (and points to `sudo ./deploy/upgrade.sh`) when the release needs root: a changed
   `server/requirements.txt`, a changed systemd unit, or a `server/` dir it can't write.
4. Refuses a lower `__version__` than the deployed one unless `--force` (with a downgrade, every host would
   self-update down too). For a mistake, `--rollback` is the usual way back.
5. Runs `tests/run.sh` on exactly that tree, with the repo's `.venv`. A failure stops the deploy (full output in
   `~/.local/state/avalon-monitor-deploy/tests.log`). A checkout without `.venv` fails this gate: create it as in
   [Tests](#tests). `--skip-tests` skips it, loudly, and is logged.
6. Copies `server/` into `/opt/avalon-monitor`, restarts the service (allowed without sudo by
   `deploy/polkit/50-avalon-monitor-deploy.rules`), waits for `/healthz` to report the new version, and smoke-tests
   `GET /`, `/api/v1/hosts` and `/api/v1/stats` from localhost. If any of that fails, it restores the previous
   `server/` and restarts it. A copy that fails part-way is restored too, so the code on disk is never a mix of old and new.
7. Only then copies `agent/`, if `/opt/avalon-monitor/agent` is writable by you. The hub serves it to every host on
   its next report, so a release that gets rolled back never reaches the fleet. A failed agent copy is restored.

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

## After a deploy

```bash
avalon-monitor-manage agents     # hub's agent version vs each host; self-update errors
```

Each agent downloads the new script on its next report, checks the sha256 the hub announced, the release signature
and that it compiles, replaces itself and re-execs. `avm.py` follows the same way. A refused agent update shows on the
host card as a self-update error (e.g. "bad signature", "the hub sent no signature") and the agent keeps running its
current code; a refused `avm.py` only goes to the agent's log. To pause updates:
`AVM_AGENT_AUTO_UPDATE=false` on the hub (fleet-wide) or `AVM_AUTO_UPDATE=false` in one host's `agent.conf`.
