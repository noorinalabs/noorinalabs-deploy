# `modules/hetzner-vps` — Shared Hetzner VPS module

Provisions one Hetzner Cloud VPS plus its firewall, bootstrapped with cloud-init. Consumed by per-env root modules under `terraform/hetzner/envs/{stg,prod}/`.

This module is **intentionally backend-less**. Backend configuration lives in each env root module so state is isolated per env — see ADR `docs/adr/0001-tf-hetzner-per-env-state-strategy.md`.

## Resources

| Resource | Name |
|---|---|
| `hcloud_server.app` | `noorinalabs-${var.env}` |
| `hcloud_firewall.web` | `noorinalabs-${var.env}-firewall` |

SSH authorized keys (`/root/.ssh/authorized_keys` and `/home/deploy/.ssh/authorized_keys`) are injected via `cloud-init.yaml.tpl`; there is no `hcloud_ssh_key` resource.

All resources carry labels `{ project = "noorinalabs", environment = var.env }`.

## Inputs (all required unless defaulted)

| Name | Type | Default | Sensitive | Notes |
|---|---|---|---|---|
| `env` | string | — | no | One of `stg`, `prod`. Validated. |
| `server_type` | string | — | no | e.g., `cpx21` (stg), `cpx41` (prod). |
| `location` | string | `ash` | no | Hetzner location code. |
| `image` | string | `ubuntu-24.04` | no | |
| `ssh_public_key_path` | string | `~/.ssh/id_ed25519.pub` | no | |
| `ssh_source_ips` | list(string) | `["0.0.0.0/0", "::/0"]` | no | Restrict for prod. |
| `ghcr_auth_b64` | string | `""` | **yes** | Base64 `username:token` for GHCR. |
| `user_postgres_password` | string | `""` | **yes** | ≥16 chars when set. |
| `user_redis_password` | string | `""` | **yes** | ≥16 chars when set. |
| `user_service_jwt_secret` | string | `""` | **yes** | ≥32 chars when set. |

## Outputs (consumed by downstream)

All outputs are **Class A — publicly safe** per ADR 0002 (`docs/adr/0002-hetzner-outputs-classification.md`). The cloudflare module reads `server_ip` + `server_ipv6` via `terraform_remote_state` and is the primary consumer; other outputs are available to any future consumer without security review (Class A is the no-gate path).

When adding a new output, classify it as Class A or B per ADR 0002 and follow that ADR's reviewer checklist. Do NOT rely on `sensitive = true` as an access barrier — `terraform_remote_state` consumers can read sensitive outputs.

| Output | Class | Consumer |
|---|---|---|
| `env` | A | All downstream — echoes env tag |
| `server_name` | A | `deploy#83` (Cloudflare) as the canonical host identifier |
| `server_ip` | A | `deploy#83` A-record target; `deploy#84` SSH target — `terraform_remote_state` consumed by cloudflare |
| `server_ipv6` | A | Optional AAAA-record target — `terraform_remote_state` consumed by cloudflare |
| `ssh_target` | A | `deploy#84` promotion workflow — already formatted as `deploy@<ip>` |
| `labels` | A | Tooling that discovers per-env resources by Hetzner label |
| `server_status` | A | Debugging / verify step |

## Provider

```hcl
hcloud = {
  source  = "hetznercloud/hcloud"
  version = "~> 1.49"
}
```

The provider `token` is configured by the calling env module, not here — the module never reads `HCLOUD_TOKEN` directly.
