# Terraform backend configuration

Every root module in `terraform/` uses the **same S3-compatible Backblaze B2 bucket** for remote state: `noorinalabs-terraform-state`. Each root module writes to its own `key` so the four state files (`hetzner/stg.tfstate`, `hetzner/prod.tfstate`, `cloudflare/terraform.tfstate`, `backblaze/terraform.tfstate`) never collide.

## Required backend block (TF 1.6+)

```hcl
terraform {
  backend "s3" {
    bucket = "noorinalabs-terraform-state"
    key    = "<module>/<env>.tfstate"
    region = "us-east-005"
    endpoints = {
      s3 = "https://s3.us-east-005.backblazeb2.com"
    }
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
  }
}
```

The current root modules are:

| Module path | State key |
|---|---|
| `terraform/hetzner/envs/stg/` | `hetzner/stg.tfstate` |
| `terraform/hetzner/envs/prod/` | `hetzner/prod.tfstate` |
| `terraform/cloudflare/` | `cloudflare/terraform.tfstate` |
| `terraform/backblaze/` | `backblaze/terraform.tfstate` |

## `endpoints` (plural) vs `endpoint` (singular) — the trap

Terraform's S3 backend grew a **new `endpoints` attribute (plural, map-shape) in v1.6**. The pre-1.6 form used a top-level `endpoint = "<url>"` (singular, string). Both attributes are accepted by the parser to keep older configs working through the deprecation window, but they are **not interchangeable**:

- **`endpoint` (singular)** — pre-1.6 form. Deprecated in 1.6+, removed in a future release. Continues to parse without error today.
- **`endpoints = { s3 = "<url>" }` (plural)** — 1.6+ form. Required when any other non-default backend behavior is in use; future-proof.

