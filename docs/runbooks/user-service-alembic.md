# Runbook: user-service alembic pre-deploy gate (deploy#85)

**Scope:** the `alembic upgrade head` pre-deploy gate that runs before any user-service deploy to stg or prod. Implemented in `.github/workflows/db-migrate.yml`, invoked by the promotion workflow (`promote.yml` — landed via [`deploy#155`](https://github.com/noorinalabs/noorinalabs-deploy/pull/155) for issue #84). The wiring step that adds `uses: ./.github/workflows/db-migrate.yml` to the promotion workflow is tracked in [`deploy#160`](https://github.com/noorinalabs/noorinalabs-deploy/issues/160).

**NOT in scope:**
- Data migrations (user data Neo4j→Postgres) — see `user-service-migration.md`.
- Fresh-volume alembic (first-ever boot on a new VPS) — tracked in `deploy#141`.
- Neo4j schema DDL — runs on isnad-graph startup, not alembic.

## Architecture summary

```
promotion workflow (promote.yml — deploy#155, merged)
    │
    ├── pre-deploy gate (this workflow): db-migrate.yml
    │       │
    │       ├── step 1: alembic heads  (belt-and-suspenders)
    │       │     ├── must print exactly 1 line with "(head)"
    │       │     └── that line must contain EXPECTED_MERGE_HEAD (currently 0040)
    │       │
    │       └── step 2: alembic upgrade head  (singular)
    │             └── runs inside ghcr.io/noorinalabs/noorinalabs-user-service:<tag>
    │                 container, joined to docker network noorinalabs_user-backend,
    │                 with DATABASE_URL pointing at user-postgres:5432.
    │
    └── deploy step (docker compose up)  —  ONLY runs if the gate succeeded.
```

Stg-first is enforced by the caller (`promote.yml`, landed via deploy#155) — `matrix: [stg, prod]` with `max-parallel: 1`, same pattern as `terraform.yml`. A stg gate failure halts the workflow before the prod job can be manually approved.

## Environment protection

- `env: stg`  → GH Environment `staging`    → staging secrets, no manual approval.
- `env: prod` → GH Environment `production` → production secrets, **manual approval required**.

Per-env secrets (`USER_POSTGRES_USER`, `USER_POSTGRES_DB`, `DEPLOY_SSH_PRIVATE_KEY`, etc.) resolve from whichever Environment the job maps to. No cross-env leakage — if the prod-env job tries to use stg secrets, the Environment mapping simply returns empty strings and the SSH step fails loudly.

## Failure modes and recovery

### 1. Heads-count assertion fails — count != 1

```
ERROR: [stg] alembic heads reported 2 heads, expected exactly 1.
```

**Cause:** a PR in `noorinalabs-user-service` added a new migration without a proper `down_revision`, creating a second head. `alembic upgrade head` would then either fail or silently pick a branch.

**Recovery:**

1. Do NOT retry the gate. Retrying will not fix multiple heads.
2. In `noorinalabs-user-service`, run locally:
   ```bash
   cd noorinalabs-user-service
   alembic heads
   alembic branches  # shows the DAG split point
   ```
3. Either (a) add a merge migration — same pattern as user-service#80 — or (b) fix the offending migration's `down_revision` to linearize the DAG.
4. Open a PR. Once merged to the wave branch, rebuild the user-service image and retry the promotion.

**Do not bypass the gate.** The gate is protecting prod.

### 2. Heads-count assertion fails — head != EXPECTED_MERGE_HEAD

```
ERROR: [stg] alembic heads reports a single head, but it is NOT the
       expected merge-migration revision '0040'.
```

**Cause:** a new migration landed in user-service (probably another merge migration down the line) and `EXPECTED_MERGE_HEAD` in `db-migrate.yml` was not bumped in the same PR.

**Recovery:**

1. Confirm the migration chain in user-service:
   ```bash
   docker run --rm ghcr.io/noorinalabs/noorinalabs-user-service:<tag> \
     /app/.venv/bin/alembic heads
   ```
2. If the new head is legitimate, open a same-wave PR in `noorinalabs-deploy` that updates `EXPECTED_MERGE_HEAD` in `.github/workflows/db-migrate.yml` to the new revision id. Merge that PR, then retry the promotion.
3. If the new head is NOT legitimate (someone committed it by mistake), revert in user-service and restart the promotion.

### 3. `alembic upgrade head` fails

```
ERROR: [stg] alembic upgrade head
alembic.util.exc.CommandError: ...
```

Most common causes:

| Symptom | Likely cause | Recovery |
|---|---|---|
| `duplicate column` / `relation already exists` | migration not idempotent, db partially migrated by a previous run that crashed between statements | Inspect `alembic_version` table; manually stamp to the revision already applied; re-run gate |
| `relation "..." does not exist` when dropping/altering | earlier migration never ran (skipped) | `alembic current` to see where you are, then `alembic upgrade +1` until head |
| Connection refused / timeout | `user-postgres` container not running, or network `noorinalabs_user-backend` missing | `docker compose -f compose/docker-compose.prod.yml ps user-postgres`; `docker network ls \| grep user-backend` |
| Permission denied | `USER_POSTGRES_USER` is not owner of the objects being altered | Check that the compose env-file matches the db (user mismatch from a secret rotation) |

**Manual rollback:** alembic itself can downgrade — but only if the failed migration wrote a proper `downgrade()`:

```bash
# On the VPS, in the same shape the gate uses:
docker run --rm \
  --network noorinalabs_user-backend \
  -e DATABASE_URL="postgresql+psycopg://${USER_POSTGRES_USER}:${USER_POSTGRES_PASSWORD}@user-postgres:5432/${USER_POSTGRES_DB}" \
  ghcr.io/noorinalabs/noorinalabs-user-service:<previous-tag> \
  /app/.venv/bin/alembic downgrade -1
```

If `downgrade()` is empty or unsafe, rollback = restore from pre-promotion backup (see `user-service-migration.md` §9 Rollback). Backup should have been taken by the caller workflow before invoking the gate — `deploy#84` must snapshot `user-postgres` before each promotion, not this workflow's responsibility.

### 4. SSH step fails — `.env missing on <env> VPS`

```
ERROR: /opt/noorinalabs-deploy/.env is missing on stg VPS.
```

**Cause:** the VPS is fresh, or someone ran `rm .env` during a debug session. The deploy workflow writes `.env` each time it runs; the gate requires the most recent env-file to assemble `DATABASE_URL`.

**Recovery:**

1. Run the main deploy workflow (`deploy-isnad-graph.yml`) against the env once — it writes `.env`.
2. Retry the promotion / gate.
3. If this happens on prod, treat it as a sev-2 incident: something deleted the env-file mid-deploy. Escalate to Bereket.

### 5. GHCR pull fails

```
Error response from daemon: manifest for ghcr.io/...:stg-latest not found
```

**Cause:** no image has been tagged for this env yet. Usual on first W10 promotion before the Contract is fully wired through.

**Recovery:**

1. Confirm `isnad-graph#815` and `user-service#64` Contract consumer PRs have merged.
2. Confirm a CI run has published at least one image with the expected tag (`stg-<short>` or `<env>-latest`).
3. If needed, dispatch `db-migrate.yml` manually with an explicit known-good `image_tag` input to unstick.

## Hotfix cadence

If a migration needs to land urgently (e.g., a prod outage fix):

1. Land the alembic migration in `noorinalabs-user-service` → merge to the wave branch.
2. Wait for the image publish workflow to produce a new `<env>-latest` tag.
3. Promote via the normal promotion workflow (`promote.yml`, deploy#155). Do NOT bypass the gate — even for hotfixes.
4. If the gate flags an unexpected head and the migration is genuinely additive and safe, the correct action is still to update `EXPECTED_MERGE_HEAD` in `db-migrate.yml` — not to skip the assertion. This is a 1-line PR and reviewable in minutes.

## Admin bootstrap

The same `db-migrate.yml` SSH step runs a **post-migrate admin-grant reassertion**
immediately after `alembic upgrade head` succeeds (deploy#426). It invokes
user-service's `scripts/bootstrap_admin.py` (us#159 / PR
[noorinalabs-user-service#160](https://github.com/noorinalabs/noorinalabs-user-service/pull/160))
inside the same one-shot user-service image, against the just-migrated
`user-postgres`, to grant the `admin` role to the bootstrap account.

```
alembic upgrade head  →  bootstrap_admin.py --database-url <user-postgres>
```

**Why it exists:** the `admin` ROLE is seeded by migration `0001`, but no user is
granted it. Every user-service admin endpoint — and the isnad-graph admin panels
behind it — needs an admin JWT, so without a seeded grant they 401/403. Wiring it
into post-migrate makes the grant self-healing on every deploy instead of a
forgettable manual step.

**First-login dependency (important).** Account creation in user-service is
**OAuth-only** — the bootstrap account row does not exist until the owner logs in
once via Google OAuth as `BOOTSTRAP_ADMIN_EMAIL` (default
`parametrization@gmail.com`). So:

- On a **brand-new env**, this step correctly **no-ops** (the script logs
  `user_not_found` and exits 0). It does **not** fail the deploy.
- After the owner's **first OAuth login**, the grant lands on the **next** deploy.
- Every subsequent deploy is idempotent — the script reports `already_admin` and
  exits 0 (no duplicate grant).

**It never fails the migration gate.** The step is deliberately best-effort: it is
wrapped in `set +e` with an explicit rc check, so the `migrated` output the
promotion workflow gates on reflects the **alembic** result only — never the admin
reassertion. The only non-zero exit the script produces is `BootstrapError`
(rc=1, the `admin` role missing => an unmigrated DB), which is essentially
impossible here because `alembic upgrade head` just succeeded. If it ever fires it
is **logged loudly** (a `WARNING:` line in the SSH step output) but does not abort
the deploy.

**Config sourcing.** `BOOTSTRAP_ADMIN_EMAIL` and the DB connection are sourced from
the deploy env's existing user-service config, not hardcoded in the workflow:

- DB — the asyncpg `DATABASE_URL` assembled from the VPS `.env` `USER_POSTGRES_*`
  values (the same URL the alembic step uses), passed via `--database-url`.
- Email — read from the VPS `.env` (`set -a; . ./.env` export). Documented as an
  optional override in `compose/.env.example`; unset → the script's default. Like
  `USER_POSTGRES_PASSWORD`, it is sourced from the VPS env-file, not pushed through
  the GH Actions transport.

**Manual run.** To grant out-of-band (e.g. right after the owner's first login,
without waiting for the next deploy), run the user-service target directly on the
VPS image, or from a user-service checkout:

```bash
make bootstrap-admin            # uv run python scripts/bootstrap_admin.py
# or, one-shot against the deployed DB from the user-service image:
docker run --rm --network noorinalabs_user-backend \
  ghcr.io/noorinalabs/noorinalabs-user-service:<env>-latest \
  /app/.venv/bin/python scripts/bootstrap_admin.py \
    --database-url "postgresql+asyncpg://<user>:<pass>@user-postgres:5432/<db>"
```

## Escalation

| Failure | Primary | Secondary |
|---|---|---|
| heads-count assertion fails | Lucas.Ferreira (SRE) | Anya.Kowalczyk (user-service, DAG owner) |
| `alembic upgrade head` fails on stg | Lucas.Ferreira | Aisha.Idrissi (SRE) |
| `alembic upgrade head` fails on prod | Bereket.Tadesse (IM) | Nadia.Boukhari (user-service manager) |
| SSH / VPS state problem | Lucas.Ferreira | Weronika.Zielinska (Platform Architect) |
| Secret mismatch / env-file drift | Nino.Kavtaradze (Security) | Bereket.Tadesse |
| admin bootstrap `WARNING` (rc=1) | Lucas.Ferreira | Anya.Kowalczyk (user-service) |

## Observability

**Status: implemented — [`deploy#161`](https://github.com/noorinalabs/noorinalabs-deploy/issues/161) (P3W1) wired the textfile-collector plumbing and landed the Prometheus alert.**

Gate runs emit two gauges to node-exporter's textfile collector via an `if: always()` SSH step in `db-migrate.yml`. The metric file lives at `/var/lib/node_exporter/textfile_collector/user_service_alembic_gate.prom` on the VPS (mode 0644, written by the `deploy` user via temp + atomic rename). The host directory is provisioned automatically by Terraform cloud-init on every VPS — see `terraform/hetzner/modules/hetzner-vps/cloud-init.yaml.tpl` `runcmd:` (creates `/var/lib/node_exporter/textfile_collector`, owned `deploy:deploy`, mode `0755`). No manual runbook step is needed on fresh VPSes. The `node-exporter` service in `compose/docker-compose.prod.yml` mounts that directory read-only and reads the file on each scrape.

Metric schema:

```
user_service_alembic_gate_last_run_success{env="..."} <0|1>
user_service_alembic_gate_last_run_timestamp_seconds{env="..."} <unix-ts>
```

Alerts (`infra/prometheus/alerts.yml` group `db_migrate`) — two distinct rules so on-call can discriminate failure modes at first sight:

| Alert | Expression | Fires when | Operator action |
|-------|-----------|-----------|------------------|
| `UserServiceAlembicGateFailure` | `_success == 0` | The most recent gate run returned `_success=0` (and has not been replaced by a successful run since) | Walk § Failure Modes above to discriminate heads-count vs `alembic upgrade head` failure; fix upstream and re-promote. |
| `UserServiceAlembicGateStale` | `(time() - _timestamp) > 86400` | The most recent gate timestamp is older than 24h | Check `Deploy to staging` / `Promote to production` workflow runs first. If they ran but the metric didn't move, the textfile emission step or node-exporter scrape is broken. If they didn't run, the upstream pipeline (notify-deploy / repository_dispatch / ghcr-publish) is broken. |

Both severities are `critical` and `for: 0m` — the gate is one-shot and every signal is operator-actionable; flap suppression isn't needed at this layer. Severity matches the § Escalation table above.

Gate failures surface via:

1. **Prometheus alert** (above) — routed through Alertmanager per `infra/alertmanager/alertmanager.yml` warning-severity rules.
2. **GitHub Actions UI** — the `Report migration result` step in `db-migrate.yml` writes a structured summary to `$GITHUB_STEP_SUMMARY` for every run (success or failure) including env, image tag, expected head, and the `migrated` boolean. The runbook link is included on failure.
3. **Caller workflow signal** — the reusable workflow's `migrated` output is `false` (and the job result is `failure`) on any gate failure, which the promotion workflow (#155 → `promote.yml`) gates on. A failed stg gate hard-stops before prod manual-approval is even offered.
4. **On-call escalation** — the table above (§ Escalation) lists primary/secondary owners per failure class.

### Operator: recovering the textfile_collector directory

The host directory is provisioned automatically by Terraform cloud-init on every VPS — see `terraform/hetzner/modules/hetzner-vps/cloud-init.yaml.tpl` (the `runcmd:` block creates `/var/lib/node_exporter/textfile_collector` at first boot, owned `deploy:deploy` mode `0755`). **This recipe is for recovery only**, not bootstrap. Reach for it if:

- The directory is missing or has wrong perms on a long-lived VPS (someone manually deleted it, partial restore from backup, etc.).
- The cloud-init template was changed in a way that didn't run on existing VPSes (cloud-init only fires once on first boot).
- An ad-hoc rebuild of the alert plumbing without re-running terraform.

The most common observable symptom is the `Emit textfile-collector metrics on VPS` step in `db-migrate.yml` failing with `ERROR: /var/lib/node_exporter/textfile_collector is not writable by deploy user.` — this means cloud-init didn't fire (or its provisioning was overwritten). On the affected VPS, check `cloud-init status --long` first to confirm the diagnosis, then apply the recovery recipe:

```bash
sudo mkdir -p /var/lib/node_exporter/textfile_collector
sudo chown deploy:deploy /var/lib/node_exporter/textfile_collector
sudo chmod 0755 /var/lib/node_exporter/textfile_collector
```

Note: as of #210 the `Emit` step **fails loud** rather than silently auto-creating a wrong-perm directory. If the directory is missing OR exists but is not writable by `deploy`, the SSH step exits non-zero and the gate run is marked failed. This catches Docker's default-create-on-bind behavior (root:root 0755) on a fresh VPS where cloud-init didn't run — without fail-loud, the metric would silently never land and the alert would be silently dark.

## Related issues

- **deploy#85** — original gate PR (this runbook authored alongside).
- **deploy#155** — promotion workflow that calls this gate (merged 2026-04-23, produced `promote.yml` + `deploy-stg.yml` + `deploy-prod.yml`). Issue #84.
- **deploy#160** — wired `promote.yml` to `uses: ./.github/workflows/db-migrate.yml` (P3W1, merged).
- **deploy#161** — this section's textfile-collector plumbing + `UserServiceAlembicGate{Failure,Stale}` alerts (P3W1).
- **user-service#80** — alembic merge migration producing revision `0040`. Upstream unblock.
- **user-service#63** — original alembic merge migration issue (closed by #80).
- **deploy#141** — fresh-volume alembic-in-compose-up init container. Out of scope here.
