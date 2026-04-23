# Terraform — Hetzner Cloud per-env provisioning

Provisions two Hetzner Cloud VPS instances:

| Env | Server name | Type | Location | State key |
|---|---|---|---|---|
| `stg` | `noorinalabs-stg` | CPX21 (3 vCPU, 4 GB) | Ashburn (ash) | `hetzner/stg.tfstate` |
| `prod` | `noorinalabs-prod` | CPX41 (8 vCPU, 16 GB) | Ashburn (ash) | `hetzner/prod.tfstate` |

Both envs run the same cloud-init bootstrap (Docker, Caddy, fail2ban, ufw, GHCR auth, user-service env file). All application services run via Docker Compose on the resulting VPS.

## Layout

```
terraform/hetzner/
├── README.md                          # this file
├── modules/
│   └── hetzner-vps/                   # shared child module
│       ├── main.tf                    # hcloud_server + firewall + ssh_key
│       ├── variables.tf
│       ├── outputs.tf
│       ├── versions.tf                # provider constraints ONLY
│       ├── cloud-init.yaml.tpl
│       └── README.md
└── envs/
    ├── stg/                           # root module — state: hetzner/stg.tfstate
    │   ├── main.tf                    # provider + module "vps" call
    │   ├── backend.tf                 # B2 S3 backend, key=hetzner/stg.tfstate
    │   ├── variables.tf
    │   ├── outputs.tf
    │   ├── versions.tf
    │   └── terraform.tfvars.example
    └── prod/                          # root module — state: hetzner/prod.tfstate
        ├── main.tf
        ├── backend.tf                 # B2 S3 backend, key=hetzner/prod.tfstate
        ├── variables.tf
        ├── outputs.tf
        ├── versions.tf
        └── terraform.tfvars.example
```

Each env is a self-contained Terraform root module. **Working directory IS the env selector** — `cd envs/stg` vs `cd envs/prod`. There is no Terraform workspace used; see `docs/adr/0001-tf-hetzner-per-env-state-strategy.md` for why.

## Deployment Model

```
Terraform manages:                    Docker Compose manages:
├── VPS (hcloud_server)               ├── isnad-graph (FastAPI + React)
├── Firewall (hcloud_firewall)        ├── neo4j
├── SSH Key (hcloud_ssh_key)          ├── user-service (FastAPI)
└── cloud-init bootstrap              ├── user-postgres
                                      ├── user-redis
                                      └── caddy (reverse proxy)
```

Terraform does NOT manage individual containers — that is Docker Compose's responsibility.

## Prerequisites

- [Terraform >= 1.6](https://developer.hashicorp.com/terraform/install)
- A Hetzner Cloud API token (create one at https://console.hetzner.cloud)
- An SSH key pair
- Backblaze B2 application key scoped to the `noorinalabs-terraform-state` bucket

## Backend credentials

The S3-compatible backend authenticates via standard AWS environment variables. Set these before any Terraform command (in both envs):

```bash
export AWS_ACCESS_KEY_ID="your-b2-application-key-id"
export AWS_SECRET_ACCESS_KEY="your-b2-application-key"
```

## Usage

```bash
# stg
cd terraform/hetzner/envs/stg
terraform init
terraform plan   -var-file=terraform.tfvars
terraform apply  -var-file=terraform.tfvars

# prod (independent state — separate apply)
cd terraform/hetzner/envs/prod
terraform init
terraform plan   -var-file=terraform.tfvars
terraform apply  -var-file=terraform.tfvars
```

Copy `terraform.tfvars.example` → `terraform.tfvars` in each env and fill in real values. `terraform.tfvars` is git-ignored.

## Outputs

Both envs expose the same output shape:

| Output | Description | Consumer |
|---|---|---|
| `env` | Environment tag (`stg` or `prod`) | All |
| `server_name` | `noorinalabs-stg` / `noorinalabs-prod` | `deploy#83` (Cloudflare) |
| `server_ip` | Public IPv4 | `deploy#83`, `deploy#84` |
| `server_ipv6` | Public IPv6 | Optional AAAA target |
| `ssh_target` | `deploy@<ipv4>` | `deploy#84` promotion workflow |
| `labels` | `{ project, environment }` map | Tooling that queries Hetzner by label |
| `server_status` | `running` / etc. | Debugging |

## State isolation

- `stg` → B2 object `noorinalabs-terraform-state/hetzner/stg.tfstate`
- `prod` → B2 object `noorinalabs-terraform-state/hetzner/prod.tfstate`

A `terraform destroy` in `envs/stg` **cannot** touch prod state, and vice versa. This is the primary failure-mode guard — see ADR 0001.

## Migrating the existing prod state

The pre-refactor root at `terraform/hetzner/` used state key `hetzner/terraform.tfstate`. One-time migration (performed by an SRE, not automated in code):

```bash
# 1. Back up
aws s3 cp s3://noorinalabs-terraform-state/hetzner/terraform.tfstate ./hetzner-terraform.tfstate.backup \
  --endpoint-url https://s3.us-east-005.backblazeb2.com

# 2. Rename the B2 object
aws s3 mv s3://noorinalabs-terraform-state/hetzner/terraform.tfstate \
          s3://noorinalabs-terraform-state/hetzner/prod.tfstate \
  --endpoint-url https://s3.us-east-005.backblazeb2.com

# 3. Init the new prod env
cd terraform/hetzner/envs/prod
terraform init
terraform plan   # MUST show changes only for the rename (noorinalabs-isnad-graph-prod → noorinalabs-prod)
```

Per issue #82 and the owner's confirmation, the existing `noorinalabs-isnad-graph-prod` resources may be destroyed/recreated — no data preservation required. The currently live production site runs on a hand-made VPS outside Terraform (`isnad-graph-prod`), tracked for decommission in `deploy#86`.

## Secrets flow

Same as before — sensitive Terraform variables are passed via `-var` flags in CI (GitHub Actions repository secrets) or via a git-ignored `terraform.tfvars`. Cloud-init writes them to `/opt/noorinalabs-deploy/.env.user-service` (mode 0600, owned by `deploy`) on the VPS; Docker Compose reads that file at container startup.

To add a new secret:
1. Add a `variable` block in `modules/hetzner-vps/variables.tf` (with `sensitive = true`).
2. Thread it through `templatefile()` in `modules/hetzner-vps/main.tf`.
3. Write it to the appropriate env file in `modules/hetzner-vps/cloud-init.yaml.tpl`.
4. Add a matching `variable` in each env's `variables.tf`.
5. Pass it into the `module "vps"` block in each env's `main.tf`.
6. Add the GitHub Actions secret in the repo settings (per-env if they differ).

## ADRs

- [`docs/adr/0001-tf-hetzner-per-env-state-strategy.md`](../../docs/adr/0001-tf-hetzner-per-env-state-strategy.md) — rationale for `envs/{stg,prod}/` layout over Terraform workspaces.
