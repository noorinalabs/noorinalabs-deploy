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
| Add a *new* service to the stack | [`docs/runbooks/new-service-deploy-checklist.md`](docs/runbooks/new-service-deploy-checklist.md) |
| User-service Alembic migration | [`docs/runbooks/user-service-alembic.md`](docs/runbooks/user-service-alembic.md) |
| Per-env OAuth provisioning | [`docs/runbooks/oauth-per-env.md`](docs/runbooks/oauth-per-env.md) |
| Blackbox probes / synthetic checks | [`docs/runbooks/blackbox-probes.md`](docs/runbooks/blackbox-probes.md) |
| Observability stack index + Cloudflare Web Analytics | [`docs/runbooks/observability.md`](docs/runbooks/observability.md) |
| Break-glass `workflow_dispatch` inputs | [`docs/runbooks/break-glass.md`](docs/runbooks/break-glass.md) |
| Cold-rebuild dry-run gate | [`docs/runbooks/cold-rebuild-dry-run.md`](docs/runbooks/cold-rebuild-dry-run.md) |
| Phase C cutover (hand-made → TF-managed prod) | [`docs/runbooks/phase-c-cutover.md`](docs/runbooks/phase-c-cutover.md) |
| Decommission the old hand-made prod VPS (post-cutover) | [`docs/runbooks/decommission-old-prod-vps.md`](docs/runbooks/decommission-old-prod-vps.md) |
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

After `apply`, the new VPS is bootstrapped end-to-end by
[`terraform/hetzner/modules/hetzner-vps/cloud-init.yaml.tpl`](terraform/hetzner/modules/hetzner-vps/cloud-init.yaml.tpl)
on first boot — Docker, docker-compose, fail2ban, ufw, unattended-upgrades,
rclone, the `deploy` user, SSH keys (root + deploy), GHCR auth (conditional
on `ghcr_auth_b64`), `.env.user-service`, root SSH password disable, and
the repo clone to `/opt/noorinalabs-deploy` are all provisioned without
operator action. SSH in as `root` (or `deploy`) using the key authorized
at provisioning time — see the `cloud_init_ssh_key_gap` note in
[`ontology/repos/deploy.yaml`](ontology/repos/deploy.yaml) for the known
gap on stg.

[`scripts/bootstrap-vps.sh`](scripts/bootstrap-vps.sh) is the **residual**
bootstrap, idempotent on re-run, kept for:

