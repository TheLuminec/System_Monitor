# Security model

Who can see what, who can change what, and what the hub and the agents refuse. Code: `server/avalon_monitor/auth.py`
(Access JWT, networks), `core.py` (Host allowlist, Origin checks, operator rule), `main.py` / `routes_*.py` (which rule
each route uses), `agent/avalon_agent.py` (local policy, release signatures).

## Who may do what

| Caller | How the hub knows | May |
|--------|-------------------|-----|
| **Viewer on a trusted network** (tailnet, localhost) | direct connection (no Cloudflare headers) from `AVM_TRUSTED_NETWORKS`, `AVM_ALLOW_TRUSTED_NO_AUTH=true` | read everything; save its own layout prefs. **Read-only**: every change is refused with 403 |
| **Access user** | valid Cloudflare Access JWT (RS256, `aud`/`iss`/`exp` checked) | read everything |
| **Operator** | Access user, and on `AVM_OPERATORS` when that list is set (empty = anyone Access admits) | change things: prompts, session restart/stop, checks and host settings, respawn, acknowledging and deleting tasks/prompts |
| **Agent** | per-host bearer token, from `AVM_INGEST_NETWORKS`, never through Cloudflare | report samples, tasks, results and its own checks; fetch its update (scoped to its own host) |
| anyone | - | `GET /healthz` and `/static/*` |

Changes need an Access identity because "trusted network" covers more than you: every tailnet device, every local
user and process on AVALON, and every Claude session running there. In practice you make changes at the public URL
(`AVM_PUBLIC_URL`); the tailnet address shows the same dashboard read-only, and a refused change names the public URL.

`AVM_TRUSTED_WRITE=true` restores the old behaviour (trusted-network viewers may change things). Use it only on a hub
without Access, on a network where everyone who can reach `:8787` may run prompts on your machines.

