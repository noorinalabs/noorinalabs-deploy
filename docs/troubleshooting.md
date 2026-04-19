# Troubleshooting

Common production issues and how to resolve them.

## OAuth 401 Errors

**Symptom:** Users get 401 Unauthorized when trying to log in via Google or GitHub OAuth.

**Root cause:** OAuth client ID or secret mismatch between what the API expects and what is configured.

**Resolution:**

1. Verify secrets are set in the correct repo — **`noorinalabs-deploy`**, NOT `noorinalabs-isnad-graph`:
   ```bash
   gh secret list -R noorinalabs/noorinalabs-deploy --env production
   ```
2. Compare the `AUTH_GOOGLE_CLIENT_ID` / `AUTH_GITHUB_CLIENT_ID` values with what is configured in:
   - [Google Cloud Console](https://console.cloud.google.com/apis/credentials) — OAuth 2.0 Client IDs
   - [GitHub Developer Settings](https://github.com/settings/developers) — OAuth Apps
3. Verify the OAuth redirect URI matches `https://isnad-graph.noorinalabs.com` in both providers
4. After updating secrets, re-run the deploy workflow to inject the new values into the VPS `.env` file

> Secrets are NOT hot-reloaded. A new deploy is required after any secret change.

## Container Health Check Failures

**Symptom:** Deploy workflow fails at the health check step. API container reports `unhealthy`.

**Diagnosis:**

```bash
ssh deploy@<VPS_HOST>
cd /opt/noorinalabs-deploy

# Check container status
docker compose -p noorinalabs -f compose/docker-compose.prod.yml ps

# Check API logs
docker compose -p noorinalabs -f compose/docker-compose.prod.yml logs --tail=100 api

# Check if dependencies are healthy
docker inspect --format='{{.State.Health.Status}}' noorinalabs-neo4j-1
docker inspect --format='{{.State.Health.Status}}' noorinalabs-postgres-1
docker inspect --format='{{.State.Health.Status}}' noorinalabs-redis-1
```

**Common causes:**

- **Database not ready:** Neo4j takes 30+ seconds to start. The API has `depends_on` with `condition: service_healthy`, but if Neo4j is slow the API may timeout.
- **Bad image:** A broken image was pushed. Roll back to a known-good tag (see [deploy runbook](runbooks/deploy-isnad-graph.md#rollback)).
- **Missing environment variables:** Check that `.env` contains all required variables: `cat /opt/noorinalabs-deploy/.env`
- **Port conflict:** Another process is using port 8000. Check with `ss -tlnp | grep 8000`.

## Caddy TLS Provisioning Issues

**Symptom:** Site returns connection errors or browser shows certificate warning.

**Diagnosis:**

```bash
ssh deploy@<VPS_HOST>

# Check Caddy logs
docker compose -p noorinalabs -f compose/docker-compose.prod.yml logs --tail=50 caddy

# Check if ports 80 and 443 are open
ss -tlnp | grep -E ':(80|443)\b'
```

**Common causes:**

- **DNS not pointing to VPS:** Caddy needs DNS to resolve to the VPS IP for ACME challenge. Verify: `dig isnad-graph.noorinalabs.com +short`
- **Port 80 blocked:** ACME HTTP-01 challenge requires port 80. Check the Hetzner firewall rules.
- **Rate limiting:** Let's Encrypt has rate limits (50 certs/week per domain). If hit, wait or use the staging ACME server.
- **Stale Caddy config:** After updating the Caddyfile, restart Caddy: `docker compose -p noorinalabs -f compose/docker-compose.prod.yml restart caddy`

## Docker Image Pull Failures (GHCR Auth)

**Symptom:** Deploy logs show `GHCR pull failed — will build locally` and the build takes much longer than expected (or fails).

**Diagnosis:**

```bash
ssh deploy@<VPS_HOST>

# Test GHCR pull manually
docker pull ghcr.io/noorinalabs/noorinalabs-isnad-graph:latest
```

**Common causes:**

- **GHCR images not published:** CI in `noorinalabs-isnad-graph` may not have pushed images. Check the CI workflow in that repo.
- **Authentication required:** If the GHCR package is private, Docker needs to be logged in:
  ```bash
  echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin
  ```
- **Image tag doesn't exist:** The requested `IMAGE_TAG` may not exist in GHCR. List available tags:
  ```bash
  gh api orgs/noorinalabs/packages/container/noorinalabs-isnad-graph/versions \
    --jq '.[].metadata.container.tags[]' | head -20
  ```

**No fallback:** As of the GHCR cutover (`noorinalabs-main#145`), application services are image-only — there is no local-build fallback. If `docker compose pull` fails, the deploy fails. Fix the GHCR auth/tag/image issue and re-run; do not attempt to `--build` on the VPS.

## VPS Disk Space / Resource Exhaustion

**Symptom:** Containers crash, fail to start, or deployments fail with `no space left on device`.

**Diagnosis:**

```bash
ssh deploy@<VPS_HOST>

# Check disk usage
df -h /

# Check Docker disk usage
docker system df

# Check memory
free -h

# Check for large log files
du -sh /var/lib/docker/containers/*/
```

**Resolution:**

```bash
# Prune unused Docker resources (images, containers, networks, build cache)
docker system prune -a --volumes --filter "until=72h"

# If Docker volumes are consuming space, check which ones are large
docker system df -v | head -40

# Prune only dangling images (safer)
docker image prune

# Check and rotate container logs (should be auto-limited by compose config)
# All services have max-size: 10m, max-file: 3-5 in docker-compose.prod.yml
```

**Prevention:**

- All services have logging limits configured (`max-size: 10m`, `max-file: 3-5`)
- Docker resource limits are set on all services (memory + CPU)
- Daily backups run at 03:00 UTC and prune old backups automatically (7 daily + 4 weekly)

## Neo4j Out of Memory

**Symptom:** Neo4j container restarts repeatedly or queries time out.

**Diagnosis:**

```bash
ssh deploy@<VPS_HOST>
docker compose -p noorinalabs -f compose/docker-compose.prod.yml logs --tail=100 neo4j
```

**Resolution:**

Neo4j is configured with 4G heap and 2G page cache (6G total) with an 8G container limit. If data grows beyond this:

1. Increase limits in `compose/docker-compose.prod.yml`:
   - `NEO4J_server_memory_heap_max__size`
   - `NEO4J_server_memory_pagecache_size`
   - `deploy.resources.limits.memory`
2. Ensure the VPS has enough total RAM (CPX41 has 16 GB)
3. Re-deploy

## Deployment Workflow Not Triggering

**Symptom:** Merge to `noorinalabs-isnad-graph` main does not trigger a deploy.

**Check:**

1. Verify `notify-deploy.yml` exists and is enabled in `noorinalabs-isnad-graph`
2. Check the Actions tab in `noorinalabs-isnad-graph` for the `notify-deploy` workflow run
3. Verify the `repository_dispatch` event type matches: `deploy-noorinalabs-isnad-graph`
4. Check that the `DEPLOY_DISPATCH_TOKEN` secret is set and has `repo` scope in `noorinalabs-isnad-graph`
5. Trigger manually via `deploy-isnad-graph.yml` workflow_dispatch as a workaround