The classic failure mode (which directly motivated this doc — see deploy#23 and the originating PR #22 incident):

1. Operator copies an older example using `endpoint = "<url>"`.
2. `terraform init` succeeds (parser accepts both forms).
3. `terraform validate` succeeds (validate doesn't connect anywhere).
4. CI's `plan` job authenticates against the **default AWS S3 endpoint** instead of B2 — because `endpoint` (singular) was silently demoted to a no-op by the new parser path.
5. The failure surfaces as a generic AWS auth error during `init` against the wrong endpoint, not as a config-shape error.

**Use the plural `endpoints = { s3 = ... }` form everywhere. tflint's terraform-recommended preset (wired in `.github/workflows/terraform.yml`'s `tflint` job as of deploy#23) catches mismatches at PR-time.**

## Required variables must not be empty (plan-time guard)

**Every required variable (no `default`) MUST carry a non-empty `validation` block so that emptiness fails at `terraform plan` in CI — never at `apply` against the live provider.**

```hcl
variable "noorinalabs_net_zone_id" {
  description = "Cloudflare Zone ID for noorinalabs.net (canonical-domain redirect target zone)."
  type        = string
  validation {
    condition     = length(trimspace(var.noorinalabs_net_zone_id)) > 0
    error_message = "noorinalabs_net_zone_id must not be empty — set repo Actions variable CLOUDFLARE_NOORINALABS_NET_ZONE_ID."
  }
}
```

The `error_message` MUST name the **source** (the repo Actions secret/variable or env-protected secret that feeds the `TF_VAR_*`) so a CI failure tells the operator exactly what to set.

The classic failure mode this guards against (deploy#346; originating PR #344 partial prod apply, 2026-05-20):

1. A variable is declared *required* (no `default`), but is wired in CI from a repo Actions variable that was never created — so `TF_VAR_…` resolves to an **empty string**.
2. An empty string still satisfies "required" — Terraform errors only on an *unset* required variable, not an empty-but-set one.
3. `terraform plan` passes clean in CI; the gap bites only at `apply`, where the provider rejects the malformed call (an empty `noorinalabs_net_zone_id` produced `/zones//rulesets` → "Could not route … perhaps your object identifier is invalid?").
4. Result: a partial prod apply on a class of error a plan-time guard would have caught in the PR.

Note: `terraform validate -backend=false` (the CI `validate` job, which passes no vars) does **not** evaluate `validation` conditions — so adding these blocks does not break the no-vars validate job. They fire at `plan`/`apply`, exactly where the empty value would otherwise slip through.

### Exemption — CI-wired vars that resolve to `default=""` in real plans

A small set of credential/config variables carry `default = ""` and are intentionally empty in real CI plans/applies — a hard non-empty guard on them would break the existing green `plan` job (which does not thread them in as `TF_VAR_*`). These are exempt; document which and why in the variable's `description`. Current exemptions:

| Variable(s) | Why exempt |
|---|---|
| `ghcr_auth_b64`, `user_postgres_password`, `user_redis_password`, `user_service_jwt_secret` (hetzner env roots + `hetzner-vps` module) | Never wired as `TF_VAR_*` in the hetzner `plan`/`apply` jobs (only `TF_VAR_hcloud_token` is); consumed as cloud-init template fields that tolerate empty. The module's `user_*` keep their `== "" \|\| length >= N` allow-empty validation. |
| `deploy_ssh_public_key_path`, `root_ssh_public_key_path` (hetzner env roots + module) | Carry meaningful checked-in-pubkey defaults so cold-rebuild/CI provisions without an operator-local path (ADR 0006). |
| `net_redirect_ruleset_id`, `org_redirect_ruleset_id` (cloudflare) | CI-discovered at plan/apply time; `default=""` so the no-var `validate` job parses. |

Any new exemption requires an explicit rationale in the variable's `description`.

## Required CI credentials

The four root modules all read auth from the same env-var pair:

| Env var (TF backend reads it) | Source secret name |
|---|---|
| `AWS_ACCESS_KEY_ID` | `TF_STATE_B2_KEY_ID` |
| `AWS_SECRET_ACCESS_KEY` | `TF_STATE_B2_APP_KEY` |

These are set per-GH-Environment (`staging` and `production`) so rotation of the B2 application key only requires updating both env secret slots, not a per-module secret. The `validate-creds` job in `.github/workflows/terraform.yml` probes both envs on every push/PR/Monday-cron to surface rotation drift before any `plan` runs.

## Why all four modules share one bucket

One bucket, separate state keys keeps the `terraform_remote_state` data sources simple. The cloudflare module reads hetzner state via `data "terraform_remote_state" "hetzner_prod"` (see `terraform/cloudflare/main.tf`) using the same B2 auth — no second credential pair to manage. Same for any future module that needs to read VPS IPs or backblaze bucket names from sibling state.

## Adding a new root module — checklist

1. Create `terraform/<module>/versions.tf` (or `backend.tf`) with the block above; pick a unique state key.
2. Add the module path to `.github/workflows/terraform.yml`'s `validate`, `tflint`, and `plan-<module>` / `apply-<module>` jobs.
3. Wire any per-module provider creds via `TF_VAR_*` in the workflow `env:` block.
4. Add a non-empty `validation` block to every required (no-`default`) variable, with a source-naming `error_message` (see "Required variables must not be empty" above). Document any `default=""` exemption in the variable's `description`.
5. If the new module needs to read sibling state, use `data "terraform_remote_state"` pointing at the same bucket — no extra auth needed.

## Pre-existing checkov skips

The `checkov` job in `.github/workflows/terraform.yml` runs against `terraform/` with `soft_fail: false`, so any **new** finding blocks the PR. Pre-existing findings (if any surface on first CI run) should be skipped inline at the resource level using:

```hcl
resource "<type>" "<name>" {
  # checkov:skip=CKV_AWS_xxx: <one-line justification>
  ...
}
```

If a skip is added, document the justification in the inline comment AND in a follow-up issue if the skip represents real tech-debt that should be revisited.
