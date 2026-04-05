# CLAUDE.md — noorinalabs-deploy

This file provides guidance to Claude Code when working in the deployment orchestration repository.

## Project Overview

**noorinalabs-deploy** is the deployment orchestration repo for all NoorinALabs services. It owns everything that runs on the production server: Docker Compose configs, Terraform provisioning, reverse proxy, observability stack, and deployment workflows.

## Guiding Principle

**Service repos own what they build. This repo owns what runs on the server.**

## Directory Structure

| Directory | Purpose |
|-----------|---------|
| `.github/workflows/` | Deployment and verification workflows |
| `terraform/hetzner/` | Hetzner VPS provisioning (Terraform) |
| `compose/` | Production Docker Compose and .env template |
| `caddy/` | Caddyfile for reverse proxy |
| `infra/` | Prometheus, Grafana, Loki, Alertmanager, Promtail configs |
| `systemd/` | Backup service and timer units |
| `scripts/` | VPS bootstrap, deployment verification, backup/restore |

## Deployment Flow

1. Push to `noorinalabs-isnad-graph` main → CI passes → `notify-deploy.yml` fires `repository_dispatch`
2. This repo's `deploy-isnad-graph.yml` receives the event → SSHs to VPS → pulls images → `docker compose up`
3. `verify-deploy.yml` runs post-deploy health checks

## Key Files

- `compose/docker-compose.prod.yml` — production stack (image-only, no build contexts for app services)
- `caddy/Caddyfile` — reverse proxy routes
- `terraform/hetzner/main.tf` — VPS provisioning
- `scripts/verify_deployment.sh` — post-deploy verification script

## Team

| Role | Level | Name | Roster File |
|------|-------|------|-------------|
| Infrastructure Manager | Senior VP | Bereket Tadesse | `roster/manager_bereket.md` |
| Platform Architect | Staff | Weronika Zielinska | `roster/platform_architect_weronika.md` |
| SRE Engineer | Senior | Lucas Ferreira | `roster/sre_engineer_lucas.md` |
| SRE Engineer | Senior | Aisha Idrissi | `roster/sre_engineer_aisha.md` |
| Security Engineer | Senior | Nino Kavtaradze | `roster/security_engineer_nino.md` |
| Observability Engineer | Senior | Nurul Hakim | `roster/observability_engineer_nurul.md` |

## Team Workflow

See the org-level charter at `noorinalabs-main/.claude/team/charter.md` and this repo's charter at `.claude/team/charter.md`.

## Infrastructure Details

- **VPS:** Hetzner CPX41 (8 vCPU, 16GB RAM), Ubuntu 24.04, Ashburn
- **Services:** Neo4j, PostgreSQL+pgvector, Redis, FastAPI, React/nginx, Caddy, Prometheus, Grafana, Loki
- **Secrets:** Managed via GitHub Actions encrypted secrets with environment protection rules
