# Runbook — Observability

Index of the observability stack and the dashboards / external surfaces
that complement it. This file is intentionally an entrypoint: each named
sub-system has its own runbook (linked below) for the deep procedure.

The deploy-repo observability stack is documented per-component in
`infra/{prometheus,grafana,loki,alloy,alertmanager,blackbox-exporter}/`.
The list of running containers is canonical in
`compose/docker-compose.prod.yml` and mirrored in
`ontology/repos/deploy.yaml` (services.observability).

## Surfaces

| Surface | Signal type | Where it lives | Runbook |
|---|---|---|---|
| Prometheus | metrics scrape | prod VPS, port 9090 (internal) | per-alert links in `infra/prometheus/alerts.yml` |
| Grafana | metrics + log visualisation | prod VPS, `/grafana` behind Caddy | [§ Grafana access](#grafana-access) |
| Loki + Alloy | log aggregation | prod VPS, container-side `docker-socket` scrape | [`log-ingestion.md`](log-ingestion.md) · [§ Loki retention](#loki-retention-hot-reloadable) |
| Alertmanager | alert routing | prod VPS, port 9093 (internal) | [`alertmanager-slack-routing.md`](alertmanager-slack-routing.md) |
| blackbox-exporter | synthetic probes (HTTP) | prod VPS, container `blackbox-exporter` | [`blackbox-probes.md`](blackbox-probes.md) |
| Cloudflare Web Analytics (CWA) | real-user RUM | CF dashboard (per zone) | § below |

The synthetic-vs-real-user distinction matters for triage:
**blackbox** answers "is this route reachable from a third-party host?" and
**CWA** answers "what does a real visitor's browser see for p75 LCP?"
The two are complements, not duplicates — a regression in either signal
is independently actionable.

---

## Grafana access

### Access URL

Grafana is mounted as a **sub-path** behind Caddy on the isnad vhost:

| Env | URL |
|---|---|
| prod | `https://isnad.noorinalabs.com/grafana` |
| stg | `https://isnad.stg.noorinalabs.com/grafana` |

The host is `isnad.{$BASE_DOMAIN}` — the same vhost that serves the
isnad-graph frontend/API (`caddy/Caddyfile` → `handle /grafana/*` →
`reverse_proxy grafana:3000`). It is **not** a dedicated subdomain. The
legacy `isnad-graph.noorinalabs.com` record that Grafana's
`GF_SERVER_ROOT_URL` formerly pointed at was destroyed in the
2026-05-02 reconciliation (#192/#226); `GF_SERVER_ROOT_URL` is now
templated to `https://isnad.${BASE_DOMAIN}/grafana` so login-redirect
and sub-path asset URLs resolve to the live host (deploy#45).

Two compose env vars make the sub-path mount work and must stay in
lockstep with the Caddy route — change one, change all three:

- `GF_SERVER_ROOT_URL: https://isnad.${BASE_DOMAIN}/grafana`
- `GF_SERVER_SERVE_FROM_SUB_PATH: "true"`
- `caddy/Caddyfile` → `handle /grafana/*` on the `isnad.{$BASE_DOMAIN}` vhost

### Login

Admin login is provisioned from compose env (`compose/.env`):

- user: `GRAFANA_ADMIN_USER` (defaults to `admin` if unset)
- password: `GRAFANA_ADMIN_PASSWORD` (required — compose fails fast if unset)

The password is **not** in the repo. On the VPS it lives in
`compose/.env`, written from the GitHub Actions encrypted secret
`GRAFANA_ADMIN_PASSWORD` by the deploy workflow. To retrieve it for the
project owner, read `compose/.env` on the VPS (`grep GRAFANA_ADMIN
compose/.env`) or rotate it via the GitHub secret + redeploy.

**App-admin SSO (deploy#458):** app admins reach Grafana with no second login
via a Caddy `forward_auth` RBAC bridge (`GF_AUTH_PROXY_*` env + the `handle
/grafana/*` block) — see [`grafana-forward-auth-sso.md`](grafana-forward-auth-sso.md)
for the design, the anti-spoof controls, the cross-repo completion gate, and the
SSH-tunnel break-glass to the admin login form. The basic-auth path above is the
break-glass; there is no Grafana OAuth integration.

### Dashboards

Provisioned at boot from `infra/grafana/dashboards/` via
`infra/grafana/provisioning/dashboards/dashboards.yml`. Datasources
(Prometheus default, Loki) are provisioned from
`infra/grafana/provisioning/datasources/datasource.yml`. Bundled
dashboards:

- `api-overview.json` — API latency, request counts, error rates
- `kafka-pipeline.json` — pipeline worker throughput / lag
- `blackbox-probes.json` — synthetic HTTP probe results

### Post-merge verification (Test Plan — requires the live stack)

Grafana reachability + working login cannot be verified at PR time — it
needs the deployed stack with real DNS, TLS, and the admin secret. After
this change deploys, an operator confirms on the live host:

1. `curl -fsS https://isnad.noorinalabs.com/grafana/api/health` → returns
   `{"database":"ok",...}` (200). Confirms reachability + sub-path routing.
2. Open `https://isnad.noorinalabs.com/grafana/login` in a browser →
   Grafana login page renders with correct CSS/JS (no asset 404s — this
   is what the `GF_SERVER_ROOT_URL` fix is for; a wrong root URL serves
   the page but breaks sub-path assets + the post-login redirect).
3. Log in with `admin` / `GRAFANA_ADMIN_PASSWORD` (from VPS
   `compose/.env`) → lands on the Grafana home, no redirect loop to a
   dead host.
4. Open **Dashboards** → confirm `api-overview`, `kafka-pipeline`, and
   `blackbox-probes` are present, and that `api-overview` panels render
   data (datasource wiring is live).
5. Record the URL + credential-location in the owner hand-off so the
   project owner can reach metrics.

If step 1 or 2 fails after deploy, check that `BASE_DOMAIN` is set in the
VPS `compose/.env` and that the `isnad` Caddy vhost is serving (the
Grafana route is nested under it, not a standalone subdomain).

---

## Loki retention (hot-reloadable)

Loki's log retention is **hot-reloadable** — the "keep last X days" window can be
changed at runtime **without redeploying or restarting** the Loki container
(deploy#451). This backs the admin-panel "log retention (days)" control
(isnad-graph ig#1038).

### How it works

Two layers, static default + dynamic override:

| Layer | File | Behavior |
|---|---|---|
| Static default / fallback | `infra/loki/loki-config.yml` → `limits_config.retention_period` (`168h` / 7d) | Read at **startup only**. Applies to any tenant with no override entry. |
| Dynamic override | `infra/loki/runtime-overrides.yaml` → `overrides.fake.retention_period` | Re-read every `runtime_config.period` (`30s`); **takes precedence**; hot-reloadable. |

`auth_enabled: false`, so every log stream lands under the single built-in Loki
tenant id **`fake`** — that is the key under `overrides:` to edit.

The override file lives on the writable named volume **`loki_runtime`**, mounted
into three containers (`compose/docker-compose.prod.yml`):

- `loki-runtime-init` (one-shot) — seeds the volume with the in-tree default
  `infra/loki/runtime-overrides.yaml` **iff** the volume is empty, then `chmod`s
  it writable. Mirrors the `kafka-init` / `minio-setup` idiom. Loki
  `depends_on` it with `service_completed_successfully` because **Loki fails to
  start if the `runtime_config.file` is missing** (verified: the
  `runtime-config` module errors and the whole process exits) — the seed
  guarantees a valid file exists before Loki loads.
- `loki` — reads `/etc/loki/runtime/overrides.yaml` (the volume), re-reading on
  the 30s reload interval.
- `api` — the ig#1038 admin endpoint writes the new `retention_period` into the
  same file (in-place truncate-write — do **not** rename-replace, which changes
  the inode and breaks the bind/volume view).

Persistence: because the seed only runs when the volume is empty, an admin-set
value survives redeploys and is not clobbered by the repo copy on every
`compose up`.

### Timing — two intervals stack

A retention change is visible in two steps:

1. **Config reload** (≤ `runtime_config.period`, 30s): Loki picks up the new
   value. Observable on the `loki_runtime_config_hash` metric (it changes) and
   `loki_runtime_config_last_reload_successful` (stays `1`).
2. **Enforcement** (≤ `compactor.compaction_interval`, 10m): the compactor
   applies the new window on its next cycle, marking now-expired chunks for
   deletion (then `retention_delete_delay`, 2h, before the chunk is removed).

So "shrink retention" frees disk within ~10m + 2h, not instantly; "grow
retention" takes effect immediately for future compaction. Document this lag in
the admin UI copy so the owner doesn't expect an instant disk drop.

### Verify a retention change (live host)

```bash
# 1. Read the current effective override
docker compose -f compose/docker-compose.prod.yml exec loki \
  cat /etc/loki/runtime/overrides.yaml

# 2. Capture the runtime-config hash BEFORE the change
docker compose -f compose/docker-compose.prod.yml exec loki \
  wget -qO- http://localhost:3100/metrics | grep loki_runtime_config_hash

# 3. Change it (the ig#1038 admin control does this; manual edit shown for ops).
#    Edit overrides.fake.retention_period IN PLACE, e.g. 168h -> 336h.

# 4. After ~30s, confirm the hash CHANGED and the reload succeeded — and that
#    the container did NOT restart (StartedAt / RestartCount unchanged).
docker compose -f compose/docker-compose.prod.yml exec loki \
  wget -qO- http://localhost:3100/metrics \
  | grep -E 'loki_runtime_config_(hash|last_reload_successful)'
docker inspect -f 'StartedAt={{.State.StartedAt}} RestartCount={{.RestartCount}}' \
  "$(docker compose -f compose/docker-compose.prod.yml ps -q loki)"
```

This exact sequence was validated locally before merge (grafana/loki:2.9.10):
the hash changed on a 168h→720h edit, `last_reload_successful=1`, and
`RestartCount=0` / `StartedAt` unchanged — i.e. no restart. The compactor
deletion step itself only runs against aged data on the live box, so confirming
that now-expired chunks actually disappear is a **post-merge owner observation**
(watch `loki_compactor_*` metrics / the Loki disk usage panel after a shrink).

---

## Cloudflare Web Analytics (CWA)

### What this is

Cloudflare's cookieless real-user monitoring (RUM), shipped free with
any zone on the platform. It collects Core Web Vitals (LCP, FID, CLS,
INP), pageviews, top pages, status-code breakdown, geo, browser, OS,
and referrer at p75/p90 percentiles, 30-day retention.

Free tier is **dashboard-only** — there is no API export and no
programmatic alerting. The Pro tier ($20/zone/month) adds Web Analytics
GraphQL API for Grafana export and custom dimensions / events; that
tier is explicitly out of scope today and will be re-evaluated no
earlier than 2026-08-01 (issue #253 acceptance).

### Operator pre-step: verify enablement

CWA must be enabled in the Cloudflare dashboard for each zone we want
RUM coverage on. This is one of the few CF surfaces not managed via
Terraform — there is no `cloudflare_web_analytics_site` resource in
`terraform/cloudflare/` and adding one is not in scope here.

For each zone:

1. Open `https://dash.cloudflare.com/{account-id}/{zone}/analytics/web`
   (the dashboard auto-resolves to your account; the URL above is the
   shape you'll see).
2. If the page reads "Web Analytics is not enabled," click **Enable**.
   First data appears ~10 minutes after enablement.
3. Repeat for both zones if they are separate:
   - `noorinalabs.com` — covers `www.noorinalabs.com`,
     `isnad.noorinalabs.com`, `users.noorinalabs.com`
   - `stg.noorinalabs.com` — covers `*.stg.noorinalabs.com`
     (verify whether this is a separate zone or a sub-record on the
     parent zone; coverage shape differs)

This step is operator-only because the runbook process has no
credentials to the Cloudflare dashboard. Confirm enablement here is
treated as a pre-condition for the rest of this section.

### Dashboard URLs

Fill in the per-account URLs after first enablement — they are
account-scoped and not knowable from this repo:

| Zone | Dashboard URL |
|---|---|
| `noorinalabs.com` (prod) | `https://dash.cloudflare.com/<account>/<zone-id>/analytics/web` |
| `stg.noorinalabs.com` (stg, if separate zone) | `https://dash.cloudflare.com/<account>/<zone-id>/analytics/web` |

When you fill these in, prefer the stable `account` slug over the
numeric id (CF accepts both, the slug survives org rename).

### Baseline snapshot

A baseline of current CWV per top page lets a frontend change
(e.g. the `users.noorinalabs.com` absolute-URL shift in deploy#245)
be evaluated against a known-good before-picture. Snapshot is text
in this runbook; no automation.

**Snapshot template — fill in after first enablement + 7 days of data:**

```
as of YYYY-MM-DD (7-day window, all zones)

Top page                              | p75 LCP | p75 CLS | p75 INP | pageviews
--------------------------------------|---------|---------|---------|----------
noorinalabs.com/                      |         |         |         |
isnad.noorinalabs.com/                |         |         |         |
isnad.noorinalabs.com/search          |         |         |         |
users.noorinalabs.com/login           |         |         |         |
```

Core Web Vitals thresholds (Google's "Good" bucket, for reference when
reading the table):

| Metric | Good | Needs improvement | Poor |
|---|---|---|---|
| LCP (largest contentful paint) | ≤ 2.5s | ≤ 4.0s | > 4.0s |
| CLS (cumulative layout shift) | ≤ 0.1 | ≤ 0.25 | > 0.25 |
| INP (interaction to next paint) | ≤ 200ms | ≤ 500ms | > 500ms |

Re-snapshot after any frontend-shipping wave (especially deploy#245
absolute-URL shift) and after any noticeable CF/Caddy/Compose
change that could affect the request path.

### Cadence

- **Default:** monthly review — open the dashboard, scan the CWV
  trend per top page, note anything trending out of "Good." A
  five-minute read.
- **During active frontend work:** weekly. Frontend changes are the
  most common cause of CWV regressions, so the tighter cadence pairs
  with the wave that's shipping them.
- **Ad-hoc:** any time blackbox reports a probe regression on a
  frontend route (`landing-root`, the isnad frontend), check CWA for
  the same window — real-user signal often surfaces the cause that
  synthetic probes can only see as a 200/timeout boolean.

### Optional: wave-retro check-in

Wave retros are a natural cadence anchor. The minimum useful read is:

- Open each zone's CWA dashboard for the wave window.
- For each top page, note whether p75 LCP / CLS / INP shifted
  bucket (Good → Needs improvement, etc.).
- If any did, file an issue against the owning service repo with
  the before/after snapshot.

This is currently a manual habit, not a charter-enforced step;
deploy#253 acceptance is to document it here for adoption.

### Out of scope

- **Pro-tier upgrade.** Re-evaluate no earlier than 2026-08-01.
  Trigger conditions are: (a) we want CWV in Grafana alongside
  blackbox, (b) we want event-level export for funnels, (c) we want
  custom dimensions / events. Not pre-paying for capability we may
  not use.
- **Programmatic alerting** on RUM metrics — requires Pro-tier API
  access; deferred with the Pro upgrade above.
- **Custom event tracking** (button clicks, form submits) — a
  different tool (PostHog, Plausible) is the right shape if/when
  this becomes load-bearing.

### Why this isn't in Terraform

`cloudflare_web_analytics_site` is a real provider resource; we
intentionally don't manage it from `terraform/cloudflare/` today
because (a) free-tier CWA has no per-site configuration worth
codifying, (b) enabling it is a one-click operator action, and
(c) the auto-injected `beacon.min.js` is the only runtime artifact
and has no per-env variance. If we adopt Pro tier or start managing
custom rules, that decision changes — open an ADR before adding
the resource.
