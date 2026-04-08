# Terraform — Hetzner Cloud Provisioning

Provisions a Hetzner Cloud VPS (CPX41) for the NoorinALabs production deployment. All application services (isnad-graph, user-service) run on this single VPS via Docker Compose.

## Deployment Model

```
Terraform manages:                Docker Compose manages:
├── VPS (hcloud_server)           ├── isnad-graph (FastAPI + React)
├── Firewall (hcloud_firewall)    ├── neo4j
├── SSH Key (hcloud_ssh_key)      ├── user-service (FastAPI)
└── cloud-init bootstrap          ├── user-postgres
                                  ├── user-redis
                                  └── caddy (reverse proxy)
```

- **Terraform** provisions the VPS, firewall rules, SSH keys, and runs cloud-init on first boot.
- **cloud-init** installs Docker, hardens the OS, writes environment/secret files, and pre-creates Docker volumes.
- **Docker Compose** (in the deploy repo) manages all application containers, networks, and runtime config.

Terraform does NOT manage individual containers — that is Docker Compose's responsibility.

## Resources Created

- **VPS**: CPX41 (8 vCPU, 16 GB RAM) running Ubuntu 24.04 in Ashburn (ash)
- **Firewall**: Allows inbound TCP on ports 22 (SSH), 80 (HTTP), 443 (HTTPS); denies all else
- **SSH Key**: Uploaded from a local public key file

## Prerequisites

- [Terraform >= 1.5](https://developer.hashicorp.com/terraform/install)
- A Hetzner Cloud API token (create one at https://console.hetzner.cloud)
- An SSH key pair

## Usage

```bash
cd terraform/hetzner

# Initialize providers
terraform init

# Preview changes
terraform plan -var="hcloud_token=YOUR_TOKEN"

# Apply
terraform apply -var="hcloud_token=YOUR_TOKEN"

# Destroy when no longer needed
terraform destroy -var="hcloud_token=YOUR_TOKEN"
```

To avoid passing secrets on every command, create a `terraform.tfvars` file (git-ignored):

```hcl
hcloud_token            = "your-token-here"
ssh_public_key_path     = "~/.ssh/id_ed25519.pub"
user_postgres_password  = "a-strong-password-at-least-16-chars"
user_redis_password     = "a-strong-password-at-least-16-chars"
user_service_jwt_secret = "a-strong-secret-at-least-32-chars"
```

## Variables

| Name | Description | Sensitive | Default |
|------|-------------|-----------|---------|
| `hcloud_token` | Hetzner Cloud API token | Yes | — |
| `ssh_public_key_path` | Path to SSH public key | No | `~/.ssh/id_ed25519.pub` |
| `server_type` | Hetzner server type | No | `cpx41` |
| `server_name` | Instance name | No | `noorinalabs-isnad-graph-prod` |
| `location` | Hetzner location | No | `ash` |
| `ssh_source_ips` | CIDR ranges allowed to SSH | No | `["0.0.0.0/0", "::/0"]` |
| `ghcr_auth_b64` | Base64 `username:token` for GHCR auth | Yes | `""` |
| `user_postgres_password` | Password for user-service PostgreSQL (min 16 chars) | Yes | `""` |
| `user_redis_password` | Password for user-service Redis (min 16 chars) | Yes | `""` |
| `user_service_jwt_secret` | JWT signing secret for user-service (min 32 chars) | Yes | `""` |

## Outputs

| Name | Description |
|------|-------------|
| `server_ip` | Public IPv4 address for DNS setup |
| `server_ipv6` | Public IPv6 address |
| `server_status` | Current server status |

## Secrets Management

Secrets flow through two paths:

1. **CI/CD (GitHub Actions):** Encrypted repository secrets are passed as `-var` flags during `terraform plan`/`apply`.
2. **VPS runtime:** cloud-init writes secrets to `/opt/noorinalabs-deploy/.env.user-service` (mode 0600, owned by `deploy`). Docker Compose reads this env file at container startup.

To add a new secret:
1. Add a `variable` block in `variables.tf` (with `sensitive = true`)
2. Pass it through `templatefile()` in `main.tf`
3. Write it to the appropriate env file in `cloud-init.yaml.tpl`
4. Add the GitHub Actions secret in the repo settings
5. Reference the variable in the CI workflow's `terraform apply` step

## Remote State Backend

State is stored remotely in a Backblaze B2 bucket (`noorinalabs-terraform-state`) using the S3-compatible backend. This provides:

- **Team collaboration** — multiple operators can plan/apply without passing state files around
- **State locking** — prevents concurrent applies from corrupting state
- **Automatic backups** — B2 retains object versions

### Backend Credentials

The backend authenticates via standard AWS environment variables. Set these before running any Terraform commands:

```bash
export AWS_ACCESS_KEY_ID="your-b2-application-key-id"
export AWS_SECRET_ACCESS_KEY="your-b2-application-key"
```

To generate B2 application keys:
1. Log in to [Backblaze B2](https://secure.backblaze.com/b2_buckets.htm)
2. Go to **App Keys** > **Add a New Application Key**
3. Restrict the key to the `noorinalabs-terraform-state` bucket
4. Copy the `keyID` as `AWS_ACCESS_KEY_ID` and `applicationKey` as `AWS_SECRET_ACCESS_KEY`

### Migrating from Local State

If you have an existing local `terraform.tfstate` file, migrate it to the remote backend:

```bash
cd terraform/hetzner

# Initialize the new backend — Terraform will detect the change and offer to migrate
terraform init -migrate-state

# Verify the migration succeeded
terraform plan   # should show no changes

# Remove the local state file (now safe — state lives in B2)
rm terraform.tfstate terraform.tfstate.backup
```

### First-Time Setup (No Existing State)

If this is a fresh checkout with no local state:

```bash
cd terraform/hetzner
terraform init
```

Terraform will configure the S3 backend and pull any existing remote state automatically.
