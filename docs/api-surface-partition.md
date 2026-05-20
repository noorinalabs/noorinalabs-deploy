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
| `/auth/callback/*` | frontend | OAuth post-login UI render (`AuthCallbackPage`); MUST be matched **before** `/auth/*` — see Caddyfile comment block at line 32. |
| `/auth/*` | user-service | OAuth provider redirects (`/auth/oauth/{provider}/login`, `/auth/oauth/{provider}/callback`), login, logout, refresh. |
| `/.well-known/jwks.json` | user-service | JWT verifier discovery (IETF well-known). |
| `/api/v1/users`, `/api/v1/users/*` | user-service | User CRUD. |
| `/api/v1/sessions`, `/api/v1/sessions/*` | user-service | Session lifecycle. |
| `/api/v1/subscriptions`, `/api/v1/subscriptions/*` | user-service | Billing / subscription. |
| `/api/v1/verification`, `/api/v1/verification/*` | user-service | Email / phone verification. |
| `/api/v1/roles`, `/api/v1/roles/*` | user-service | RBAC role assignment. |
| `/api/v1/2fa`, `/api/v1/2fa/*` | user-service | 2FA enroll / verify (TOTP). |
| `/api/v1/user-service/health` | user-service | Rewritten to `/health` on user-service — explicit so the isnad-vhost can probe user-service liveness without colliding with the isnad-graph-api `/health`. |
| `/api/*` (everything else) | isnad-graph-api | Catch-all for the isnad-graph domain. |
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

## Vhost split (post-#241)

Post-#241, user-service routes are **dual-bound** on `users.{$BASE_DOMAIN}`
AND `isnad.{$BASE_DOMAIN}`. This is the pragmatic two-phase migration
completion documented in the Caddyfile comment at line 18:

- **Phase 1 (current):** dual-binding both vhosts because the frontend
  hard-codes relative URLs (`/auth/oauth/{provider}/login`, `/api/v1/users`,
  …) — see `frontend/src/hooks/useAuth.ts` and
  `frontend/src/pages/LoginPage.tsx`. Removing the user-service routes from
  `isnad.*` entirely would break login.
- **Phase 2 (#245):** update the frontend to issue absolute URLs targeting
  `users.{$BASE_DOMAIN}`, then drop the user-service `handle` blocks from
  `isnad.*`.

Until #245 lands, **the partition table above applies to BOTH vhosts** for
the user-service-owned prefixes — i.e., a new user-service prefix must be
added to **both** the `isnad.{$BASE_DOMAIN}` block (transitional) and the
`users.{$BASE_DOMAIN}` block (permanent).

After #245 lands, the user-service rows in the `isnad.{$BASE_DOMAIN}`
partition table can be removed; the `/auth/callback/*` → frontend carve-out
and the isnad-graph-api rows remain.

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
