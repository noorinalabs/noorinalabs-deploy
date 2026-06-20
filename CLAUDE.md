# CLAUDE.md — noorinalabs-deploy

This file provides guidance to Claude Code when working in the deployment orchestration repository.

## Project Overview

**noorinalabs-deploy** is the deployment orchestration repo for all Noorina Labs services. It owns everything that runs on the production server: Docker Compose configs, Terraform provisioning, reverse proxy, observability stack, and deployment workflows.

## Guiding Principle

**Service repos own what they build. This repo owns what runs on the server.**

## Directory Structure

| Directory | Purpose |
|-----------|---------|
| `.github/workflows/` | Deployment and verification workflows |
| `terraform/hetzner/` | Hetzner VPS provisioning (Terraform) |
| `compose/` | Production Docker Compose and .env template |
| `caddy/` | Caddyfile for reverse proxy |
| `infra/` | Prometheus, Grafana, Loki, Alertmanager, Alloy configs |
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

## Project Memory

Project memory for this repo is **version-controlled in-repo** at `.claude/memory/`, not in the user-space auto-memory directory. This makes the accumulated state **transferable**: a developer who pulls a branch gets the memory with it, with zero per-machine setup. The index below is auto-loaded into every session via this committed CLAUDE.md import:

@.claude/memory/MEMORY.md

`MEMORY.md` is the always-loaded index (one line per memory); the individual topic files in `.claude/memory/*.md` are read on demand when a line looks relevant. This repo is **self-contained** — it imports only its own `.claude/memory/`, never the parent org corpus or sibling repos. The deploy-specific memories here were split out of the org-level `noorinalabs-main` corpus (deploy#479, from main#740 / driver main#732).

**Recording a memory:** create or edit `.claude/memory/<kebab-slug>.md` with the standard frontmatter (`name`, `description`, `metadata.type` = `user` | `feedback` | `project` | `reference`), add a one-line pointer to `MEMORY.md` (`- [Title](file.md) — hook`), and **commit it** so it travels with the branch. Link related memories with `[[other-slug]]`; cross-repo links into the org-level corpus are acceptable soft pointers and may dangle here. Before adding, check for an existing file covering the same fact and update it instead of duplicating; delete memories that turn out to be wrong.

> `.claude/memory/**` is excluded from the markdown/cspell/lychee linters (dense append-only note prose with names, SHAs, `[[wikilinks]]`, and Arabic) — same rationale as the org corpus, mirrored into this repo's `.markdownlint-cli2.jsonc`, `.cspell.json`, and `.lychee.toml`.

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

> **Cross-repo session-team note:** The team structure described below is the **per-repo team** — operative when a session is opened isolated in this repo for repo-only work.
>
> When work is orchestrated from the parent `noorinalabs-main` (the common case — wave kickoff, cross-repo features, wave-coordinated bug fixes), all spawned agents — regardless of which repo they edit — join the single `noorinalabs` session team. The per-repo roster below still governs **commit identity, domain ownership, and reviewer pairing**, but the team-creation surface lives in the orchestrator session, not here.
>
> See `noorinalabs-main/CLAUDE.md` § "Session team architecture" and `noorinalabs-main/.claude/team/charter/agents.md` § "Single-Leader Constraint" for the delegation pattern.

See the org-level charter at `noorinalabs-main/.claude/team/charter.md` and this repo's charter at `.claude/team/charter.md`.

## Infrastructure Details

- **VPS:** Hetzner CPX41 (8 vCPU, 16GB RAM), Ubuntu 24.04, Ashburn
- **Services:** Neo4j, PostgreSQL+pgvector, Redis, FastAPI, React/nginx, Caddy, Prometheus, Grafana, Loki
- **Secrets:** Managed via GitHub Actions encrypted secrets with environment protection rules
