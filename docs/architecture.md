# Architecture

This document is the definitive reference for how the Noorina Labs deployment system works.

## Table of Contents

- [Overview](#overview)
- [File Structure](#file-structure)
- [Deployment Triggering](#deployment-triggering)
- [Deployment Flow](#deployment-flow)
- [Secrets Management](#secrets-management)
- [Infrastructure](#infrastructure)
- [Docker Compose Stack](#docker-compose-stack)
- [Caddy Configuration](#caddy-configuration)
- [Post-Deployment Verification](#post-deployment-verification)
- [Rollback](#rollback)
- [Observability](#observability)
- [Backup and Restore](#backup-and-restore)

## Overview

`noorinalabs-deploy` orchestrates deployments for the Noorina Labs platform. It owns everything running on the production VPS: Docker Compose services, infrastructure provisioning, reverse proxy configuration, and observability.

**Core principle:** Service repos own what they build; this repo owns what runs on the server.

## File Structure

```
noorinalabs-deploy/
├── .github/workflows/
│   ├── deploy-isnad-graph.yml    # Primary deploy (repository_dispatch + manual)
│   ├── deploy-all.yml            # Manual full-stack deploy
│   ├── verify-deploy.yml         # Post-deploy verification
│   └── rollback.yml              # Manual rollback
├── caddy/
│   └── Caddyfile                 # Reverse proxy and security headers
├── compose/
│   └── docker-compose.prod.yml   # Production Docker Compose (13 services)
├── infra/
│   ├── alertmanager/
│   │   └── alertmanager.yml
│   ├── grafana/
│   │   ├── dashboards/
│   │   │   └── api-overview.json
│   │   └── provisioning/
│   │       ├── dashboards/dashboards.yml
│   │       └── datasources/datasource.yml
│   ├── loki/
│   │   └── loki-config.yml
│   ├── neo4j/
│   │   └── Dockerfile
│   ├── prometheus/
│   │   ├── alerts.yml
│   │   └── prometheus.yml
│   └── promtail/
│       └── promtail-config.yml
├── scripts/
│   ├── backup.sh                 # Automated database backup
│   ├── bootstrap-vps.sh          # First-time VPS setup
│   ├── restore.sh                # Database restore from backup
│   └── verify_deployment.sh      # Live deployment verification
├── systemd/
│   ├── isnad-backup.service      # Backup systemd unit
│   └── isnad-backup.timer        # Daily backup timer (03:00 UTC)
└── terraform/
    └── hetzner/
        ├── main.tf               # Server, firewall, SSH key
        ├── variables.tf          # Configuration variables
        ├── outputs.tf
        ├── versions.tf
        └── README.md
```

## Deployment Triggering

### Automated (primary path)

When code merges to `main` in `noorinalabs-isnad-graph`, its `notify-deploy.yml` workflow fires a `repository_dispatch` event of type `deploy-noorinalabs-isnad-graph` to this repo, which triggers `deploy-isnad-graph.yml`.

### Manual

All workflows support `workflow_dispatch` for manual triggering via the GitHub Actions UI:

| Workflow | Purpose | Key Inputs |
|----------|---------|------------|
| `deploy-isnad-graph.yml` | Deploy isnad-graph services | `image_tag` (default: `latest`) |
| `deploy-all.yml` | Full-stack deploy (all 13 services) | `isnad_image_tag` (default: `latest`) |
| `verify-deploy.yml` | Run verification checks | (none) |
| `rollback.yml` | Roll back to a previous image | `image_tag` (required), `service` (all/api/frontend) |

All deploy and rollback workflows share a concurrency group (`deploy-production`) that limits to one deployment at a time without cancelling in-progress runs.

## Deployment Flow

The primary deployment workflow (`deploy-isnad-graph.yml`) follows this sequence:

1. **SSH to VPS** as `deploy` user via `appleboy/ssh-action@v1`
2. **Pull latest deploy configs** — `git fetch origin main && git reset --hard origin/main` in `/opt/noorinalabs-deploy`
3. **Write `.env`** — all secrets are injected as SSH environment variables and written to `.env` with `chmod 600`. The file is recreated fresh each deployment.
4. **Pull latest isnad-graph source** — `git fetch && reset` in `/opt/noorinalabs-isnad-graph` (used as build context fallback)
5. **Try GHCR images first** — `docker compose pull api frontend` attempts to pull pre-built images from `ghcr.io/noorinalabs/`
6. **Fall back to local build** — if GHCR pull fails, `docker compose up --build` builds from the local source checkout
7. **Start services** — `docker compose up -d --build --force-recreate --remove-orphans`
8. **Health check loop** — polls the API container health status up to 24 times at 5-second intervals (120 seconds total). On failure, dumps the last 50 lines of API logs and exits non-zero.

## Secrets Management

All secrets are stored as GitHub Actions encrypted secrets scoped to the `production` environment.

### Injection path

```
GitHub Secrets → SSH env vars → .env file → docker compose environment
```

The `.env` file is ephemeral: recreated fresh on each deployment, written with `chmod 600`, and never committed to version control.

### Required secrets

| Secret | Used by |
|--------|---------|
| `NEO4J_PASSWORD` | neo4j, api |
| `POSTGRES_USER` | postgres, api, postgres-exporter |
| `POSTGRES_PASSWORD` | postgres, api, postgres-exporter |
| `POSTGRES_DB` | postgres, api, postgres-exporter |
| `REDIS_PASSWORD` | redis, api |
| `AUTH_GOOGLE_CLIENT_ID` | api |
| `AUTH_GOOGLE_CLIENT_SECRET` | api |
| `AUTH_GITHUB_CLIENT_ID` | api |
| `AUTH_GITHUB_CLIENT_SECRET` | api |
| `GRAFANA_ADMIN_PASSWORD` | grafana |
| `DEPLOY_SSH_PRIVATE_KEY` | SSH connection to VPS |

### Required repository variables

| Variable | Purpose |
|----------|---------|
| `VPS_HOST` | Hostname/IP of the production VPS |

### Derived at deploy time

| Variable | Source |
|----------|--------|
| `IMAGE_TAG` | Workflow input or `latest` |
| `CACHEBUST` | `date +%s` (forces frontend rebuild) |

## Infrastructure

### Server

| Property | Value |
|----------|-------|
| Provider | Hetzner Cloud |
| Server type | CPX41 (8 vCPU, 16 GB RAM) |
| OS | Ubuntu 24.04 |
| Location | Ashburn (`ash`) |
| Server name | `noorinalabs-isnad-graph-prod` |

Provisioned via Terraform in `terraform/hetzner/`. The Terraform configuration creates:

- `hcloud_server` — the VPS instance
- `hcloud_ssh_key` — deploy SSH key
- `hcloud_firewall` — allows SSH (22), HTTP (80), HTTPS (443)

### Bootstrap

First-time setup is performed by `scripts/bootstrap-vps.sh`, which runs as root on a fresh VPS and:

1. Installs Docker, docker-compose-v2, docker-buildx, git, curl
2. Creates a `deploy` user with Docker group access
3. Copies SSH authorized keys from root to deploy user
4. Clones this repo to `/opt/noorinalabs-deploy`
5. Creates a template `.env` file with placeholder values
6. Installs rclone for backups
7. Installs and enables the backup systemd timer

## Docker Compose Stack

Defined in `compose/docker-compose.prod.yml`. The stack runs 13 services under the `noorinalabs` project name.

### Services

| Service | Image | Role | Networks |
|---------|-------|------|----------|
| `api` | `ghcr.io/noorinalabs/noorinalabs-isnad-graph:{tag}` | FastAPI application (uvicorn, 4 workers) | backend, frontend |
| `frontend` | `ghcr.io/noorinalabs/noorinalabs-isnad-graph-frontend:{tag}` | React SPA served by nginx | frontend |
| `neo4j` | Built from `infra/neo4j/Dockerfile` (neo4j:5-community) | Graph database (APOC + GDS plugins) | backend |
| `postgres` | `pgvector/pgvector:pg16` | Relational database with vector support | backend |
| `redis` | `redis:7-alpine` | Cache (512 MB, allkeys-lru eviction) | backend |
| `caddy` | `caddy:2-alpine` | Reverse proxy with auto TLS | backend, frontend |
| `prometheus` | `prom/prometheus:v3.4.0` | Metrics collection (30-day retention) | backend |
| `alertmanager` | `prom/alertmanager:v0.28.1` | Alert routing | backend |
| `grafana` | `grafana/grafana:11.6.0` | Dashboards at `/grafana` | backend |
| `loki` | `grafana/loki:2.9.10` | Log aggregation | backend |
| `promtail` | `grafana/promtail:2.9.10` | Log collection from Docker containers | backend |
| `node-exporter` | `prom/node-exporter:v1.9.1` | Host-level metrics | backend |
| `postgres-exporter` | `prometheuscommunity/postgres-exporter:v0.16.0` | PostgreSQL metrics | backend |

### Networks

| Network | Type | Purpose |
|---------|------|---------|
| `backend` | bridge, internal | Database/observability traffic (not exposed to host) |
| `frontend` | bridge | Caddy-to-application traffic |

### Security hardening

- **Read-only containers** with `read_only: true` on `api`, `frontend`, `redis`, `caddy` — writable paths use tmpfs mounts
- **Resource limits** on all services (memory and CPU caps plus reservations)
- **Health checks** on all services with configurable intervals, timeouts, retries, and start periods
- **Logging limits** — json-file driver with max-size (10m) and max-file (3-5) on all services
- **Localhost-only ports** for databases — postgres and redis bind to `127.0.0.1` only

## Caddy Configuration

Defined in `caddy/Caddyfile`. Caddy serves as the TLS-terminating reverse proxy for `isnad-graph.noorinalabs.com`.

### Routing

| Path | Upstream |
|------|----------|
| `/api/*` | `api:8000` |
| `/health` | `api:8000` |
| `/status` | `api:8000` |
| `/metrics` | `api:8000` |
| `/grafana/*` | `grafana:3000` |
| `/` (default) | `frontend:80` |

### Security headers

All responses include:

- `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-XSS-Protection: 0` (OWASP-recommended: disable browser XSS filter)
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Content-Security-Policy` restricting script/style/font/connect sources
- `Server` header removed

### TLS

Caddy automatically obtains and renews certificates via ACME (Let's Encrypt).

## Post-Deployment Verification

The `verify-deploy.yml` workflow runs automatically after a successful deploy (via `workflow_run` trigger) and can also be triggered manually.

Verification is performed by `scripts/verify_deployment.sh`, which checks:

1. **Live site reachability** — HTTP 200 from the root URL
2. **API health endpoint** — `/health` or `/api/v1/health` returns a healthy status
3. **API status endpoint** — `/status` reports operational state
4. **Endpoint smoke tests** — requests to `/api/v1/narrators`, `/api/v1/hadiths`, `/api/v1/collections`, `/api/v1/search`, `/api/v1/parallels`, `/api/v1/timeline` (expects 200, 401, or 403)
5. **Security headers** — validates all six required headers are present
6. **Caddy config verification** — confirms security headers originate from Caddy
7. **SSL certificate** — checks validity period and subject match
8. **Response time** — health endpoint must respond within 500ms (warning) / 2000ms (failure)

Results are uploaded as a GitHub Actions artifact and written to the job summary.

## Rollback

Rollback is a manual process via `workflow_dispatch` on `rollback.yml`.

### Inputs

| Input | Description |
|-------|-------------|
| `image_tag` | Required. Tag to roll back to (e.g., `sha-a1b2c3d`, `phase12-wave1`) |
| `service` | Choice of `all`, `api`, or `frontend` (default: `all`) |

### Process

1. SSH to VPS as `deploy` user
2. Update `IMAGE_TAG` in the existing `.env` file (all other secrets remain unchanged)
3. Pull the specified image tag from GHCR (with fallback)
4. Recreate the target service(s) with `docker compose up -d --force-recreate`
5. Wait 15 seconds and report container status

Rollback shares the `deploy-production` concurrency group with regular deploys.

## Observability

### Metrics (Prometheus)

Configured in `infra/prometheus/prometheus.yml`. Scrapes three targets at 15-second intervals:

| Job | Target | Metrics |
|-----|--------|---------|
| `api` | `api:8000/metrics` | Application metrics |
| `node-exporter` | `node-exporter:9100` | Host CPU, memory, disk, network |
| `postgres-exporter` | `postgres-exporter:9187` | PostgreSQL query/connection stats |

Storage retention: 30 days. Alert rules defined in `infra/prometheus/alerts.yml`.

### Logging (Loki + Promtail)

- **Promtail** reads container logs from `/var/lib/docker/containers` via read-only Docker socket mount
- **Loki** aggregates and indexes logs, configured via `infra/loki/loki-config.yml`

### Dashboards (Grafana)

- Served at `https://isnad-graph.noorinalabs.com/grafana`
- Pre-provisioned dashboard: `infra/grafana/dashboards/api-overview.json`
- Datasources provisioned automatically via `infra/grafana/provisioning/datasources/datasource.yml`

### Alerting (Alertmanager)

- Receives alerts from Prometheus based on rules in `infra/prometheus/alerts.yml`
- Routing configured in `infra/alertmanager/alertmanager.yml`

## Backup and Restore

### Backup

Automated via systemd timer (`systemd/isnad-backup.timer`): daily at 03:00 UTC with up to 5 minutes of randomized delay.

The backup script (`scripts/backup.sh`):

1. Dumps PostgreSQL using `pg_dump --format=custom`
2. Stops Neo4j, runs `neo4j-admin database dump`, restarts Neo4j
3. Compresses Neo4j dump with zstd
4. Generates SHA256 checksums for all dump files
5. Uploads to Backblaze B2 via rclone
6. Prunes old backups: retains 7 daily + 4 weekly (Sundays)
7. Cleans up local staging directory on exit (including on failure)

Required environment: `B2_KEY_ID`, `B2_APP_KEY`, `B2_BUCKET`.

### Restore

`scripts/restore.sh` supports restoring from any backup:

```bash
./scripts/restore.sh latest                  # Most recent backup
./scripts/restore.sh daily/2026-03-25        # Specific date
./scripts/restore.sh --list                  # List available backups
./scripts/restore.sh --force latest          # Skip confirmation
```

Restore process:

1. Downloads backup from B2
2. Verifies SHA256 checksums
3. Restores PostgreSQL via `pg_restore --clean --if-exists`
4. Stops Neo4j, loads dump via `neo4j-admin database load --overwrite-destination`, restarts
5. Cleans up staging directory
