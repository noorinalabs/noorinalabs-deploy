# Secrets inventory

Source: [deploy#11](https://github.com/noorinalabs/noorinalabs-deploy/issues/11)
(ops: Secrets management and rotation).
Companion: [`runbooks/secret-rotation-policy.md`](runbooks/secret-rotation-policy.md)
(the policy + cadence + central-management decision this inventory feeds).
Related: [`env-inventory.md`](env-inventory.md) is the **generated env-var
reference** (every env-var consumer across all 7 repos, #116); THIS file is the
curated **secrets** view — owner, rotation cadence, env-separation, and runbook
per secret. They are different lenses; keep both.

> **Scope:** secrets consumed by the `noorinalabs-deploy` deployment surface
> (GH Actions Environment/repo secrets + Terraform-injected credentials).
> Not values — names and metadata only (per deploy#116 scope). Maintain this
> table by hand when the secret surface changes; it is not auto-generated.

## Current posture (as of this PR)

- Secrets live in **GitHub Actions secrets** (repo-level + per-Environment),
  surfaced into CI with masking and written to the VPS `.env` (chmod 600) at
  deploy time over SSH, or injected via Terraform cloud-init `user_data`.
- **No central secrets manager** (Vault/Doppler/SOPS) — the central-management
  option is an open **owner decision**, framed in
  [`secret-rotation-policy.md`](runbooks/secret-rotation-policy.md) § Central
  management — open decision. This PR does **not** pick a tool.
- **Env separation:** `staging` and `production` GitHub Environments hold
  separate values for the per-env secrets; a few account-wide / repo-level
  credentials are intentionally shared (flagged below).

## Rotation-cadence legend

| Cadence | Meaning |
|---|---|
| **annual** | scheduled yearly + on-demand triggers (ADR 0004 Decision C) |
| **on-demand** | rotate on offboarding / suspected compromise only (no calendar) |
| **on-deploy** | value re-materialises into `.env` on every deploy; rotate by changing the GH secret + redeploy |
| **provider** | lifecycle owned by the upstream provider (OAuth app, GitHub PAT) |
| **manual** | rotate by an operator runbook; no automation yet |

## Inventory

### Terraform / infrastructure credentials

| Secret | Class | Owner role | Env-separated | Rotation | Runbook |
|---|---|---|---|---|---|
| `HCLOUD_TOKEN` | Hetzner API token | Infra Manager | shared (account) | on-demand | — (provider console) |
| `TF_STATE_B2_KEY_ID` / `TF_STATE_B2_APP_KEY` | B2 state-bucket key | Platform Architect | per-env (stg/prod) | **annual** | [`state-key-annual-rotation.md`](runbooks/state-key-annual-rotation.md) |
| `B2_MASTER_KEY_ID` / `B2_MASTER_APP_KEY` | B2 master key (account-wide) | Platform Architect | shared (per-operator) | on-demand | ADR 0004 Decision D — **must not be in CI** (being retired from CI, see #361) |
| `CLOUDFLARE_API_TOKEN` | Cloudflare API token | Infra Manager | shared | on-demand | — (provider console) |

### SSH access

| Secret | Class | Owner role | Env-separated | Rotation | Runbook |
|---|---|---|---|---|---|
| `DEPLOY_SSH_PRIVATE_KEY` | deploy@VPS private key | Infra Manager | per-env (stg/prod) | on-demand | [`ssh-key-rotation.md`](runbooks/ssh-key-rotation.md) (per-env, per-role — ADR 0006) |
| *root keys* (`noorinalabs_{stg,prod}_root`) | root@VPS — **never in any GH secret** | Owner | per-env | on-demand | [`ssh-key-rotation.md`](runbooks/ssh-key-rotation.md) |

### Application secrets (state-resident — see #193)

These reach the VPS via Terraform cloud-init `user_data` and are captured in the
hetzner tfstate; a one-time defense-in-depth rotation is tracked in
[deploy#193](https://github.com/noorinalabs/noorinalabs-deploy/issues/193)
(state-resident-secret-rotation runbook lands with that PR).

| Secret | Class | Owner role | Env-separated | Rotation | Notes |
|---|---|---|---|---|---|
| `user_service_jwt_secret` | user-service JWT signing secret | Security Eng | per-env | on-demand (highest blast radius — kicks sessions) | #193 |
| `ghcr_auth_b64` | GHCR pull cred (base64 `user:pat`) | Security Eng | per-env | **provider** (PAT) + on-deploy | #193 |
| `user_postgres_password` | user-service Postgres | Security Eng | per-env | on-demand | rotated 2026-04-30 (#126) |
| `user_redis_password` | user-service Redis | Security Eng | per-env | on-demand | rotated 2026-04-30 (#126) |

### Application secrets (deploy-time `.env`)

| Secret | Class | Owner role | Env-separated | Rotation | Notes |
|---|---|---|---|---|---|
| `NEO4J_PASSWORD` | isnad-graph Neo4j | Infra Manager | per-env | on-deploy / manual | — |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | isnad-graph Postgres | Infra Manager | per-env | on-deploy / manual | `*_DB`/`*_USER` are low-sensitivity |
| `REDIS_PASSWORD` | isnad-graph Redis | Infra Manager | per-env | on-deploy / manual | — |
| `USER_POSTGRES_*` / `USER_REDIS_PASSWORD` | user-service stores (deploy `.env` copy) | Security Eng | per-env | on-deploy / manual | rotated 2026-04-30 (#126) |
| `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY` | JWT signing keypair (deploy `.env` path) | Security Eng | per-env | on-demand | distinct from `user_service_jwt_secret` (symmetric, cloud-init path) |
| `GRAFANA_ADMIN_PASSWORD` | Grafana admin | Observability Eng | per-env | on-deploy / manual | — |
| `KAFKA_CLUSTER_ID` / `KAFKA_UI_USER` / `KAFKA_UI_PASSWORD` | pipeline Kafka UI | Observability Eng | per-env | on-deploy / manual | `CLUSTER_ID` is config, not secret |

### Storage / backup credentials

| Secret | Class | Owner role | Env-separated | Rotation | Notes |
|---|---|---|---|---|---|
| `PIPELINE_B2_KEY` / `PIPELINE_B2_KEY_ID` | pipeline bucket RW key | Security Eng | per-env | on-demand | minted by `terraform/backblaze/`; #193 |
| `BACKUP_B2_KEY_ID` / `BACKUP_B2_APP_KEY` | backup bucket key | Infra Manager | per-env | on-demand | — |
| `PIPELINE_B2_*` / `BACKUP_B2_BUCKET` / `*_ENDPOINT` / `*_REGION` | bucket config (not secret) | — | — | n/a | public-ish config, listed for completeness |

### Integration / OAuth / misc

| Secret | Class | Owner role | Env-separated | Rotation | Notes |
|---|---|---|---|---|---|
| `AUTH_GOOGLE_CLIENT_ID` / `AUTH_GOOGLE_CLIENT_SECRET` | Google OAuth app | Security Eng | per-env | **provider** | rotate in Google Cloud console |
| `AUTH_GITHUB_CLIENT_ID` / `AUTH_GITHUB_CLIENT_SECRET` | GitHub OAuth app | Security Eng | per-env | **provider** | rotate in GitHub OAuth app settings |
| `GH_PACKAGES_TOKEN` | GHCR packages PAT (CI) | Infra Manager | repo-level | **provider** (PAT) | — |
| `GITHUB_TOKEN` | GH Actions ephemeral token | n/a (per-run) | n/a | per-run (auto) | not operator-rotatable |
| `SLACK_WEBHOOK_URL` | Alertmanager Slack webhook (primary alert channel) | Observability Eng | per-env | on-demand | rotate in Slack app config |
| `SMTP_PASSWORD` | Alertmanager Email-backup SMTP password (deploy#452) | Observability Eng | per-env | on-demand | rotate at SMTP relay (app password) |
| `HEALTHCHECKS_PING_URL` | Alertmanager dead-man's-switch ping URL (deploy#453) | Observability Eng | per-env | on-demand | regenerate check ping URL in Healthchecks.io |
| `STG_TEST_USER_EMAIL` / `STG_TEST_USER_PASSWORD` | staging smoke-test creds | SRE | staging only | on-demand | low sensitivity (test account) |

## Machine-readable counterpart (deploy#513)

This human table has a structured counterpart at
[`../scripts/secret_rotation_inventory.yaml`](../scripts/secret_rotation_inventory.yaml),
consumed by the deterministic rotation engine
[`../scripts/secret_rotation.py`](../scripts/secret_rotation.py) to compute
rotation-due status, per-class refresh plans, and self-rescheduling (see
[`runbooks/secret-rotation-policy.md`](runbooks/secret-rotation-policy.md) §
Deterministic rotation engine). Its test suite reconciles the YAML secret-name
set against this file, so the two inventories cannot silently drift — when you
edit the table below, mirror the change into the YAML.

## Maintenance

Update this table whenever the secret surface changes (a new GH secret, a new
Terraform-injected credential, a service added/removed). The generated
[`env-inventory.md`](env-inventory.md) is the cross-check: a `secrets.*`
reference appearing there but missing here is an inventory gap. The
[secret-rotation-policy](runbooks/secret-rotation-policy.md) § Cadence table is
derived from the **Rotation** column above — keep them in sync.
