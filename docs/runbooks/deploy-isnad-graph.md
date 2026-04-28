# Runbook: Deploy noorinalabs-isnad-graph

Step-by-step guide for deploying the isnad-graph application to the production VPS.

## Pre-Deployment Checklist

- [ ] CI passes on `noorinalabs-isnad-graph` main branch
- [ ] All required secrets are current in GitHub Actions (see [dependencies.md](../dependencies.md))
- [ ] VPS is accessible via SSH (`ssh deploy@<VPS_HOST>`)
- [ ] Disk space is adequate (`df -h /` — ensure > 5 GB free)
- [ ] No other deployment is in progress (check Actions tab for `deploy-production` concurrency group)

## Automated Deployment (Primary Path)

No manual action required. When code merges to `main` in `noorinalabs-isnad-graph`:

1. `noorinalabs-isnad-graph/.github/workflows/notify-deploy.yml` fires a `repository_dispatch` event
2. `noorinalabs-deploy/.github/workflows/deploy-isnad-graph.yml` receives the event
3. The workflow SSHs to the VPS and runs the full deployment sequence
4. `verify-deploy.yml` runs automatically after a successful deploy

Monitor progress in the [Actions tab](https://github.com/noorinalabs/noorinalabs-deploy/actions).

## Manual Deployment (workflow_dispatch)

Use this when you need to deploy a specific image tag or re-trigger a failed deploy.

1. Go to [Actions > Deploy noorinalabs-isnad-graph](https://github.com/noorinalabs/noorinalabs-deploy/actions/workflows/deploy-isnad-graph.yml)
2. Click **Run workflow**
3. Set `image_tag` (default: `latest`) — use a specific tag like `sha-a1b2c3d` if needed
4. Click **Run workflow** to start

## Full-Stack Deploy

To deploy all 13 services (including infrastructure):

1. Go to [Actions > Deploy All](https://github.com/noorinalabs/noorinalabs-deploy/actions/workflows/deploy-all.yml)
2. Click **Run workflow**
3. Set `isnad_image_tag` (default: `latest`)
4. Click **Run workflow**

## What Happens During Deploy

1. SSH to VPS as `deploy` user
2. Pull latest deploy config: `git fetch origin main && git reset --hard origin/main` in `/opt/noorinalabs-deploy`
3. Write `.env` from GitHub secrets (recreated fresh each deploy, `chmod 600`)
4. Authenticate to GHCR: `docker login ghcr.io` with auto-provisioned `GITHUB_TOKEN` (logout on exit via trap)
5. Pull images from GHCR for `api`, `frontend`, `landing`, `user-service`
6. Start services: `docker compose -p noorinalabs -f compose/docker-compose.prod.yml --env-file .env up -d --force-recreate --remove-orphans`
7. Health check loop: polls API container up to 24 times at 5-second intervals (120s total)

## Post-Deployment Verification

`verify-deploy.yml` is split by environment (deploy#87) and runs
automatically after a successful deploy:

- **Stg deploy** → `verify-stg` job runs the full cross-repo integration
  suite (`integration-tests/run-tests.sh`). Failure blocks prod-promote
  (gate enforcement: deploy#179, follow-up).
- **Prod deploy** → `verify-prod` job runs `scripts/verify_prod_smoke.sh`
  (<60s smoke battery: health 200s, narrator query, JWKS, auth wiring).
  Failure surfaces a `::error::` annotation; no auto-rollback.

To run manually:

1. Go to [Actions > Verify Deployment](https://github.com/noorinalabs/noorinalabs-deploy/actions/workflows/verify-deploy.yml)
2. Click **Run workflow**, select `target` (`stg` or `prod`)

For broader manual verification against an arbitrary env (legacy/operator
script, not invoked by CI anymore — used by the user-service migration
runbook):

```bash
SITE_URL=https://isnad-graph.noorinalabs.com ./scripts/verify_deployment.sh --skip-workflow
```

This broader script checks: site reachability (HTTP 200), API health
endpoint, API status, endpoint smoke tests, security headers, Caddy
config, SSL certificate, and response time.

## Rollback

### Via GitHub Actions (recommended)

1. Go to [Actions > Rollback](https://github.com/noorinalabs/noorinalabs-deploy/actions/workflows/rollback.yml)
2. Click **Run workflow**
3. Set:
   - `image_tag`: the tag to roll back to (e.g., `sha-a1b2c3d` — find previous tags in GHCR or past deploy run summaries)
   - `service`: `all`, `api`, or `frontend`
4. Click **Run workflow**

### Manual Rollback via SSH

If GitHub Actions is unavailable:

```bash
ssh deploy@<VPS_HOST>
cd /opt/noorinalabs-deploy

# Update the image tag in .env
sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG="sha-a1b2c3d"/' .env

# Roll back api and frontend
docker compose -p noorinalabs -f compose/docker-compose.prod.yml --env-file .env pull api frontend
docker compose -p noorinalabs -f compose/docker-compose.prod.yml --env-file .env up -d --force-recreate api frontend

# Verify
sleep 15
docker compose -p noorinalabs -f compose/docker-compose.prod.yml ps --format '{{.Name}}\t{{.Status}}'
```

### Finding Previous Image Tags

```bash
# List recent GHCR tags for the API image
gh api orgs/noorinalabs/packages/container/noorinalabs-isnad-graph/versions \
  --jq '.[].metadata.container.tags[]' | head -20

# Check previous deploy run summaries in GitHub Actions for the IMAGE_TAG used
```

## Emergency: Service Won't Start

1. SSH to VPS: `ssh deploy@<VPS_HOST>`
2. Check container logs: `docker compose -p noorinalabs -f compose/docker-compose.prod.yml logs --tail=100 api`
3. Check container status: `docker compose -p noorinalabs -f compose/docker-compose.prod.yml ps`
4. If the issue is a bad image, roll back (see above)
5. If the issue is infrastructure (Neo4j, Postgres, Redis), check their logs and consider restarting: `docker compose -p noorinalabs -f compose/docker-compose.prod.yml restart neo4j`
