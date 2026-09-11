# Publishing the dashboard at `monitor.avalontech.xyz`

The hub listens on AVALON at `:8787`. Agents reach it over Tailscale; the
browser reaches it through the existing Cloudflare Tunnel (`avalon-tunnel`),
with **Cloudflare Access** deciding who gets in. The hub *also* verifies the
Access token on every request, so a tunnel misconfiguration can never expose
the dashboard: without a valid token, traffic that arrives via Cloudflare gets
a 401/403 and nothing else.

```
 browser ──HTTPS──▶ Cloudflare edge ──(Access login)──▶ tunnel ──▶ cloudflared on AVALON ──▶ 127.0.0.1:8787
 agents  ──Tailscale (100.x)───────────────────────────────────────────────────────────────▶ 100.123.17.92:8787
```

## 1. Add the tunnel ingress rule (on AVALON)

Edit `/etc/cloudflared/config.yml` (this is the file the `cloudflared` service
actually uses; `~/.cloudflared/config.yml` is a stale copy). Insert the rule
**before** the final `http_status:404` entry:

```yaml
  - hostname: monitor.avalontech.xyz
    service: http://localhost:8787
```

Then create the DNS record for the hostname and restart the tunnel:

```bash
cloudflared tunnel route dns avalon-tunnel monitor.avalontech.xyz
sudo cloudflared --config /etc/cloudflared/config.yml tunnel ingress validate
sudo systemctl restart cloudflared
```

At this point `https://monitor.avalontech.xyz` answers **403 "Cloudflare Access
is not configured on this server"** — that is the hub refusing tunnel traffic
until Access is set up. Good.

## 2. Create the Access application (Zero Trust dashboard)

1. <https://one.dash.cloudflare.com> → **Access → Applications → Add an application → Self-hosted**.
2. Application name: `Avalon Monitor`. Domain: `monitor.avalontech.xyz`.
   Session duration: 24h is comfortable for a personal dashboard.
3. **Policy**: name `Owner`, action *Allow*, include → *Emails* → `theluminec@gmail.com`
   (add more emails, or an *Emails ending in* rule, as you like).
   Leave the default *One-time PIN* login method on, or add Google/GitHub under
   **Settings → Authentication** for one-click sign-in.
4. Save. Open the application's **Overview** tab and copy the
   **Application Audience (AUD) Tag**.
5. Your **team domain** is under **Settings → Custom Pages** (looks like
   `avalontech.cloudflareaccess.com`).

Optional hardening in the same application:
* **Settings → Cookie settings**: enable *HTTP Only* and *Binding cookie*.
* Add a *Require* rule for **Country** or **WARP** if you always connect from
  the same place / through WARP.

## 3. Tell the hub to verify Access tokens

```bash
sudo nano /etc/avalon-monitor/monitor.env
```
```ini
AVM_ACCESS_TEAM_DOMAIN=avalontech.cloudflareaccess.com
AVM_ACCESS_AUD=<the AUD tag from step 2>
AVM_PUBLIC_URL=https://monitor.avalontech.xyz
```
```bash
sudo systemctl restart avalon-monitor
journalctl -u avalon-monitor -n 5   # expect: "Cloudflare Access verification enabled"
```

Visit `https://monitor.avalontech.xyz` → Cloudflare login page → dashboard.
The user chip in the top-right shows the email from the Access token.

## 4. What the hub enforces (defense in depth)

| Request arrives…                                   | Result |
|----------------------------------------------------|--------|
| via Cloudflare with a valid Access JWT             | allowed, identity shown in the UI |
| via Cloudflare with a missing / invalid / expired JWT | 401 |
| via Cloudflare while Access isn't configured       | 403 (nothing served, not even the HTML shell) |
| directly from the tailnet or localhost, no Cloudflare headers | allowed (`AVM_ALLOW_TRUSTED_NO_AUTH=true`) |
| from any other network                             | 401 |
| `POST /api/v1/ingest` via Cloudflare               | 403 always |
| `POST /api/v1/ingest` from the tailnet, valid host token | accepted |

The Access JWT is validated against Cloudflare's public keys
(`https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`) with RS256 and
the `aud`/`iss`/`exp` claims checked; keys are cached for an hour. The hub
never trusts `X-Forwarded-For`; the policy is keyed on the real socket peer
plus the presence of Cloudflare's own headers.

## 5. Keep the origin private

* Do **not** open port 8787 on the router — nothing outside the tunnel or the
  tailnet needs it. If AVALON has `ufw` enabled:
  `sudo ufw allow in on tailscale0 to any port 8787 proto tcp`
* Rotate a machine's agent token any time with
  `avalon-monitor-manage rotate-token <name>`; disable a lost laptop with
  `avalon-monitor-manage disable <name>`.

## Troubleshooting

* **`invalid Cloudflare Access token`** in the journal: the AUD tag doesn't
  match the application, or the team domain is wrong.
* **Dashboard loads but "Cannot reach the hub API"**: check
  `curl -s http://127.0.0.1:8787/healthz` on AVALON and `journalctl -u cloudflared`.
* **WebSocket doesn't connect (grey dot in the user chip)**: Cloudflare proxies
  WebSockets by default; make sure no Cloudflare *Configuration Rule* disables
  them for this hostname.
