# Runbook — Blackbox prod probes

The blackbox-exporter (`prom/blackbox-exporter:v0.25.0`, container
`blackbox-exporter`) continuously probes the same 6 prod public routes
that `scripts/verify_prod_smoke.sh` validates post-deploy. Smoke is the
deploy-time gate; blackbox is the steady-state monitor between deploys.

Source of truth for the route list: `infra/prometheus/prometheus.yml`
(scrape_config `blackbox`). Modules and HTTP-status expectations live in
`infra/blackbox-exporter/blackbox.yml`.

| Route label             | URL                                                                  | Module          | Expected |
|-------------------------|----------------------------------------------------------------------|-----------------|----------|
| `isnad-health`          | `https://isnad.noorinalabs.com/health`                               | `http_2xx_json` | 200, JSON body |
| `user-service-health`   | `https://isnad.noorinalabs.com/api/v1/user-service/health`           | `http_2xx`      | 200 |
| `landing-root`          | `https://noorinalabs.com/`                                           | `http_2xx`      | 200 |
| `narrator-401`          | `https://isnad.noorinalabs.com/api/v1/narrators?limit=1`             | `http_401_json` | 401/403 + JSON body |
| `jwks`                  | `https://isnad.noorinalabs.com/.well-known/jwks.json`                | `http_2xx_json` | 200, JSON `.keys[]` |
| `auth-login-redirect`   | `https://isnad.noorinalabs.com/auth/oauth/google/login`              | `http_2xx_json` | 200 + JSON `.authorization_url` (label retained as historical name — see deploy#256 / PR#258) |

The hostnames moved to `isnad.noorinalabs.com` (frontend + isnad-graph
API + dual-bound user-service routes) and `users.noorinalabs.com`
(pure user-service API surface) during the 2026-05-02 emergency CF DNS
reconciliation (#226) and were applied to this scrape config in
deploy#255. The post-deploy smoke battery
(`scripts/verify_prod_smoke.sh`) was updated in lockstep via
deploy#252 / PR#254. The five routes above all sit on the dual-bound
isnad vhost (see `caddy/Caddyfile`); the de-duplication of those
routes onto `users.*` only is a transitional follow-up tracked in
that file's comment block — when it lands, swap the relevant probe
host here and in the smoke battery in lockstep, same as we did this
time.

## Alerts

### probe-failing

Fires when `probe_success == 0` for any route for >2m.

Investigation steps:

1. Reproduce the probe locally:
   `curl -sSI https://<route-url>` from a non-VPS host.
2. If the route is healthy from outside but blackbox shows it down:
   - SSH to prod and run `docker compose logs --tail 200 blackbox-exporter`.
   - Check for DNS-resolution errors (the container is on `frontend` +
     `backend`; only `frontend` has internet egress).
   - **Suspect hairpin-NAT** if logs show no DNS errors and the host
     itself can reach the route (`curl -sSI https://<route>` from the
     prod shell). blackbox runs on the same prod box that Caddy serves,
     so it probes its own public FQDN — egress works on Ubuntu 24.04
     via standard MASQUERADE today, but a future kernel / netfilter /
     docker-network change can break self-targeted SNAT. Reproduce from
     inside the container with `docker compose exec blackbox-exporter wget -qO- -S https://<route>`.
     If hairpin is the cause, do NOT pre-emptively re-shape the
     topology — file an issue and we'll revisit.
3. If the route is genuinely down:
   - For `isnad-health` / `user-service-health`: pivot to the
     `ServiceDown` / `ContainerUnhealthy` runbooks — those alerts
     should already be firing.
   - For `narrator-401` shape regression: this is the "Caddy answering
     in plaintext while user-service is down" failure mode. Check that
     `user-service` container is responding on its internal network
     (`docker compose exec caddy wget -qO- http://user-service:8000/health`).
   - For `auth-login-redirect`: confirm the `/auth/login` site block in
     `caddy/Caddyfile` is still wired and that user-service has the
     OAuth client credentials in `.env`.

### unexpected-status

Fires when a route returns an HTTP code outside the expected set for >5m
(e.g. narrator-401 returning 200, or jwks returning 500). The alert is a
weaker-but-louder signal than `probe-failing`; it tells you *what code*
is being returned. Most often this points at a Caddy mis-route or a
service answering in degraded mode.

### cert-expiring-soon

Fires when the TLS cert for any probed route expires in <7 days. Caddy
auto-renews ~30 days before expiry, so reaching this threshold means
renewal is failing. Check:

- `docker compose logs --tail 500 caddy | grep -i 'acme\|certificate'`
- `/data` volume disk usage (Let's Encrypt renewal needs scratch space).
- Let's Encrypt rate-limit status (was the cert reissued repeatedly?).

## Silencing during planned maintenance

When you are about to do something that will deliberately cause one or
more probes to fail (e.g. a known-disruptive deploy, a Caddy config
shuffle, taking the box down for a Hetzner snapshot), silence the
relevant alerts in alertmanager *before* the disruption rather than
acknowledging pages after the fact.

Current alertmanager runs on the prod box, bound to `127.0.0.1:9093`.
Reach it via SSH tunnel:

```bash
ssh -L 9093:127.0.0.1:9093 deploy@<prod-host>
# leave the session open while you work
```

Then create a silence with `amtool` (installed on the prod box) or via
the alertmanager web UI at `http://localhost:9093`:

```bash
# Silence ALL blackbox alerts for 30 minutes
amtool silence add \
  --alertmanager.url=http://127.0.0.1:9093 \
  --comment "Planned maintenance — deploy#NNN" \
  --duration 30m \
  alertname=~"Blackbox.*"

# Silence one specific route (e.g. just /auth/login while reshuffling Caddy)
amtool silence add \
  --alertmanager.url=http://127.0.0.1:9093 \
  --comment "Reshuffling Caddy auth carve-out — deploy#NNN" \
  --duration 15m \
  alertname=~"Blackbox.*" route="auth-login-redirect"
```

Always include a comment with the issue or PR number — it's the only
breadcrumb to whoever is on-call after you. To list/expire silences:

```bash
amtool silence query   --alertmanager.url=http://127.0.0.1:9093
amtool silence expire <silence-id> --alertmanager.url=http://127.0.0.1:9093
```

Silences auto-expire at the end of their duration; prefer a tight
duration over an open-ended one. If a maintenance window over-runs,
extend the silence rather than letting it lapse and then re-creating.
