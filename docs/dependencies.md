# Cross-Repo Dependency Map

How NoorinALabs repositories depend on each other for builds, deployments, and secrets.

## Deployment Triggers

```
noorinalabs-isnad-graph (merge to main)
  → CI passes → notify-deploy.yml fires repository_dispatch
  → noorinalabs-deploy/deploy-isnad-graph.yml
  → SSH to VPS → docker compose up (api + frontend)

noorinalabs-landing-page (merge to main)
  → [future] notify-deploy.yml fires repository_dispatch
  → noorinalabs-deploy/deploy-landing-page.yml
  → SSH to VPS → docker compose up (landing-page)

noorinalabs-isnad-graph-ingestion
  → Not containerized — runs on VPS via make commands
  → No automated deployment
```

## Build Dependencies

```
noorinalabs-design-system (publish to GitHub Packages)
  → consumed by noorinalabs-landing-page at build time
  → consumed by noorinalabs-isnad-graph/frontend at build time

noorinalabs-isnad-graph
  → Dockerfile builds API image (Python/FastAPI)
  → frontend/Dockerfile builds frontend image (React/nginx)
  → Both images pushed to ghcr.io/noorinalabs/

noorinalabs-deploy
  → Pulls GHCR images at deploy time
  → Falls back to building from /opt/noorinalabs-isnad-graph on VPS if GHCR pull fails
  → infra/neo4j/Dockerfile builds custom Neo4j image with APOC + GDS plugins
```

## Deployment Sequencing

When changes span multiple repos, deploy in this order:

1. **noorinalabs-design-system** — publish package first (consumed at build time by downstream repos)
2. **noorinalabs-isnad-graph** — merge to main, CI builds + pushes GHCR images
3. **noorinalabs-deploy** — if compose/Caddy/infra configs changed, merge to main first
4. **Deploy trigger fires** — `deploy-isnad-graph.yml` pulls latest images and config

If only `noorinalabs-deploy` configs change (Caddyfile, compose, infra), the next deploy will pick them up automatically since the workflow runs `git reset --hard origin/main` on the VPS.

## Secret Dependencies

### Where secrets live

All production secrets are stored as **GitHub Actions encrypted secrets** in the `noorinalabs-deploy` repository, scoped to the `production` environment.

> **Common mistake:** Setting secrets in `noorinalabs-isnad-graph` instead of `noorinalabs-deploy`. The isnad-graph repo does NOT inject secrets at deploy time — only `noorinalabs-deploy` does.

### Secret flow

```
GitHub Secrets (noorinalabs-deploy, production environment)
  → SSH environment variables (appleboy/ssh-action)
  → .env file on VPS (/opt/noorinalabs-deploy/.env, chmod 600)
  → Docker Compose environment variables
```

### Required secrets

| Secret | Source | Used By |
|--------|--------|---------|
| `NEO4J_PASSWORD` | Generated | neo4j, api |
| `POSTGRES_USER` | Generated | postgres, api, postgres-exporter |
| `POSTGRES_PASSWORD` | Generated | postgres, api, postgres-exporter |
| `POSTGRES_DB` | Generated | postgres, api, postgres-exporter |
| `REDIS_PASSWORD` | Generated | redis, api |
| `AUTH_GOOGLE_CLIENT_ID` | Google Cloud Console (OAuth 2.0) | api |
| `AUTH_GOOGLE_CLIENT_SECRET` | Google Cloud Console (OAuth 2.0) | api |
| `AUTH_GITHUB_CLIENT_ID` | GitHub Developer Settings (OAuth App) | api |
| `AUTH_GITHUB_CLIENT_SECRET` | GitHub Developer Settings (OAuth App) | api |
| `GRAFANA_ADMIN_PASSWORD` | Generated | grafana |
| `DEPLOY_SSH_PRIVATE_KEY` | SSH key pair (matches VPS authorized_keys) | SSH connection |

### Required repository variables

| Variable | Purpose |
|----------|---------|
| `VPS_HOST` | Hostname or IP of the production VPS |

### Backup secrets (on VPS)

| Variable | Source | Used By |
|----------|--------|---------|
| `B2_KEY_ID` | Backblaze B2 | backup.sh, restore.sh |
| `B2_APP_KEY` | Backblaze B2 | backup.sh, restore.sh |
| `B2_BUCKET` | Backblaze B2 | backup.sh, restore.sh |

## DNS Requirements

| Domain | Record Type | Points To |
|--------|-------------|-----------|
| `isnad-graph.noorinalabs.com` | A | VPS public IPv4 (from `terraform output server_ip`) |
| `isnad-graph.noorinalabs.com` | AAAA | VPS public IPv6 (from `terraform output server_ipv6`) |

Caddy auto-provisions TLS certificates via ACME (Let's Encrypt). DNS must resolve before the first deploy or TLS provisioning will fail.
