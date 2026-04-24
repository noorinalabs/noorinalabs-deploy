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

## Escalation

| Failure | Primary | Secondary |
|---|---|---|
| heads-count assertion fails | Lucas.Ferreira (SRE) | Anya.Kowalczyk (user-service, DAG owner) |
| `alembic upgrade head` fails on stg | Lucas.Ferreira | Aisha.Idrissi (SRE) |
| `alembic upgrade head` fails on prod | Bereket.Tadesse (IM) | Nadia.Boukhari (user-service manager) |
| SSH / VPS state problem | Lucas.Ferreira | Weronika.Zielinska (Platform Architect) |
| Secret mismatch / env-file drift | Nino.Kavtaradze (Security) | Bereket.Tadesse |

## Observability

**Status: aspirational — deferred to W11 follow-up [`deploy#161`](https://github.com/noorinalabs/noorinalabs-deploy/issues/161).**

The intended end state is a Prometheus alert `UserServiceAlembicGateFailure` that fires when the gate fails — short `for:` interval because the gate is one-shot and a miss is always actionable. The infrastructure to support this (textfile-collector writeout in `db-migrate.yml`, `--collector.textfile.directory=` flag + volume mount on `node-exporter` in `compose/docker-compose.prod.yml`) is **not** in place today. That plumbing is tracked in `deploy#161`.

Until #161 lands, gate failures surface via:

1. **GitHub Actions UI** — the `Report migration result` step in `db-migrate.yml` writes a structured summary to `$GITHUB_STEP_SUMMARY` for every run (success or failure) including env, image tag, expected head, and the `migrated` boolean. The runbook link is included on failure.
2. **Caller workflow signal** — the reusable workflow's `migrated` output is `false` (and the job result is `failure`) on any gate failure, which the promotion workflow (#155 → `promote.yml`) gates on. A failed stg gate hard-stops before prod manual-approval is even offered.
3. **On-call escalation** — the table above (§ Escalation) lists primary/secondary owners per failure class. Until the Prometheus alert exists, the on-call engineer learns of a failure when the promotion workflow surfaces a failed run.

## Related issues

- **deploy#85** — this PR.
- **deploy#155** — promotion workflow that will call this gate (merged 2026-04-23, produced `promote.yml` + `deploy-stg.yml` + `deploy-prod.yml`). Issue #84.
- **deploy#160** — wires `promote.yml` to `uses: ./.github/workflows/db-migrate.yml`. Follow-up.
- **deploy#161** — textfile-collector plumbing for the deferred `UserServiceAlembicGateFailure` Prometheus alert. P2W11.
- **user-service#80** — alembic merge migration producing revision `0040`. Upstream unblock.
- **user-service#63** — original alembic merge migration issue (closed by #80).
- **deploy#141** — fresh-volume alembic-in-compose-up init container. Out of scope here.
