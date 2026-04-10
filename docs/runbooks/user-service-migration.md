# Runbook: User Service Data Migration (Neo4j to PostgreSQL)

Production migration of user data from the isnad-graph Neo4j database to the
noorinalabs-user-service PostgreSQL database.

**Estimated total time:** 60-90 minutes (depending on user count and verification depth)

## 1. Pre-Migration Checklist

**Estimated time:** 10 minutes

- [ ] Schedule a maintenance window — notify stakeholders
- [ ] Confirm no other deployments are in progress (check [Actions tab](https://github.com/noorinalabs/noorinalabs-deploy/actions))
- [ ] SSH access to VPS is working: `ssh deploy@<VPS_HOST>`
- [ ] Disk space adequate: `df -h /` (ensure > 10 GB free for backups)

### Back up Neo4j

```bash
# On the VPS
cd /opt/noorinalabs-deploy

# Run the automated backup script (dumps Neo4j + PostgreSQL, uploads to B2)
./scripts/backup.sh

# Or manually dump Neo4j only:
docker compose -f compose/docker-compose.prod.yml exec neo4j \
  neo4j-admin database dump --to-path=/data/backups neo4j

# Copy the dump to the host
docker compose -f compose/docker-compose.prod.yml cp \
  neo4j:/data/backups/neo4j.dump /tmp/neo4j-pre-migration.dump
```

### Back up user-service PostgreSQL

```bash
docker compose -f compose/docker-compose.prod.yml exec user-postgres \
  pg_dump -U "${USER_POSTGRES_USER}" "${USER_POSTGRES_DB}" \
  > /tmp/user-postgres-pre-migration.sql
```

### Verify service health

```bash
# All services healthy
docker compose -f compose/docker-compose.prod.yml ps --format "table {{.Service}}\t{{.Status}}"

# isnad-graph API
curl -sf https://isnad-graph.noorinalabs.com/health

# user-service
curl -sf https://isnad-graph.noorinalabs.com/api/v1/user-service/health
```

---

## 2. Infrastructure Deployment

**Estimated time:** 5 minutes

Verify the user-service infrastructure stack is running (deployed in Phase 3 Wave 2):

```bash
docker compose -f compose/docker-compose.prod.yml ps \
  user-service user-postgres user-redis user-postgres-exporter
```

All four services should show `Up (healthy)`. If any are missing or unhealthy:

```bash
docker compose -f compose/docker-compose.prod.yml up -d \
  user-postgres user-redis user-postgres-exporter user-service
```

Wait for health checks to pass:

```bash
docker compose -f compose/docker-compose.prod.yml ps --format "table {{.Service}}\t{{.Status}}" | grep user
```

---

## 3. Service Deployment

**Estimated time:** 5 minutes

### Verify user-service health endpoint

```bash
# Direct container check
docker compose -f compose/docker-compose.prod.yml exec user-service \
  python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read().decode())"

# Via Caddy (public)
curl -sf https://isnad-graph.noorinalabs.com/api/v1/user-service/health
```

### Verify JWKS endpoint

```bash
# JWKS keys are served at the IETF well-known path
curl -sf https://isnad-graph.noorinalabs.com/.well-known/jwks.json | python3 -m json.tool

# Confirm at least one RSA key is present
curl -sf https://isnad-graph.noorinalabs.com/.well-known/jwks.json | python3 -c "
import json, sys
data = json.load(sys.stdin)
keys = data.get('keys', [])
assert len(keys) > 0, 'No JWKS keys found'
print(f'OK: {len(keys)} key(s) available')
"
```

---

## 4. Caddy Routing

**Estimated time:** 5 minutes

Verify all user-service routes are active and proxying correctly:

```bash
# Auth endpoints
curl -sf -o /dev/null -w "%{http_code}" https://isnad-graph.noorinalabs.com/auth/login

# User management
curl -sf -o /dev/null -w "%{http_code}" https://isnad-graph.noorinalabs.com/api/v1/users

# Sessions
curl -sf -o /dev/null -w "%{http_code}" https://isnad-graph.noorinalabs.com/api/v1/sessions

# Subscriptions
curl -sf -o /dev/null -w "%{http_code}" https://isnad-graph.noorinalabs.com/api/v1/subscriptions

# Verification
curl -sf -o /dev/null -w "%{http_code}" https://isnad-graph.noorinalabs.com/api/v1/verification

# Roles
curl -sf -o /dev/null -w "%{http_code}" https://isnad-graph.noorinalabs.com/api/v1/roles

# 2FA
curl -sf -o /dev/null -w "%{http_code}" https://isnad-graph.noorinalabs.com/api/v1/2fa
```

Expect `401` or `405` (not `404`) — these indicate the request reached the
user-service (not the isnad-graph catch-all).

---

## 5. Data Migration

**Estimated time:** 15-30 minutes

The migration script lives in `noorinalabs-user-service` (US #11).

### Dry run

```bash
docker compose -f compose/docker-compose.prod.yml exec user-service \
  python -m src.scripts.migrate_from_neo4j \
    --neo4j-uri bolt://neo4j:7687 \
    --neo4j-user neo4j \
    --neo4j-password "${NEO4J_PASSWORD}" \
    --database-url "postgresql+asyncpg://${USER_POSTGRES_USER}:${USER_POSTGRES_PASSWORD}@user-postgres:5432/${USER_POSTGRES_DB}" \
    --dry-run
```

Review the dry-run output:
- [ ] Total user count matches expectations
- [ ] No errors or skipped records
- [ ] Role mappings look correct

### Real migration

```bash
docker compose -f compose/docker-compose.prod.yml exec user-service \
  python -m src.scripts.migrate_from_neo4j \
    --neo4j-uri bolt://neo4j:7687 \
    --neo4j-user neo4j \
    --neo4j-password "${NEO4J_PASSWORD}" \
    --database-url "postgresql+asyncpg://${USER_POSTGRES_USER}:${USER_POSTGRES_PASSWORD}@user-postgres:5432/${USER_POSTGRES_DB}"
```

Expected output: summary of migrated users, roles, and any warnings.

---

## 6. Verification

**Estimated time:** 10-15 minutes

### Compare user counts

```bash
# Neo4j user count
docker compose -f compose/docker-compose.prod.yml exec neo4j \
  cypher-shell -u neo4j -p "${NEO4J_PASSWORD}" \
  "MATCH (u:USER) RETURN count(u) AS user_count"

# PostgreSQL user count
docker compose -f compose/docker-compose.prod.yml exec user-postgres \
  psql -U "${USER_POSTGRES_USER}" -d "${USER_POSTGRES_DB}" \
  -c "SELECT count(*) AS user_count FROM users;"
```

Counts should match. Investigate any discrepancy before proceeding.

### Spot-check records

```bash
# Pick a few known users and verify their data migrated correctly
docker compose -f compose/docker-compose.prod.yml exec user-postgres \
  psql -U "${USER_POSTGRES_USER}" -d "${USER_POSTGRES_DB}" \
  -c "SELECT id, email, display_name, created_at FROM users LIMIT 5;"

# Verify roles migrated
docker compose -f compose/docker-compose.prod.yml exec user-postgres \
  psql -U "${USER_POSTGRES_USER}" -d "${USER_POSTGRES_DB}" \
  -c "SELECT u.email, r.name AS role FROM users u JOIN user_roles ur ON u.id = ur.user_id JOIN roles r ON r.id = ur.role_id LIMIT 10;"
```

### Test auth flows end-to-end

```bash
# Test login flow (replace with a test account)
curl -X POST https://isnad-graph.noorinalabs.com/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "<test-user-email>", "password": "<test-password>"}'

# Verify the returned JWT validates against JWKS
# (use the token from the login response)
curl -sf https://isnad-graph.noorinalabs.com/.well-known/jwks.json | python3 -c "
import json, sys
jwks = json.load(sys.stdin)
print(f'JWKS has {len(jwks[\"keys\"])} key(s) — JWT validation available')
"
```

---

## 7. Cutover

**Estimated time:** 5 minutes

By this point, the following are already in place:
- **isnad-graph validates JWTs via JWKS** (done in Phase 2) — it fetches the
  public key from `/.well-known/jwks.json` served by user-service
- **Frontend calls user-service directly** (done in Phase 3 Wave 2) — login,
  registration, session management all hit user-service endpoints

Cutover verification:

```bash
# Verify isnad-graph is fetching JWKS (check logs for JWKS fetch)
docker compose -f compose/docker-compose.prod.yml logs api --since 5m 2>&1 | grep -i jwks

# Verify frontend auth requests go to user-service (check user-service logs)
docker compose -f compose/docker-compose.prod.yml logs user-service --since 5m 2>&1 | grep -E "(POST|GET).*/auth/"

# Run the full deployment verification script
./scripts/verify_deployment.sh
```

---

## 8. Monitoring

**Estimated time:** Ongoing (monitor for 24-48 hours post-migration)

### Key metrics to watch

| Metric | Where | Alert threshold |
|--------|-------|-----------------|
| user-service health | Prometheus / Grafana | Any non-200 response |
| Auth success rate | user-service logs | Drop below 95% |
| JWT validation errors | isnad-graph logs | Any increase from baseline |
| Session creation rate | user-service metrics | Drop to zero |
| user-postgres connections | postgres-exporter (port 9187) | > 80% of max |
| user-service response latency | Prometheus | p99 > 2s |

### Grafana dashboards

```bash
# Access Grafana (authenticated)
# https://isnad-graph.noorinalabs.com/grafana

# Check Prometheus targets are scraping correctly
curl -sf http://localhost:9090/api/v1/targets | python3 -c "
import json, sys
data = json.load(sys.stdin)
for group in data['data']['activeTargets']:
    if 'user' in group.get('labels', {}).get('job', ''):
        print(f\"{group['labels']['job']}: {group['health']}\")
"
```

### Log monitoring

```bash
# Watch user-service logs for errors
docker compose -f compose/docker-compose.prod.yml logs -f user-service 2>&1 | grep -i error

# Watch isnad-graph for JWT validation failures
docker compose -f compose/docker-compose.prod.yml logs -f api 2>&1 | grep -iE "(jwt|jwks|auth).*error"
```

---

## 9. Rollback Procedure

If the migration fails or causes critical issues:

### Immediate (user-service still has dev-generated keys)

The user-service was deployed with development-generated JWT keys. If the
migration script fails partway through:

```bash
# 1. Restore user-service PostgreSQL from pre-migration backup
docker compose -f compose/docker-compose.prod.yml exec -T user-postgres \
  psql -U "${USER_POSTGRES_USER}" -d "${USER_POSTGRES_DB}" \
  < /tmp/user-postgres-pre-migration.sql

# 2. Restart user-service to clear any cached state
docker compose -f compose/docker-compose.prod.yml restart user-service
```

### Full rollback (revert to Neo4j auth)

The isnad-graph still has its old auth code in `src/auth/` (until IG #758
removes it). To revert completely:

```bash
# 1. Restore Neo4j from pre-migration backup (if USER nodes were modified)
docker compose -f compose/docker-compose.prod.yml exec neo4j \
  neo4j-admin database load --from-path=/data/backups neo4j --overwrite-destination

# 2. Restart isnad-graph with old auth config
#    (reconfigure environment to use local auth instead of JWKS)
#    Edit .env to remove/comment JWKS_URL, restart:
docker compose -f compose/docker-compose.prod.yml restart api

# 3. Revert frontend to call isnad-graph auth endpoints
#    (redeploy previous frontend image tag)
docker compose -f compose/docker-compose.prod.yml up -d \
  -e IMAGE_TAG=<previous-tag> frontend
```

### Verify rollback

```bash
# Confirm isnad-graph auth is working
curl -X POST https://isnad-graph.noorinalabs.com/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "<test-user-email>", "password": "<test-password>"}'

# Confirm Neo4j USER nodes are intact
docker compose -f compose/docker-compose.prod.yml exec neo4j \
  cypher-shell -u neo4j -p "${NEO4J_PASSWORD}" \
  "MATCH (u:USER) RETURN count(u)"
```

---

## 10. Cleanup

**Estimated time:** After 1-2 weeks of stable operation

Once the migration is verified stable and monitoring shows no issues:

### Remove Neo4j USER nodes

```bash
# Final backup before cleanup
./scripts/backup.sh

# Remove USER nodes and related relationships from Neo4j
docker compose -f compose/docker-compose.prod.yml exec neo4j \
  cypher-shell -u neo4j -p "${NEO4J_PASSWORD}" \
  "MATCH (u:USER) DETACH DELETE u RETURN count(u) AS deleted_count"
```

### Remove isnad-graph auth code

This is tracked as **IG #758** — remove the `src/auth/` directory from
noorinalabs-isnad-graph once the user-service is the sole auth provider.

### Remove pre-migration backups

```bash
# Only after confirming everything is stable
rm /tmp/neo4j-pre-migration.dump
rm /tmp/user-postgres-pre-migration.sql
```

### Update documentation

- [ ] Update `docs/architecture.md` to reflect user-service as the auth provider
- [ ] Archive this runbook or mark it as completed
