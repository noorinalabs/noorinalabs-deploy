# RUNBOOK — noorinalabs-deploy

Operational entrypoint for the deployment-orchestration repo. This document
points at the right per-procedure runbook for every common operational task,
and inlines the short procedures that don't warrant their own file.

**Audience:** SRE on-call, release coordinators, infra contributors.
**Scope:** what runs on the VPSes (stg, prod) and the workflows that put it there.
**Out of scope:** application-level debugging — see the relevant service repo.

For background on why this repo exists, read `CLAUDE.md` first ("Service repos
own what they build. This repo owns what runs on the server.").

## Quick links

| Task | Go to |
|---|---|
| Deploy isnad-graph (per-service) | [`docs/runbooks/deploy-isnad-graph.md`](docs/runbooks/deploy-isnad-graph.md) |
| Deploy landing-page (per-service) | [`docs/runbooks/deploy-landing-page.md`](docs/runbooks/deploy-landing-page.md) |
| User-service Alembic migration | [`docs/runbooks/user-service-alembic.md`](docs/runbooks/user-service-alembic.md) |
| Per-env OAuth provisioning | [`docs/runbooks/oauth-per-env.md`](docs/runbooks/oauth-per-env.md) |
| Blackbox probes / synthetic checks | [`docs/runbooks/blackbox-probes.md`](docs/runbooks/blackbox-probes.md) |
| Break-glass `workflow_dispatch` inputs | [`docs/runbooks/break-glass.md`](docs/runbooks/break-glass.md) |
| Cold-rebuild dry-run gate | [`docs/runbooks/cold-rebuild-dry-run.md`](docs/runbooks/cold-rebuild-dry-run.md) |
| First-time VPS bring-up | [`docs/runbooks/user-service-migration.md`](docs/runbooks/user-service-migration.md) + § Build below |
| Architecture / topology | [`docs/architecture.md`](docs/architecture.md) |
| Dependencies / required secrets | [`docs/dependencies.md`](docs/dependencies.md) |
| Triage common failure modes | [`docs/troubleshooting.md`](docs/troubleshooting.md) + § Common failure modes below |

## Section coverage

This runbook covers the five sections required by deploy#24:

- [x] [Build](#build) — how to provision and update infrastructure
- [x] [Deploy](#deploy) — per-environment procedures (dev / stg / prod)
- [x] [Rollback](#rollback) — recovery from a bad deploy
- [x] [Common failure modes](#common-failure-modes) — Terraform state lock, B2 cred rotation, VPS connectivity
- [x] [On-call escalation](#on-call-escalation) — who to page and when

---

## Build

"Build" in this repo means **provisioning and updating the infrastructure
that the workloads run on** — VPSes, DNS, TLS, observability stack, and the
Compose stack itself. Application images are built in the service repos
(isnad-graph, user-service, landing-page) and published to GHCR.

### Provision a new VPS (Terraform)

The Hetzner VPS layout is per-environment with a shared module — see
[`docs/adr/0001-tf-hetzner-per-env-state-strategy.md`](docs/adr/0001-tf-hetzner-per-env-state-strategy.md).

```bash
# Working directory IS the env selector — no Terraform workspaces.
cd terraform/hetzner/envs/stg          # or envs/prod
terraform init                          # uses S3-protocol B2 backend
terraform plan -out tfplan
terraform apply tfplan
```

State is stored in Backblaze B2 bucket `noorinalabs-terraform-state`,
with per-env state keys (`hetzner/stg.tfstate`, `hetzner/prod.tfstate`).
Apply via CI is the preferred path — see § Apply via CI below.

After `apply`, the new VPS still needs first-time setup:

```bash
ssh -i ~/.ssh/isnad_deploy root@<new-vps-host>
curl -sL https://raw.githubusercontent.com/noorinalabs/noorinalabs-deploy/main/scripts/bootstrap-vps.sh | bash
```

`scripts/bootstrap-vps.sh` is idempotent: installs Docker, creates the
`deploy` user, clones this repo to `/opt/noorinalabs-deploy`, configures
log rotation, and stages the systemd backup units. Re-run it any time the
bootstrap needs to be re-applied.

### Apply Terraform via CI

Pushes to `main` that touch `terraform/**` run the `terraform.yml` workflow:

- `fmt` — `terraform fmt -check -recursive`
- `validate` — matrix over `[modules/hetzner-vps, envs/stg, envs/prod, cloudflare, backblaze]`
- `plan` — matrix over `[stg, prod]` on PRs (comments plan on the PR)
- `apply` — matrix `[stg, prod]` with `max-parallel=1`, push-to-`main` only

`apply` runs stg before prod. The `stg` and `prod` matrix entries map to
GitHub Environments `staging` and `production`, which gate manual approvals
and scope the B2 / Hetzner credentials.

### Update the Compose stack

Application service images are pulled fresh on every deploy — to push a
new image, merge to the service repo's `main` and let `notify-deploy.yml`
fire `repository_dispatch` (see [Deploy](#deploy)).

To change the Compose stack itself (add a service, bump an infra image,
change a network or volume), edit `compose/docker-compose.prod.yml`, open
a PR, and let the `compose-validate` and `cold-rebuild-dryrun` workflows
gate it. Once merged, the next service deploy picks up the new compose
file (workflows always `git fetch && git reset --hard origin/main` in
`/opt/noorinalabs-deploy` before `docker compose up`).

### Pre-commit local-build smoke

```bash
pre-commit install                      # one-time per clone
pre-commit run --all-files              # runs terraform-fmt, terraform-validate,
                                        # gitleaks, env-example-check
```

CI will reject anything that fails these gates, so run them locally before
pushing.

---

## Deploy

### Environment summary

| Env | VPS | Domains | Trigger | Approval |
|---|---|---|---|---|
| **dev** | local laptop, MinIO + Compose | `localhost` | manual `docker compose up` | none |
| **stg** | `noorinalabs-stg` (CPX21) | `*.stg.noorinalabs.com` | auto on service-repo `main` merge | none (auto-deploy) |
| **prod** | `noorinalabs-prod` (CPX41) | `noorinalabs.com`, `*.noorinalabs.com` | promote from stg | manual approval in `production` GH Environment |

There is no "dev VPS" — local development uses a laptop-side Compose stack
backed by MinIO. The `compose/docker-compose.minio.yml` and
`compose/minio.env.example` files cover the local dev path; see
[`compose/.env.example`](compose/.env.example) for required env vars.

### Stg deploy (auto)

When code merges to `main` in any of `noorinalabs-isnad-graph`,
`noorinalabs-user-service`, or `noorinalabs-landing-page`:

1. The service repo's `notify-deploy.yml` fires a `repository_dispatch`
   with `event_type=deploy-noorinalabs-{service}`.
2. This repo's `deploy-stg.yml` receives the event, SSHes to the stg VPS,
   pulls the new image, and runs `docker compose up -d --force-recreate`
   for that service.
3. `verify-deploy.yml` runs the `verify-stg` job — full cross-repo
   integration suite via `integration-tests/run-tests.sh`.

A failed `verify-stg` run blocks any subsequent `promote.yml` invocation
within the configured freshness window (default 24h).

### Prod deploy (promote from stg)

Prod never deploys directly from a service repo — it always promotes a
stg-validated digest. The promotion pathway:

1. Open `Actions > Promote to Prod` (`promote.yml`) — workflow_dispatch.
2. Inputs: `service` (api / frontend / landing / user-service / all),
   `image_tag` (defaults to `stg-latest`), and the four break-glass inputs
   (leave empty unless following [`docs/runbooks/break-glass.md`](docs/runbooks/break-glass.md)).
3. The workflow:
   - Validates stg-verify freshness (≤24h since last `verify-deploy.yml`
     stg success), unless `skip_stg_verify` is set.
   - For user-service: runs `db-migrate.yml` against prod user-postgres
     (`alembic upgrade head`) unless `skip_alembic_gate` is set.
   - Resolves source tag to `sha-<short>` (never `*-latest` — see #234).
   - Retags `stg-<short>` → `prod-<short>` + `prod-latest` in GHCR.
   - Triggers `deploy-prod.yml` against the new `prod-<short>` digest.
4. Manual approval gate fires in the `production` GH Environment.
5. Approve → `deploy-prod.yml` SSHes to prod VPS, pulls the `prod-<short>`
   image, and rolls the service.
6. `verify-deploy.yml` runs the `verify-prod` job —
   `scripts/verify_prod_smoke.sh` (<60s smoke battery: health 200s,
   narrator query, JWKS, auth wiring).

### Manual / out-of-band deploys

For one-off deploys (e.g., re-running a failed `deploy-stg.yml` against
the same image, or pushing a specific `sha-<short>` tag):

1. `Actions > Deploy noorinalabs-isnad-graph` (or landing / all).
2. **Run workflow** with explicit `image_tag`.
3. Watch the job log — health check polls API for up to 120s after compose-up.

### Verify after deploy

Automatic verification is best-effort but real. To run manually:

```bash
# Stg integration suite (slow — full e2e)
gh workflow run verify-deploy.yml -f target=stg

# Prod smoke (~60s)
gh workflow run verify-deploy.yml -f target=prod

# Legacy operator script — use only if Actions is unreachable
SITE_URL=https://isnad-graph.noorinalabs.com ./scripts/verify_deployment.sh --skip-workflow
```

---

## Rollback

The shape of a rollback depends on what broke and how deep the damage went.
Pick the closest matching path.

### Path 1 — bad image, infra healthy (most common)

Use the rollback workflow:

1. `Actions > Rollback` (`rollback.yml`) — workflow_dispatch.
2. Inputs:
   - `image_tag` — the tag to roll back to (e.g., `prod-a1b2c3d`). Find
     prior tags via:
     ```bash
     gh api orgs/noorinalabs/packages/container/noorinalabs-isnad-graph/versions \
       --jq '.[].metadata.container.tags[]' | head -20
     ```
   - `service` — `all`, `api`, `frontend`, `landing`, or `user-service`.
3. **Run workflow**. The workflow SSHes to prod, updates `IMAGE_TAG` in
   `.env`, pulls the older image, and re-rolls the named service.

### Path 2 — Actions unavailable (manual SSH rollback)

If GitHub Actions is unreachable (rare — usually an org-level outage):

```bash
ssh deploy@<VPS_HOST>
cd /opt/noorinalabs-deploy

# Update the image tag in .env
sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG="prod-a1b2c3d"/' .env

# Roll back the named services
docker compose -p noorinalabs -f compose/docker-compose.prod.yml \
  --env-file .env pull api frontend
docker compose -p noorinalabs -f compose/docker-compose.prod.yml \
  --env-file .env up -d --force-recreate api frontend

# Verify
sleep 15
docker compose -p noorinalabs -f compose/docker-compose.prod.yml ps \
  --format '{{.Name}}\t{{.Status}}'
```

### Path 3 — bad migration (user-service)

Alembic migrations are forward-only by convention. If a migration ships
broken code that runs but corrupts state, the rollback procedure is:

1. Roll back the user-service image (Path 1) to the prior `prod-<short>`.
2. Author a forward-only "fix-up" migration in user-service that repairs
   the corrupted rows, gated through `db-migrate.yml`.
3. **Do not** `alembic downgrade` against prod — see
   [`docs/runbooks/user-service-alembic.md`](docs/runbooks/user-service-alembic.md)
   for the full recovery decision tree.

### Path 4 — Terraform-level rollback

If a Terraform apply broke infrastructure (rare — `terraform.yml`
gates this), the rollback is **not** `terraform destroy`. Instead:

1. Revert the offending PR via `gh pr revert` and merge the revert.
2. Let `terraform.yml` re-apply with the prior config.
3. If state is corrupted (apply hung mid-resource), see
   § Terraform state lock under [Common failure modes](#common-failure-modes).

### Path 5 — total prod loss (DR)

Bring up a fresh prod VPS via Terraform, restore from B2 backups, then
re-deploy the latest `prod-latest` digests. This is the "cold rebuild"
path — the [`cold-rebuild-dry-run`](docs/runbooks/cold-rebuild-dry-run.md)
workflow gates the workflow code paths, but the actual operation is rare
enough that running through it requires SRE pairing. Page the
[on-call escalation chain](#on-call-escalation) before starting.

---

## Common failure modes

The full triage matrix lives in [`docs/troubleshooting.md`](docs/troubleshooting.md).
The three failure modes called out by deploy#24 are below.

### Terraform state lock

**Symptom:** `terraform plan` or `apply` hangs with `Error acquiring the
state lock` or `ConditionalCheckFailedException`. The state backend is
S3-protocol Backblaze B2; B2 doesn't natively support DynamoDB-style
locking, so we rely on conditional writes via the S3 backend's lock-id
files.

**Triage:**

1. Check whether another `terraform.yml` run is in flight:
   ```bash
   gh run list --workflow=terraform.yml --limit 5 --json status,conclusion,headBranch
   ```
2. If a run is genuinely in flight, wait for it to finish — do not break
   the lock.
3. If the lock is stale (no in-flight run, lock older than ~10min):
   ```bash
   cd terraform/hetzner/envs/stg          # or the affected env
   terraform force-unlock <lock-id>
   ```
   The lock-id is printed in the error. Confirm the prompt with `yes`.
4. **NEVER** force-unlock if you can see another developer's run in
   flight — you'll race the apply and corrupt state.

If state itself is corrupted (mid-resource crash, partial writes), the
recovery is `terraform state pull` + manual edit + `terraform state push`.
This is destructive — escalate first.

### B2 credential rotation

**Symptom:** Backups stop, Terraform state reads start failing, or
`scripts/backup.sh` returns non-zero. Two distinct credential paths exist:

- **Backups** — rclone-native B2 creds (`B2_KEY_ID`, `B2_APPLICATION_KEY`)
  on the VPS in `/etc/rclone/rclone.conf`.
- **Terraform state** — S3-protocol B2 creds (`AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`) in GitHub Actions secrets at
  the `staging` and `production` environment scope.

**Rotation procedure:**

1. Generate a new application key in the Backblaze console with the
   correct bucket scope (`noorinalabs-backups` for backups,
   `noorinalabs-terraform-state` for TF state).
2. For backups: SSH to the prod VPS, edit `/etc/rclone/rclone.conf` as
   root, restart the next scheduled `isnad-backup.timer` cycle (or run
   `systemctl start isnad-backup.service` to test immediately).
3. For TF state: update both `staging` and `production` env-scoped
   secrets via the repo settings UI or:
   ```bash
   gh secret set AWS_ACCESS_KEY_ID --env staging --body "$NEW_KEY_ID"
   gh secret set AWS_SECRET_ACCESS_KEY --env staging --body "$NEW_SECRET"
   # repeat for --env production
   ```
4. Verify by running a no-op `terraform.yml` PR (touch a comment-only
   line) and confirming both `plan` matrix entries succeed.
5. Revoke the old keys in the Backblaze console only after the new keys
   have been confirmed working in CI.

The two credential paths are independent — rotating one does not affect
the other. See [`docs/dependencies.md`](docs/dependencies.md) for the
full required-secrets table.

### VPS connectivity

**Symptom:** `deploy-stg.yml` / `deploy-prod.yml` SSH step fails with
`ssh: connect to host <VPS_HOST> port 22: Connection refused` or
`Permission denied (publickey)`.

**Triage:**

1. Check the VPS is up via Hetzner console (web UI, not SSH) — look for
   suspended-due-to-billing, OOM-rebooted, or network-blackholed states.
2. Confirm SSH from a known-good source (your laptop):
   ```bash
   ssh -v deploy@<VPS_HOST>
   ```
   Verbose output identifies whether the failure is at TCP, key-exchange,
   or auth.
3. If TCP fails: check the Hetzner Cloud Firewall rules in the relevant
   `terraform/hetzner/envs/<env>` config — port 22 must be open. The
   `terraform.yml` plan would have caught any drift from the canonical
   `[SSH/22, HTTP/80, HTTPS/443]` ruleset.
4. If auth fails: confirm `GITHUB_DEPLOY_SSH_KEY` (the env-scoped secret)
   matches the public key in `/home/deploy/.ssh/authorized_keys` on the
   VPS. If a Hetzner password reset was performed for emergency access,
   the deploy user's keys may have been clobbered — re-paste from
   `terraform/hetzner/modules/hetzner-vps/deploy.pub`.
5. If neither: check `appleboy/ssh-action`'s output for `port`, `host`,
   and `script_stop` — silent failures usually mean the script exited 0
   on a step that should have failed (defensive `|| true` somewhere).

For the cloud-init keypair gap on TF-provisioned boxes (stg was clobbered
by a clone-from-prod artifact in 2026-04-24), see the `cloud_init_ssh_key_gap`
note in [`ontology/repos/deploy.yaml`](ontology/repos/deploy.yaml). The
mitigation is to inject the owner pubkey into Hetzner cloud-init user-data
on first provision.

---

## On-call escalation

This repo's on-call rotation is owned by the SRE team in the deploy roster
(see `.claude/team/roster/`). Day-to-day operational issues route to the
SRE on-call; cross-cutting incidents that touch service code escalate to
the relevant service repo's manager.

### Tier 0 — auto / hooks

Several alerts are wired to `Alertmanager` and surfaced via the
break-glass-audit log issue:

- `BreakGlassUsed` — anyone invoking the four `workflow_dispatch`
  break-glass inputs.
- `BackupFailed` — `scripts/emit-backup-failure-marker.sh` writes a
  textfile-collector marker scraped by node-exporter.
- `DeployFailed` — `verify-deploy.yml` non-zero exit.

These page the SRE on-call directly via Alertmanager → the configured
notifier (see `infra/alertmanager/alertmanager.yml`).

### Tier 1 — SRE on-call

| Topic | Primary | Backup |
|---|---|---|
| Deploys, Compose stack, VPS | Lucas Ferreira | Aisha Idrissi |
| Observability (Prometheus, Grafana, Loki) | Nurul Hakim | Lucas Ferreira |
| Security (secrets, TLS, OAuth, RBAC) | Nino Kavtaradze | Lucas Ferreira |
| Platform / Terraform | Weronika Zielinska | Aisha Idrissi |

### Tier 2 — escalation

If Tier 1 cannot resolve within 30 minutes, or the issue spans repos:

- **Infrastructure manager:** Bereket Tadesse (deploy repo).
- **Program Director:** Nadia Khoury (org-level, `noorinalabs-main`).
- **Owner:** Steven French (project owner, only for outages with no
  clear sub-team owner or for break-glass auth).

### Tier 3 — emergency mode

For incidents that cross the threshold defined in
[`.claude/team/charter/emergency-mode.md`](.claude/team/charter/emergency-mode.md)
(e.g., total prod loss, data-corruption event, security breach), declare
emergency mode in-band before any action. The charter governs the
relaxed-process posture and the catchup-debt obligation that follows.

The break-glass `workflow_dispatch` inputs in `promote.yml` and
`deploy-prod.yml` are the in-band bypass mechanism; every invocation is
audited per [`docs/runbooks/break-glass.md`](docs/runbooks/break-glass.md).

---

## Document maintenance

This file is the operational entrypoint and intentionally summarizes —
deep procedure detail belongs in `docs/runbooks/<topic>.md`. When you add
a new procedure runbook, link it from [Quick links](#quick-links) above.

Updates to this file should be reviewed by the SRE on-call (Tier 1
primary for the affected topic) and the standards lead.
