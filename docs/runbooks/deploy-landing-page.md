# Runbook: Deploy noorinalabs-landing-page

> **Status:** Not yet implemented. This runbook will be completed when automated deployment is set up for the landing page (see noorinalabs-deploy issues #2 and #3).

## Planned Deployment Flow

The landing page will follow the same pattern as isnad-graph:

1. Code merges to `main` in `noorinalabs-landing-page`
2. `notify-deploy.yml` fires a `repository_dispatch` event to `noorinalabs-deploy`
3. A new `deploy-landing-page.yml` workflow receives the event
4. The workflow SSHs to the VPS, pulls the new image, and runs `docker compose up`
5. Post-deploy verification confirms the landing page is serving

## Prerequisites (before this runbook is active)

- [ ] Landing page Dockerfile exists in `noorinalabs-landing-page`
- [ ] `deploy-landing-page.yml` workflow created in `noorinalabs-deploy`
- [ ] `notify-deploy.yml` workflow created in `noorinalabs-landing-page`
- [ ] Landing page service added to `compose/docker-compose.prod.yml`
- [ ] Caddy route added for the landing page domain
- [ ] DNS configured for the landing page domain
- [ ] Rollback workflow updated to include landing page as a service option

## Temporary: Manual Deploy

Until automated deployment is configured, deploy the landing page manually:

```bash
ssh deploy@<VPS_HOST>
cd /opt/noorinalabs-deploy

# Pull latest landing page source
cd /opt/noorinalabs-landing-page
git fetch origin main
git reset --hard origin/main
cd /opt/noorinalabs-deploy

# Build and deploy (once the service is added to docker-compose.prod.yml)
docker compose -p noorinalabs -f compose/docker-compose.prod.yml --env-file .env up -d --build landing-page
```