Every route and the rule it uses: [README → API](../README.md#api).

## Request guards

* **Host allowlist** (all HTTP and WebSocket requests, before anything else). Accepted `Host` values: IP literals,
  `localhost`, the machine's hostname, `<hostname>.<tailnet>.ts.net`, the host of `AVM_PUBLIC_URL`, and anything in
  `AVM_ALLOWED_HOSTS`. Anything else gets `421 unexpected Host header` (WebSocket: close code 4421). Requests carrying
  Cloudflare's `cf-*` headers pass this check, but such a request never gets trusted-network access: it needs a valid
  Access JWT. If an agent or your browser uses another name for AVALON (a DNS alias, a Tailscale machine name that
  differs from the OS hostname), add it to `AVM_ALLOWED_HOSTS`.
* **Writes** (every POST/PUT/PATCH/DELETE the dashboard makes) need `X-Requested-With: avalon-monitor`, an `Origin`
  that matches the request's own `Host` (or none, for non-browser clients; `Origin: null` is refused), and a
  `Sec-Fetch-Site` of `same-origin` or `none` when the browser sends one. Otherwise 403.
* **The live WebSocket** (`/api/v1/live`) checks the same Origin / `Sec-Fetch-Site` rule before it authenticates;
  a cross-site socket is closed with 4403, an unauthenticated one with 4401.
* **Agent routes** refuse anything with Cloudflare headers and anything from outside `AVM_INGEST_NETWORKS`; a token
  only reaches its own host's rows. The hub never trusts `X-Forwarded-For` (uvicorn runs with `proxy_headers=False`).

## Threat model: why these guards

* **DNS rebinding.** A web page opened in any browser on a tailnet device can point its own hostname at AVALON's
  tailnet address. The browser then treats the hub as same-origin with the attacker's page, and the hub sees a request
  from a trusted IP. The page's requests still carry the attacker's hostname in `Host`, which the allowlist refuses.
  IP literals are allowed because a page can't rebind an IP: its origin would be the IP itself.
* **CSRF.** A cross-site page can make a browser send a simple POST to `http://avalon:8787`. It can't add
  `X-Requested-With` without a CORS preflight (which the hub never approves), and the Origin / `Sec-Fetch-Site` check
  refuses it as well. Since trusted-network viewers are read-only, a forged write from the tailnet would be refused
  even without these checks.
* **Cross-site WebSocket hijacking.** Browsers don't apply CORS to WebSockets, and the tailnet needs no cookie. Without
  the Origin check, any `http://` page in a tailnet browser could open `ws://avalon:8787/api/v1/live` and read
  snapshots, check details and live prompt transcripts.

## What stays trusted

* **Cloudflare Access** and the Access policy decide who is an operator (with `AVM_OPERATORS` as a second filter).
  Keep the policy tight.
* **The hub's code directory.** `/opt/avalon-monitor/server` and `/opt/avalon-monitor/agent` are owned by the
  operator's own user, so `deploy/deploy-hub.sh` can deploy without sudo (audit E-23). So the dedicated
  `avalon-monitor` service user is **not** an integrity boundary: any process running as the operator, including every
  Claude session with a shell on AVALON, can rewrite the hub. Such a process can change what the dashboard shows,
  read the hub's database, and queue prompts, session restarts and check changes. Agents still enforce their own
  limits on all of these (next section).
  **Release signing keeps it from reaching the fleet:** agents install only agent code signed with the release key
  ([RELEASING.md](RELEASING.md#signing-a-release)). The hub's code can't sign: it never sees the key. But the key is
  **not** off AVALON: it lives in the operator's own account (`~/.config/avalon-release/`), the same account the hub
  deploys from and every Claude session can act as. What protects it is the passphrase, which only the owner knows,
  and how the signing is done (audit F-02):
  * **What this stops.** Signing anything other than the reviewed, pushed commit: `sign-release.py` reads the files
    from a commit on `origin/main` (re-hashing the git objects itself), refuses a working tree that differs from it,
    and prints the commit sha, the file sha256s and the `agent/` diffstat since the last release before it asks for
    the passphrase, so the owner can compare the sha with GitHub. A process that edits `agent/*.py` in the checkout,
    or rewrites `.git/objects`, just before a signing run gets a refusal, not a signature. And the passphrase isn't
    typed into code from the user-writable checkout: the documented command runs the copy of the script on
    `origin/main` with `/usr/bin/python3 -I` and the root-owned system `cryptography`, and the script refuses to touch
    the key from a virtualenv, a non-isolated interpreter, or with anything on `sys.path` inside the repo or `$HOME`.
    So a tampered `.venv`, `sign-release.py` or agent module can't capture the passphrase.
  * **What remains possible.** A process running as the operator can still keylog the terminal the passphrase is typed
    into (an X11 or terminal-emulator hook, a modified shell rc, a `git` wrapper earlier on `PATH`, a forged
    `origin/main` copy of the script itself if the owner skips the sha comparison), or copy the encrypted key and wait
    for the passphrase. With the passphrase and the key file, it can sign anything. The passphrase keeps the key
    safe from offline theft and from a session that doesn't see it typed; it is not a boundary against a live
    compromise of the operator account at signing time. Signing on another machine, or with a hardware key, would be;
    the owner chose to keep signing on AVALON.

  To make `agent/` root-only as well, `chown` it to root (deploy-hub then deploys the hub only, and agent releases go
  through `sudo ./deploy/upgrade.sh`).
* **Loopback is a trusted network by default.** Any local process can read the whole dashboard (not change it).
  `deploy-hub.sh` relies on this for its smoke test (`GET /`, `/api/v1/hosts`, `/api/v1/stats` from 127.0.0.1).
* **Tailscale ACLs and `ufw`** decide who can reach `:8787` at all. The hub binds `0.0.0.0` by default
  (`AVM_BIND_HOST`); keep the port closed on the LAN (see [CLOUDFLARE.md](CLOUDFLARE.md#5-keep-the-origin-private)).

## Agent local policy

Each agent enforces limits that only its own `agent.conf` (or environment) sets. They are not remote keys, so the
hub, the dashboard and a modified hub can't raise them:

| Setting | Default | Effect |
|---------|---------|--------|
| `AVM_MAX_JOB_MODE` | `plan` | highest permission mode a dashboard prompt may use here: `plan`, `acceptEdits` or `full`. A higher request is refused, not downgraded |
| `AVM_ALLOW_ACTIONS` | `true` | allow restart/stop of tracked Claude sessions from the dashboard |
| `AVM_ALLOW_ROOT_JOBS` | `false` | an agent running as root (Linux) or SYSTEM (Windows) refuses all prompts, responder alerts and actions unless this is on. A Windows agent installed as SYSTEM (e.g. LAPTOP-C) is in this state; use the rootless / `-AsCurrentUser` install for prompts |
| `AVM_JOB_TRANSCRIPT` | `full` | what a prompt's transcript sends to the hub: `full`, `summary` (final answer and tool names only) or `off` (status and metadata only) |
| `AVM_SESSION_SNIPPETS` | `auto` | first/last lines of every Claude session on the host and claude processes' arguments in each report: `on`, `off`, or `auto` (on only when `AVM_JOB_TRANSCRIPT=full`) |
| `AVM_CONTENT_ROOTS` | none | a content check sends a file's first line to the hub. Targets listed in `agent.conf` itself are always allowed; a hub-added target must resolve (symlinks followed) inside one of these directories, or it reports "refused" |
| `AVM_UPDATE_PUBKEYS` | built-in `RELEASE_PUBKEYS` | extra release keys a self-update may be signed with (added to the built-in list) |
| `AVM_STATE_DIR` | beside the config file, else beside the agent script | where `remote-config.json` (the last settings the hub pushed) is kept, so checks survive a restart |

`AVM_AUTO_UPDATE=false` stops self-updates on a host entirely.

The hub may set only `AVM_WATCH_*`, `AVM_MEM_AVAILABLE_WARN_GB`, `AVM_PROCESSES` and `AVM_TAGS` on an agent. Responder
alerts from checks are always plan-mode prompts and never include a content check's file line.

## Hosts with data-use agreements

What can leave a host and end up in the hub's database (which AVALON's nightly backup keeps for months): `avm` task
output tails, command lines and working directories; prompt transcripts (tool inputs and results); content-check
first lines; and, for checks created with `--with-log`, task log tails inside responder prompts. The local switches that
stop each of these are in [HOSTS.md → Hosts with data-use agreements](HOSTS.md#hosts-with-data-use-agreements).
