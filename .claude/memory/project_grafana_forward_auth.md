---
name: project_grafana_forward_auth
description: "deploy#458 Grafana SSO bridge is 3-repo; forward_auth has no credential to validate on a /grafana nav (token is localStorage-only)"
metadata: 
  node_type: memory
  type: project
  originSessionId: b923c0f4-c87a-4bed-b4b8-91a79287509b
---

deploy#458 "admin-gated Grafana via forward_auth RBAC" is the DEPLOY slice of a 3-repo feature, not a deploy-only fix. The deploy slice (PR deploy#460, P5W4): `caddy/Caddyfile` `handle /grafana/*` → strip client X-Webauth-* → `forward_auth user-service:8000 /auth/forward-auth` → copy_headers → reverse_proxy grafana; `compose/docker-compose.prod.yml` grafana `GF_AUTH_PROXY_*` (trust X-WEBAUTH-USER, whitelist RFC1918); runbook `docs/runbooks/grafana-forward-auth-sso.md`.

KEY NON-OBVIOUS FINDING: a top-level browser NAVIGATION to isnad.*/grafana/ carries NO credential forward_auth can use — the app access token lives ONLY in the SPA localStorage (sent as Authorization: Bearer on fetches), and the refresh cookie is host-only on users.* path=/auth. So `/auth/token/validate` (Bearer-only) is the WRONG endpoint. End-to-end SSO REQUIRES 2 companions: (a) user-service cookie-based `GET /auth/forward-auth`, (b) frontend change to carry a session credential to isnad.* on link-click (ig#1073 only HID the links). Carry approach (parent-domain cookie vs oauth2-proxy vs OIDC) needed OWNER SIGN-OFF (#458 flagged "security-sensitive") — RESOLVED, see OWNER DECISION block below.

HARD CUTOVER: once forward_auth is live, Grafana's own login form is unreachable through Caddy → do NOT promote to stg/prod before user-service `/auth/forward-auth` ships (else /grafana 502s). Break-glass = SSH-tunnel to grafana:3000. Loki kept INTERNAL (no public /loki/* route — would expose raw Loki API); admin logs via Grafana Explore.

**OWNER DECISION 2026-06-14 (resolved):** carry approach = **parent-domain session cookie** (Domain=noorinalabs.com, HttpOnly+Secure+SameSite=Lax, short TTL, RS256-signed from app bearer). Owner accepted the all-subdomain cookie-surface trade-off; rejected oauth2-proxy sidecar / user-service-OIDC alternatives. Companions filed P5W4: **user-service#171** (mint endpoint + cookie-based `GET /auth/forward-auth` → 2xx+X-Webauth-User/Role for admins, 401 no-cookie, 403 non-admin; impl Idris Yusuf, rev Anya Kowalczyk+Nadia Khoury) and **isnad-graph#1081** (frontend carry on /grafana click + re-enable ig#1073-hidden links; builds against us#171 contract). deploy#460 reviewed by Nino Kavtaradze+Weronika Zielinska; merge to wave branch OK on approval, but STILL do-not-promote until us#171 lands on target env.
