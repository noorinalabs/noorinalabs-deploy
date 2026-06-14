# API Surface Partition — isnad-graph-api vs user-service

## Summary

Two backend services share the `/api/v1/*` URL space behind Caddy on
`isnad.{$BASE_DOMAIN}`. To prevent routing conflicts and ambiguous ownership,
this document records the canonical partition derived from
[`caddy/Caddyfile`](../caddy/Caddyfile).

Any new route added to either service **MUST** respect this partition.
Reviewers should reject PRs that violate it without an explicit partition
update here.

When this document and the Caddyfile disagree, **the Caddyfile wins** (it's
the runtime truth) — the disagreement is a documentation bug that must be
fixed before the next PR in the offending service merges.

## Owners

- **isnad-graph-api** (repo: `noorinalabs-isnad-graph`, compose service:
  `api`) — the computational hadith analysis backend. Owns the isnad graph
  domain endpoints, search, narrator/hadith CRUD, the top-level `/health`
  probe on `isnad.{$BASE_DOMAIN}`, and `/status`. Catch-all consumer of
  everything under `/api/*` not explicitly carved out to user-service.
- **user-service** (repo: `noorinalabs-user-service`, compose service:
  `user-service`) — JWT issuer, OAuth provider (Google/GitHub/Apple), session
  lifecycle, RBAC, 2FA, billing/subscription, email verification, JWKS.
  Owns a fixed list of `/api/v1/*` prefixes plus `/auth/*` and the IETF
  well-known JWKS path.
