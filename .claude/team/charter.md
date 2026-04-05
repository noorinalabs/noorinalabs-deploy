# Repo Charter — noorinalabs-deploy

This charter supplements the org-level charter at `noorinalabs-main/.claude/team/charter.md`. All org charter rules apply.

## team_name

All agents working in this repo use `team_name: "noorinalabs-deploy"`.

## Scope

Deployment orchestration for all NoorinALabs services. Owns:
- Production Docker Compose configuration
- Terraform infrastructure provisioning (Hetzner VPS)
- Reverse proxy (Caddy) configuration
- Observability stack (Prometheus, Grafana, Loki)
- Backup/restore scripts and systemd units
- GitHub Actions deployment workflows
- Cross-repo deployment coordination

## Deployments Branching

- **Pattern:** `deployments/wave-{M}` (no phase prefix — infrastructure changes)

## Key Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `deploy-isnad-graph.yml` | `repository_dispatch` from noorinalabs-isnad-graph | Deploy noorinalabs-isnad-graph services |
| `deploy-all.yml` | Manual dispatch | Full stack redeploy with sequencing |
| `verify-deploy.yml` | After deploy succeeds | Post-deploy health checks |
| `rollback.yml` | Manual dispatch | Emergency rollback to specific image tag |

## Build & Test Commands

No application code — infrastructure and config only. Test by running workflows.
