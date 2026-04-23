# `modules/hetzner-vps` — Shared Hetzner VPS module

Provisions one Hetzner Cloud VPS plus its firewall and SSH key, bootstrapped with cloud-init. Consumed by per-env root modules under `terraform/hetzner/envs/{stg,prod}/`.

This module is **intentionally backend-less**. Backend configuration lives in each env root module so state is isolated per env — see ADR `docs/adr/0001-tf-hetzner-per-env-state-strategy.md`.

## Resources

| Resource | Name |
|---|---|
| `hcloud_server.app` | `noorinalabs-${var.env}` |
| `hcloud_firewall.web` | `noorinalabs-${var.env}-firewall` |
| `hcloud_ssh_key.deploy` | `noorinalabs-${var.env}-deploy` |

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

| Output | Consumer |
|---|---|
| `env` | All downstream — echoes env tag |
| `server_name` | `deploy#83` (Cloudflare) as the canonical host identifier |
| `server_ip` | `deploy#83` A-record target; `deploy#84` SSH target |
| `server_ipv6` | Optional AAAA-record target |
| `ssh_target` | `deploy#84` promotion workflow — already formatted as `deploy@<ip>` |
| `labels` | Tooling that discovers per-env resources by Hetzner label |
| `server_status` | Debugging / verify step |

## Provider

```hcl
hcloud = {
  source  = "hetznercloud/hcloud"
  version = "~> 1.49"
}
```

The provider `token` is configured by the calling env module, not here — the module never reads `HCLOUD_TOKEN` directly.
