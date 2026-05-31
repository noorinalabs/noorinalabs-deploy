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
| Loki + Alloy | log aggregation | prod VPS, container-side `docker-socket` scrape | [`log-ingestion.md`](log-ingestion.md) |
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
compose/.env`) or rotate it via the GitHub secret + redeploy. There is
no OAuth integration for Grafana today — admin basic-auth only.

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