- **frontend** (repo: `noorinalabs-isnad-graph`, compose service:
  `frontend`) — React SPA. Owns `/auth/callback/*` (OAuth post-login redirect
  target rendered by `AuthCallbackPage` — see deploy#133, user-service#67)
  and the SPA catch-all (`handle { … }`) on `isnad.{$BASE_DOMAIN}`.

## Partition (`isnad.{$BASE_DOMAIN}`)

| Path | Owner | Notes |
|---|---|---|
| `/auth/callback/*` | frontend | OAuth post-login UI render (`AuthCallbackPage`); the **only** `/auth/*` path that stays on this vhost post-#245. Matched before the `/api/*` catch-all — see Caddyfile comment block. |
| `/api/*` | isnad-graph-api | The entire `/api/*` space on this vhost. All user-service routes (`/auth/*`, JWKS, `/api/v1/{users,sessions,subscriptions,verification,roles,2fa}`, and the `/api/v1/user-service/health` rewrite) were **removed** from `isnad.*` in #245 phase 2 (commit `e321e9a7`) and now live solely on `users.{$BASE_DOMAIN}` — see "Vhost split" below. A request for any of those paths on `isnad.*` now hits this catch-all (→ isnad-graph-api 404) or the SPA fallthrough, by design; monitoring probes for them target `users.*` (deploy#449). |
| `/health` | isnad-graph-api | Liveness probe for isnad-graph-api on this vhost. |
| `/status` | isnad-graph-api | Status endpoint for isnad-graph-api. |
| `/metrics` | (none — 403) | Prometheus scrapes `api:8000/metrics` directly via the Docker backend network. |
| `/grafana/*` | grafana | Internal observability (auth at the Grafana layer). |
| `/` (everything else) | frontend | SPA fallback (`handle { reverse_proxy frontend:80 }`). |

## Partition (`users.{$BASE_DOMAIN}`)

This vhost is a **pure user-service API surface**, carved out from
`isnad.{$BASE_DOMAIN}` in #220. Every path on this vhost — whether explicitly
handled or via the final `handle { reverse_proxy user-service:8000 }`
catch-all — terminates at `user-service:8000`. There is no isnad-graph-api,
no frontend SPA, and no grafana on this vhost.

Direct hits to `users.{$BASE_DOMAIN}/anything-unmapped` return a clean
user-service 404 JSON.

Security headers on `users.{$BASE_DOMAIN}` are tuned for a JSON-only API
surface (`Content-Security-Policy: default-src 'none'; frame-ancestors
'none'; base-uri 'none'`, `Cross-Origin-Opener-Policy: same-origin`,
`Cross-Origin-Resource-Policy: same-site`) — see closes-#243 comment block
in the Caddyfile at line 232.

## Rules for adding a new route

1. **isnad-graph-api MUST NOT add routes under any path prefix owned by
   user-service** in the partition table above. If you need a user-adjacent
   endpoint:
   - Put it under a non-overlapping prefix (e.g.,
     `/api/v1/research/users-following`), **or**
   - Move the responsibility to user-service.
2. **user-service MUST NOT add routes outside its owned prefixes** above. New
   user-service endpoints get a new prefix; that prefix is added to
   **BOTH** this document AND `caddy/Caddyfile` (in **both** the `isnad.` and
   `users.` blocks during the dual-binding transition — see "Vhost split"
   below) in the same PR.
3. **frontend MUST NOT add a route on `isnad.{$BASE_DOMAIN}` outside
   `/auth/callback/*` and the catch-all `handle { }`**. The SPA serves
   everything not explicitly carved out to a backend; new SPA routes are
   client-side React Router paths, not Caddy `handle` blocks.
4. **`caddy/Caddyfile` and this document are the canonical pair.** If they
   ever disagree, the Caddyfile wins (it's the runtime truth) but the
   disagreement is a documentation bug that must be fixed before the next PR
   in the offending service merges.
5. **Order matters in Caddy `handle` matching**: more-specific paths must
   appear before broader ones in the same site block. `/auth/callback/*`
   before `/auth/*`; user-service `/api/v1/<prefix>/*` carve-outs before the
   isnad-graph-api `/api/*` catch-all. See Caddyfile comment at line 32 for
   the documented precedent.

## Vhost split (complete — post-#245)

The user-service vhost split is **done**. The migration ran in two phases,
documented in the Caddyfile comment block on the `isnad.{$BASE_DOMAIN}` site:

- **Phase 1 (#241, 2026-05-02):** dual-bound user-service routes on
  `users.{$BASE_DOMAIN}` AND `isnad.{$BASE_DOMAIN}` because the frontend then
  hard-coded relative URLs (`/auth/oauth/{provider}/login`, `/api/v1/users`,
  …). Removing the routes from `isnad.*` at that point would have broken login.
- **Phase 2 (#245, commit `e321e9a7`):** the frontend cut over to absolute URLs
  resolved from `window.RUNTIME_CONFIG.USER_SERVICE_ORIGIN =
  https://users.{$BASE_DOMAIN}` (isnad-graph#932/#934), so the dual-bind was no
  longer load-bearing and the user-service `handle` blocks were dropped from
  `isnad.*`. That vhost is now a **pure frontend + isnad-graph-API surface**;
  the only `/auth/*` path it keeps is the `/auth/callback/*` frontend carve-out
  (AuthCallbackPage). All user-service routes — `/auth/*`, JWKS, the
  `/api/v1/*` user prefixes, and the `/api/v1/user-service/health` rewrite —
  live **solely** on `users.{$BASE_DOMAIN}`.

**Consequence for monitoring (deploy#449):** the post-deploy smoke battery
(`scripts/verify_{stg,prod}_smoke.sh`) moved its user-service checks to
`USER_SERVICE_BASE_URL` (the `users.*` vhost) in #414. The Prometheus blackbox
probes (`infra/prometheus/prometheus.{stg,prod}.yml`) were left probing
`isnad.*` and so 404'd / fell through to the SPA for those paths; deploy#449
re-pointed them to `users.*` to re-align with the smoke battery and the
runtime truth. The regression guard `scripts/tests/test_blackbox_partition.py`
asserts that no user-service-owned probe targets the `isnad.*` host.

**Rule going forward:** a new user-service prefix is added to the
`users.{$BASE_DOMAIN}` block **only** — never to `isnad.*`.

## References

- [`caddy/Caddyfile`](../caddy/Caddyfile) — canonical routing source
- #220 — `users.{$BASE_DOMAIN}` vhost carve-out
- #241 — dual-binding user-service routes on `isnad.{$BASE_DOMAIN}` for
  frontend relative-URL compatibility
- #243 — API-surface-tuned security headers on `users.{$BASE_DOMAIN}`
- #245 — frontend Phase 2: absolute URLs targeting `users.{$BASE_DOMAIN}`
- #133, user-service#67 — `/auth/callback/*` carve-out to frontend
  (AuthCallbackPage)
- PR #35 — original review where Jelani Mwangi surfaced the partition risk
