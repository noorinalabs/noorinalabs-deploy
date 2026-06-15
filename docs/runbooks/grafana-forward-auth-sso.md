# Runbook — Grafana admin-gated SSO (forward_auth RBAC bridge)

Closes the gap reported in **deploy#458** / ig#1073: the admin-dashboard
Observability quick-links (Grafana / Logs / Alerts, added in ig#1066) used to
dead-end on Grafana's own login wall because the app session did not carry
through. This runbook documents the SSO bridge that lets an app admin reach
Grafana with no second login, how it is hardened against header spoofing, the
operator break-glass path, and exactly what can only be verified live on the
cluster.

## How it works

```
browser (app admin on isnad.<domain>)
  │  GET /grafana/...   (carries the app session credential — see "Credential
  │                      carry" below)
  ▼
Caddy  handle /grafana/*  (caddy/Caddyfile, isnad.<domain> vhost)
  │  1. strip any client-supplied X-Webauth-User / X-Webauth-Role  (anti-spoof L1)
  │  2. forward_auth → user-service:8000  GET /auth/forward-auth
  │        ├─ 2xx + X-Webauth-User[, X-Webauth-Role]  → admin: continue
  │        └─ 401/403                                  → relayed to client, STOP
  │  3. copy_headers X-Webauth-User X-Webauth-Role onto the upstream request
  ▼
Grafana  grafana:3000   (GF_AUTH_PROXY_ENABLED, trusts X-WEBAUTH-USER)
         auto-creates/updates the user, maps Role → org role
```

Three independent controls stop a forged identity header (acceptance item
"external X-WEBAUTH-USER cannot impersonate"):

1. **Network isolation** — Grafana has `expose: 3000` and NO host `ports:`, and
   sits only on the `internal: true` `backend` network. The only process that
   can reach `grafana:3000` is Caddy. An external client cannot.
2. **Caddy up-front strip** — `request_header -X-Webauth-User` / `-X-Webauth-Role`
   run before the verify sub-request, so a client that forges the header has it
   removed; the only `X-Webauth-*` that survives is the one `copy_headers` lifts
   from the user-service 2xx response.
3. **Grafana whitelist** — `GF_AUTH_PROXY_WHITELIST` bounds the set of source
   IPs Grafana will trust the header from to the in-cluster RFC1918 ranges
   (defense-in-depth against a *compromised in-cluster* peer, on top of 1 & 2).

## Credential carry — the cross-repo contract

A top-level browser **navigation** to `/grafana/` does not send an
`Authorization: Bearer` header, and the app access token lives only in the SPA's
`localStorage` — so the existing `GET /auth/token/validate` (Bearer-based) is the
wrong endpoint for forward_auth. The bridge therefore needs a session credential
the browser auto-sends to `isnad.<domain>` and a cookie-aware verify endpoint:

- **user-service — `GET /auth/forward-auth`** (companion change): read the app
  session credential the browser carries to `isnad.<domain>`, return
  **2xx + `X-Webauth-User: <email>`** (and `X-Webauth-Role: Admin`) for an admin
  principal, **401/403** otherwise. The mechanism by which a credential reaches
  `isnad.<domain>` (e.g. a short-lived parent-domain `Domain=<domain>` session
  cookie, httpOnly+Secure+SameSite=Lax, minted from the app bearer) is a
  **security decision that needs owner sign-off** (parent-domain scoping widens
  the cookie surface to every subdomain) — see deploy#458.
- **frontend (ig#1073)** — re-enable the obs links and carry that credential on
  the link click.

Until both companions ship, the Caddy/Grafana config here is **inert and must
not be promoted** (see next section).

## Promotion gate (release coordination)

`forward_auth` is a **hard cutover**: once it is live, Grafana's own login form
is no longer reachable *through Caddy*. If this deploy slice is promoted to
stg/prod before `user-service GET /auth/forward-auth` exists, every `/grafana`
request gets a `502`/`401` from the missing endpoint and Grafana becomes
unreachable via the browser. Therefore:

> **Do NOT promote the `/grafana` forward_auth + Grafana auth-proxy change to
> stg or prod until the user-service `/auth/forward-auth` endpoint is deployed.**
> The product surface is already safe in the meantime — ig#1073 hides the obs
> links until SSO ships.

## Break-glass — Grafana admin login (form)

Operators who need the raw Grafana login form (the `GRAFANA_ADMIN_USER` /
`GRAFANA_ADMIN_PASSWORD` path) bypass Caddy and hit Grafana directly over an SSH
tunnel — this sidesteps forward_auth entirely:

```bash
ssh -L 3000:127.0.0.1:3000 deploy@<prod-vps>   # then open http://localhost:3000
```

Grafana is bound to the container only; the tunnel reaches it through the host
docker bridge. The auth-proxy header is absent on this path, so Grafana presents
its normal login form.

## What can only be verified live on the cluster (operational gate, not PR-time)

These require the full stack + the user-service companion running on staging and
cannot be asserted by `docker compose config` / unit checks:

- [ ] App admin (logged into the product) clicks an obs link → reaches Grafana
      with **no second login** (acceptance #1).
- [ ] Unauthenticated / non-admin request to `/grafana/*` is **rejected**
      (401/403 or app-login redirect), verified on staging (acceptance #2).
- [ ] A request that forges `X-WEBAUTH-USER` (direct or via Caddy) does **not**
      impersonate — confirm the Caddy strip + Grafana whitelist hold
      (acceptance #3). Probe both `curl -H 'X-Webauth-User: ...' https://isnad.stg…/grafana/`
      and (from a non-Caddy backend peer) `curl -H 'X-Webauth-User: ...' http://grafana:3000/…`.
- [ ] (Hardening follow-up) tighten `GF_AUTH_PROXY_WHITELIST` from the RFC1918
      ranges to Caddy's `/32` once the backend subnet or Caddy IP is pinned.

## Why Loki is not exposed as `/loki/*`

The issue mentions "`/loki/*` if exposed". Loki is **internal only** (no public
Caddy route; queried by Grafana over the `backend` network as a datasource).
Adding a public `/loki/*` route would expose the raw Loki API to the edge — a
security downgrade. Admin log access is delivered through Grafana **Explore**,
which is itself now SSO-gated by this bridge. So no `/loki/*` route is added.

## Files

- `caddy/Caddyfile` — `handle /grafana/*` forward_auth block (isnad.<domain> vhost)
- `compose/docker-compose.prod.yml` — `grafana` service `GF_AUTH_PROXY_*` env
- Companion (other repos): user-service `GET /auth/forward-auth`, frontend ig#1073
