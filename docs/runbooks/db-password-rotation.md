# Runbook — automated database / cache password rotation

Source: [deploy#387](https://github.com/noorinalabs/noorinalabs-deploy/issues/387)
(automated DB-password rotation, deferred from
[deploy#11](https://github.com/noorinalabs/noorinalabs-deploy/issues/11)).
Decision: [ADR 0007](../adr/0007-central-secrets-manager.md) (central
secrets-manager + rotation policy, Accepted 2026-06-12).
Policy: [`secret-rotation-policy.md`](secret-rotation-policy.md)
(the cadence/scope this runbook mechanizes).

## What this rotates

The four app-runtime DB/cache password secrets, **per-secret** (ADR 0007 scope
S1), for the env whose stack the target VPS runs:

| Secret | Engine | How it is applied |
|---|---|---|
| `POSTGRES_PASSWORD` | isnad-graph Postgres | live `ALTER ROLE` + recreate `api`, `postgres-exporter` |
| `USER_POSTGRES_PASSWORD` | user-service Postgres | live `ALTER ROLE` + recreate `user-service`, `user-postgres-exporter` |
| `REDIS_PASSWORD` | isnad-graph Redis | recreate `redis` (re-applies `requirepass`) + `api` |
| `USER_REDIS_PASSWORD` | user-service Redis | recreate `user-redis` + `user-service` |

> **Why Postgres and Redis differ.** Postgres stores the role password in its
> data volume — `POSTGRES_PASSWORD` only seeds an *empty* volume, so a recreate
> does NOT change it; rotation runs a live `ALTER ROLE`. Redis takes
> `requirepass` from its `command:` arg, so recreating the container with the
> new `.env` value re-applies it by construction. See the header of
> [`scripts/rotate_db_password.sh`](../../scripts/rotate_db_password.sh) for the
> full mechanics.

## Architecture

```
schedule (quarterly, stg only)  ─┐
workflow_dispatch (stg or prod) ─┴─> rotate-db-passwords.yml (runner)
        1. preflight SECRETS_ADMIN_TOKEN can write the Environment secret
        2. mint URL-safe value (token_urlsafe), mask it
        3. SSH ─> scripts/rotate_db_password.sh (on the VPS)
                    ALTER ROLE / recreate  ->  health-gate  ->  rollback-on-fail
        4. on box-success ONLY: gh secret set  (GH Environment secret = box)
```

The **ordering is the safety property**: the GH Environment secret is updated
*only after* the box rotation exits 0. A failed health gate rolls the box back
to the old credential and the workflow leaves the GH secret unchanged — so
"**GH Environment secret == live box**" always holds, and the next deploy never
rewrites `.env` to a value the running DB does not accept.

## Cadence

- **Scheduled:** quarterly (1st of Jan/Apr/Jul/Oct, 04:00 UTC) — **staging only**.
- **On-demand / production:** `workflow_dispatch` (below). Also rotate
  immediately on any [`secret-rotation-policy.md` § On-demand trigger](secret-rotation-policy.md#on-demand-triggers-apply-to-every-class)
  (offboarding, value-in-logs, provider compromise, drift alarm).

## Prerequisite — `SECRETS_ADMIN_TOKEN`

The workflow writes Environment secrets, which the built-in `GITHUB_TOKEN`
**cannot** do. Provision a token with `secrets: write` on this repo and store it
as `SECRETS_ADMIN_TOKEN` in **both** the `staging` and `production` GitHub
Environments:

- A **fine-grained PAT** scoped to `noorinalabs/noorinalabs-deploy` with
  **Secrets: Read and write** (covers Environment secrets), or
- a GitHub App installation token with the same permission.

The job **preflights** this token (`gh secret list --env <env>`) before touching
the box, so a missing or under-scoped token fails fast with **no** half-applied
state. Rotate `SECRETS_ADMIN_TOKEN` itself on its provider cadence (it is a
`provider`-class secret; record in #11).

## How to run an on-demand / production rotation

1. Actions → **Rotate DB passwords** → **Run workflow**.
2. Inputs:
   - **environment:** `staging` or `production`.
   - **secret:** `all` (the four) or one specific secret.
   - **dry_run:** `true` to mint + log only (verifies wiring without touching
     anything) — recommended for a first run in a new Environment.
3. **Production gate:** with `environment=production` the job waits on the
   `production` Environment **approval rule** — an authorized owner must approve
   the run (the same gate `deploy-prod.yml` / `promote.yml` use). The workflow
   **never** auto-rotates production on the cron.
4. Watch the run: the box step is health-gated; on failure it rolls back and the
   GH secret is left untouched. On success the GH Environment secret is updated
   and the run summary records the rotation (the audit trail per ADR 0007).

## Verification (runtime — post-merge / per-run)

These can only be confirmed against a live stack, not at PR time:

- [ ] **Staging dry run** (`dry_run=true`) succeeds — proves token preflight,
      mint, and matrix wiring without side effects.
- [ ] **Staging real run** (`all`) succeeds: each secret's recreated services
      reach `healthy`; a pre-rotation app session re-connects with the new
      credential; the four `staging` Environment secrets show new values.
- [ ] **Forced-rollback drill (staging):** temporarily point the rotation at a
      stack where the app cannot come healthy (or fault-inject) and confirm the
      script restores `.env`, `ALTER ROLE`s Postgres back, recreates against the
      old credential, and the GH secret is **unchanged**.
- [ ] **Production run** (owner-approved): same checks against prod URLs via the
      [`verify_prod_smoke.sh`](../../scripts/verify_prod_smoke.sh) battery.
- [ ] Rotation timestamps recorded in
      [deploy#11](https://github.com/noorinalabs/noorinalabs-deploy/issues/11).

## Recovery — box rotated but GH secret not persisted

The one residual gap (a transient `gh` API failure *after* the box rotated but
*before* the secret was written) leaves the live box on the **new** value while
the GH secret still holds the **old** one. The persist step fails loudly with
this instruction. Pick one:

- **Re-persist (preferred):** set the GH Environment secret to the new value the
  run minted (it is masked in logs; if unrecoverable, do a fresh rotation
  instead), then verify with a no-op deploy.
- **Roll the box back:** SSH to the VPS and run the rotation script's rollback
  path against the OLD value (re-run a rotation targeting the old credential is
  not supported; instead `gh secret set` the OLD value and run
  `deploy-<env>.yml`, which rewrites `.env` from the GH secret and recreates the
  stack — for Postgres also `ALTER ROLE … WITH PASSWORD '<old>'` on the box
  first, since the volume holds the new password).

**Do not deploy** the affected env until the box and the GH secret are
reconciled — a deploy would rewrite `.env` from the GH secret and break the
service whose live DB no longer accepts it.

## Out of scope

- The central-management posture and tool choice — settled in
  [ADR 0007](../adr/0007-central-secrets-manager.md) (Option A + SOPS-for-config).
- Non-DB secret classes (JWT, GHCR, pipeline, state-bucket, SSH) — see their own
  rows in [`secret-rotation-policy.md`](secret-rotation-policy.md) and runbooks.
- A consolidated audit pane — ADR 0007 accepts the distributed audit trail (GH
  Actions run + org secret-change audit log) at current scale.
