# Terraform — Hetzner Cloud Provisioning

Provisions a Hetzner Cloud VPS (CPX41) for the noorinalabs-isnad-graph production deployment.

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
cd terraform/

# Initialize providers
terraform init

# Preview changes
terraform plan -var="hcloud_token=YOUR_TOKEN"

# Apply
terraform apply -var="hcloud_token=YOUR_TOKEN"

# Destroy when no longer needed
terraform destroy -var="hcloud_token=YOUR_TOKEN"
```

To avoid passing the token on every command, create a `terraform.tfvars` file (git-ignored):

```hcl
hcloud_token       = "your-token-here"
ssh_public_key_path = "~/.ssh/id_ed25519.pub"
```

## Variables

| Name | Description | Default |
|------|-------------|---------|
| `hcloud_token` | Hetzner Cloud API token (sensitive) | — |
| `ssh_public_key_path` | Path to SSH public key | `~/.ssh/id_ed25519.pub` |
| `server_type` | Hetzner server type | `cpx41` |
| `server_name` | Instance name | `noorinalabs-isnad-graph-prod` |
| `location` | Hetzner location | `ash` |

## Outputs

| Name | Description |
|------|-------------|
| `server_ip` | Public IPv4 address for DNS setup |
| `server_ipv6` | Public IPv6 address |
| `server_status` | Current server status |

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