- **Pre-cloud-init VPSes** (the hand-made `isnad-graph-prod` box outside
  Terraform — decom tracked in deploy#86)
- **Gaps cloud-init does not yet cover** (#163): aux-repo dirs under `/opt/`,
  the systemd backup units, `git safe.directory` exceptions, and the
  append-with-dedup SSH key merge for operator-added admin keys

Run only when one of those conditions applies:

```bash
curl -sL https://raw.githubusercontent.com/noorinalabs/noorinalabs-deploy/main/scripts/bootstrap-vps.sh | bash
```

The script preflight-checks for the `deploy` user and `docker` binary and
exits fast if either is missing — i.e., it will not silently half-bootstrap
a host that should have been cloud-init'd but wasn't.

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
SITE_URL=https://isnad.noorinalabs.com ./scripts/verify_deployment.sh --skip-workflow
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

1. Open a revert PR for the offending merge:
   ```bash
   git revert -m 1 <bad-merge-sha>      # or: git revert <bad-squash-sha>
   git push -u origin revert-<sha>
   gh pr create --title "Revert: <original title>" --body "Reverts #<bad-pr>"
   ```
2. Merge the revert. `terraform.yml` re-applies with the prior config.
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
`scripts/backup.sh` returns non-zero. Three distinct credential paths
exist — all stored as GitHub Actions env-scoped secrets, then injected
onto the VPS via the deploy workflows. None of them live in a
hand-edited file on the box.

| Path | GH secret names (env scope) | VPS env-var names | Bucket | Consumer |
|---|---|---|---|---|
| Backups | `BACKUP_B2_KEY_ID`, `BACKUP_B2_APP_KEY`, `BACKUP_B2_BUCKET` | `B2_KEY_ID`, `B2_APP_KEY`, `B2_BUCKET` | `${B2_BUCKET}` (⚠️ see below — never hardcode it) | `scripts/backup.sh`, `scripts/restore.sh` (rclone-native via `RCLONE_CONFIG_ISNAD_*` exports inside the script) |
| Terraform state | `TF_STATE_B2_KEY_ID`, `TF_STATE_B2_APP_KEY` (also exposed as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` to the S3-protocol backend) | n/a (Actions only) | `noorinalabs-terraform-state` | `terraform.yml` |
| Pipeline ingest | `PIPELINE_B2_KEY_ID`, `PIPELINE_B2_KEY` | `PIPELINE_B2_KEY_ID`, `PIPELINE_B2_KEY` | (declared in data-acquisition repo) | data-acquisition / ingest workers |

Rotating one path does not affect the others.

> ### ⚠️ Never type a bucket name. Use `${B2_BUCKET}`. (deploy#636)
>
> **In any command you run, write `isnad:${B2_BUCKET}/<env>/…` — never a literal
> bucket name.** `${B2_BUCKET}` resolves to whatever the `BACKUP_B2_BUCKET`
> secret actually says, which is the only thing true by construction. A literal
> is a stale artifact waiting to happen, and this table is the proof: it used to
> name the wrong bucket, and it did so confidently, for months.
>
> Three names in this repo look alike. Two are buckets and only one is real:
>
> | Name | What it is |
> |---|---|
> | **`${B2_BUCKET}`** | the **B2 bucket** — every real backup lives here (9 `stg/` + 18 `prod/` objects, 2026-07-14). **Use this form.** Its literal value is the `BACKUP_B2_BUCKET` secret; do not hardcode it. |
> | **`/var/lib/noorinalabs-backups`** | a **local directory** on the VPS — the staging area `backup.sh` writes to before the rclone upload. **Not** a bucket at all. Does not need rotation. Its name is *confusingly close* to the bucket's; that resemblance is what misled this document. |
> | **`isnad-graph-backups`** | a **legacy, EMPTY B2 bucket**. It EXISTS, so nothing errors — it simply holds nothing, and never will. |
>
> The last row is the trap, and it is worse than a stale doc. An operator
> mid-incident copies a line naming it, runs
> `rclone lsf isnad:isnad-graph-backups/prod`, and gets `status=absent dumps=0
> bucket_objects=0` — a clean, confident **zero**, from the one check that reads
> B2 rather than a host-local gauge — and concludes **production has no
> restorable backup**. Measured against stg at the same moment:
>
> ```text
> isnad:isnad-graph-backups/prod  ->  status=absent  dumps=0  objects=0
> isnad:${B2_BUCKET}/prod         ->  status=fresh   dumps=3  objects=20  age=5h
> ```
>
> A false **RED**, and it sticks: an empty bucket lists `rc=0` with nothing in
> it, which is byte-for-byte the signature of a healthy-but-empty prefix. *A
> silent zero is not a measurement* — least of all in the document you only read
> when you are already in trouble.
>
> **And always name the environment.** stg and prod share this one bucket,
> namespaced `${B2_BUCKET}/stg/…` and `${B2_BUCKET}/prod/…` (deploy#632). A path
> at the bucket **root** reads both environments' objects as one pool, so a prod
> check can report healthy on the strength of a staging backup.

**Rotation procedure (backup path — most common):**

1. Generate a new B2 application key in the Backblaze console scoped to
   the `noorinalabs-backups` bucket only (read+write+delete).
2. Update the env-scoped GH secrets:
   ```bash
   gh secret set BACKUP_B2_KEY_ID  --env staging    --body "$NEW_KEY_ID"
   gh secret set BACKUP_B2_APP_KEY --env staging    --body "$NEW_APP_KEY"
   gh secret set BACKUP_B2_KEY_ID  --env production --body "$NEW_KEY_ID"
   gh secret set BACKUP_B2_APP_KEY --env production --body "$NEW_APP_KEY"
   ```
3. The new credentials reach the VPS on the next `deploy-stg.yml` /
   `deploy-prod.yml` run (the deploy workflow rewrites
   `/opt/noorinalabs-deploy/.env`, which `isnad-backup.service` reads
   via its `EnvironmentFile=` directive). To force a refresh without a
   real deploy, dispatch the relevant `deploy-*.yml` against the
   currently-pinned `IMAGE_TAG`.
4. Verify by SSHing to the VPS and triggering the unit immediately:
   ```bash
   sudo systemctl start isnad-backup.service
   sudo journalctl -u isnad-backup.service -n 200
   ```
   Confirm the rclone upload step exits 0 and a new dated subdirectory
   appears under **that host's own prefix** — never at the bucket root, and
   never against a hardcoded bucket name:
   ```bash
   rclone lsf "isnad:${B2_BUCKET}/stg/daily/"    # on stg
   rclone lsf "isnad:${B2_BUCKET}/prod/daily/"   # on prod
   ```
   An empty result here is **not** proof the backup is missing. Two different
   things both return `rc=0` with no output: a prefix the key is not permitted
   to list (deploy#634/#638), and a bucket that is simply empty — which is what
   you get if you typed a bucket name and typed the wrong one. Check
   `isnad_backup_retention_ok` and the journal before concluding anything from
   a zero.
5. Revoke the old keys in the Backblaze console only after step 4 has
   passed on **both** stg and prod.

**Rotation procedure (TF state path):**

1. Generate a new B2 application key scoped to
   `noorinalabs-terraform-state` only.
2. Update the env-scoped GH secrets:
   ```bash
   gh secret set TF_STATE_B2_KEY_ID  --env staging    --body "$NEW_KEY_ID"
   gh secret set TF_STATE_B2_APP_KEY --env staging    --body "$NEW_APP_KEY"
   gh secret set TF_STATE_B2_KEY_ID  --env production --body "$NEW_KEY_ID"
   gh secret set TF_STATE_B2_APP_KEY --env production --body "$NEW_APP_KEY"
   ```
3. Verify by opening a no-op `terraform.yml` PR (touch a comment-only
   line) and confirming both `plan` matrix entries (`stg`, `prod`)
   succeed.
4. Revoke the old keys after step 3 passes.

See [`docs/dependencies.md`](docs/dependencies.md) for the full
required-secrets table.

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

### Tier 0 — Prometheus alerts (firing today, paging not yet wired)

The signals in `infra/prometheus/alerts.yml` that are operationally
relevant to the deploy lifecycle:

- `BackupFailure` — fires when `scripts/emit-backup-failure-marker.sh`
  writes a textfile-collector marker (triggered by the
  `OnFailure=isnad-backup-failure-marker.service` directive on the
  backup unit).
- `BreakGlassUsed` — anyone invoking the break-glass `workflow_dispatch`
  inputs in `promote.yml` / `deploy-prod.yml` (see
  [`docs/runbooks/break-glass.md`](docs/runbooks/break-glass.md)).
- `BreakGlassMetricStale`, `UserServiceAlembicGateFailure`,
  `UserServiceAlembicGateStale`, `BlackboxProbeFailing`,
  `BlackboxUnexpectedStatus`, `BlackboxCertExpiringSoon`,
  `ServiceDown`, `ContainerUnhealthy`, `HighErrorRate`, `HighLatencyP95`,
  `HighDiskUsage`, `HighMemoryUsage` — see `infra/prometheus/alerts.yml`
  for the full set.

There is no `DeployFailed` Prometheus alert — `verify-deploy.yml` is a
GitHub Actions job, not a Prometheus signal source. Workflow failures
surface as red runs in the Actions tab and as `::error::` annotations.

> **External paging — wired, owner-secret-gated.** The per-env Alertmanager
> configs (`infra/alertmanager/alertmanager.{stg,prod}.yml`, deploy#262/#263)
> route both `default` and `critical` to **Slack** (primary) and **Email**
> (independent backup), with a **Healthchecks.io dead-man's switch** for the
> monitor itself (deploy#452/#453). The wiring is complete and validated; each
> channel goes live the moment its secret is set in the matching GitHub
> Environment — `SLACK_WEBHOOK_URL`, `SMTP_PASSWORD` (+ owner-edited
> `smtp_*`/`to:` literals), and `HEALTHCHECKS_PING_URL`. Until a given secret
> is set, that channel writes the `<unset>` placeholder and no-ops (the config
> still loads cleanly); other channels are unaffected. Activation, the
> channel-swap procedure, troubleshooting, and rotation are in
> [`docs/runbooks/alertmanager-slack-routing.md`](docs/runbooks/alertmanager-slack-routing.md).
> While paging is unactivated, treat the Tier-1 SRE on-call as the de-facto
> first responder and rely on Grafana dashboards plus
> `gh run list --workflow=verify-deploy.yml` to surface deploy failures.

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
