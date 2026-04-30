# ADR 0001 — Terraform Hetzner per-env state strategy

- **Status:** Accepted
- **Date:** 2026-04-22
- **Author:** Weronika Zielinska (Platform Architect)
- **Context issue:** [deploy#82](https://github.com/noorinalabs/noorinalabs-deploy/issues/82), meta-issue `noorinalabs-main#141`
- **Supersedes:** none
- **Superseded by:** none

## Context

Wave 10 introduces a two-environment topology for NoorinALabs: `noorinalabs-stg` (CPX21, Ashburn) and `noorinalabs-prod` (CPX41, Ashburn). The existing `terraform/hetzner/` module provisions a single production VPS and stores state under a single key (`hetzner/terraform.tfstate`) in the `noorinalabs-terraform-state` Backblaze B2 bucket via the S3-compatible backend.

We need a layout that:

1. Provisions both envs from the same source of truth (shared resource shape).
2. Isolates state so a mistake in stg cannot corrupt prod state (and vice versa).
3. Allows env-specific inputs — server type, location, cloud-init secrets — without duplicating the resource definitions.
4. Supports targeted `plan`/`apply` per env (CI and local).
5. Is legible to downstream consumers — `deploy#83` (Cloudflare), `deploy#84` (promotion workflow), `deploy#87` (verify-deploy split) — who only need per-env outputs (IP, hostname, label).

## Options considered

### Option A — Terraform workspaces

Single root module; `terraform workspace new stg` / `prod`; `backend "s3"` key becomes `env:/stg/hetzner/terraform.tfstate` internally.

- **Pros:** minimal file churn, one place to edit resource shape.
- **Cons — why rejected:**
  - Shared backend config means a single B2 credential issue, provider lock, or plugin upgrade blocks both envs at once. "What happens when this fails?" answer: both envs go down as a plan target.
  - Per-env variable selection relies on `lookup(var.envs, terraform.workspace)` patterns which are error-prone — a typo silently falls through to a default, often the prod one.
  - Workspace name is implicit global state; a `terraform apply` without `terraform workspace select` runs against whichever env was last selected. This is the root cause of the "I thought I was in stg" incident class.
  - Destroys are scarier — `terraform destroy` in the wrong workspace kills prod with no structural guard.
  - CI pipelines must `workspace select` on every job; forgetting it is a silent cross-env apply.
  - Cloud-init secrets are per-env; workspaces don't cleanly map to per-env `.tfvars` files without wrapper scripts, which reintroduces the problem this was supposed to solve.

### Option B — `envs/{stg,prod}/` subdirectories calling a shared `modules/hetzner-vps/` module ← **chosen**

Each env is its own root module. Each has its own `backend.tf` with a distinct `key` (`hetzner/stg.tfstate` vs `hetzner/prod.tfstate`). Both env roots call the same child module, passing env-specific values.

- **Pros:**
  - State files are fully separate objects in B2. A corruption or drift in stg cannot touch prod.
  - `cd envs/stg && terraform apply` and `cd envs/prod && terraform apply` are structurally independent — working directory IS the selector, not an implicit workspace.
  - Destroy blast radius is bounded to one env by construction.
  - Per-env `.tfvars` files live next to their env; no indirection.
  - Child module has no backend block; it's reusable and testable.
  - CI jobs are simpler: `working-directory: terraform/hetzner/envs/${{ matrix.env }}`.
- **Cons:**
  - Slight duplication of wiring code in each `envs/*/main.tf`. Mitigated: wiring is ~30 lines calling the module with env-specific values; the resource definitions themselves stay in the shared module.
  - Two `backend.tf` files with nearly-identical backend config. Accepted — the difference (`key`) is the whole point.

### Option C — Single root with `for_each` over an envs map

One `hcloud_server` block using `for_each = var.envs`, all envs in a single state.

- **Pros:** no duplication at all.
- **Cons — why rejected:**
  - Worst blast radius of the three: state mishap hits both envs; `terraform destroy` requires `-target` gymnastics every time.
  - A `for_each` typo can recreate both servers (destroy/create) on a single merge.
  - Completely fails requirement (2) on state isolation.

## Decision

Adopt **Option B**: `envs/{stg,prod}/` subdirectories calling `modules/hetzner-vps/`, with per-env backend keys.

### Backend key convention

```hcl
# envs/stg/backend.tf
terraform {
  backend "s3" {
    bucket   = "noorinalabs-terraform-state"
    key      = "hetzner/stg.tfstate"
    region   = "us-east-005"
    endpoints = { s3 = "https://s3.us-east-005.backblazeb2.com" }
    # ...skip_* flags per Backblaze B2 S3 compatibility
  }
}

# envs/prod/backend.tf — identical except:
    key = "hetzner/prod.tfstate"
```

### State migration

The existing `hetzner/terraform.tfstate` becomes `hetzner/prod.tfstate`. Migration path (executed by Bereket or an SRE, not automated in this PR):

1. Back up `hetzner/terraform.tfstate` locally via `aws s3 cp ... --endpoint-url ...`.
2. Rename the B2 object: `aws s3 mv s3://noorinalabs-terraform-state/hetzner/terraform.tfstate s3://noorinalabs-terraform-state/hetzner/prod.tfstate --endpoint-url ...`.
3. `cd envs/prod && terraform init` — Terraform binds to the renamed key.
4. `terraform plan` must show zero changes. If it does not, the existing resources (which were named `noorinalabs-isnad-graph-prod`) will be replaced — **acceptable per issue #82**, because the owner has confirmed no data preservation is required and the hand-made `isnad-graph-prod` VPS (not in Terraform) is the current live box and will be decommissioned separately (`deploy#86`).
5. `cd envs/stg && terraform init && terraform apply` on the fresh stg state.

### Naming convention

- Server name: `noorinalabs-${var.env}` (e.g., `noorinalabs-stg`, `noorinalabs-prod`).
- Firewall: `noorinalabs-${var.env}-firewall`.
- SSH key: `noorinalabs-${var.env}-deploy`.
- Labels: `{ project = "noorinalabs", environment = var.env }`.

This pattern is documented in the PR Contract and is **authoritative for `deploy#83`/`#84`/`#87`** — they read these as inputs, not replicate them.

## Consequences

### Positive

- Strict state isolation — satisfies the "what happens when stg apply fails?" question: prod is untouched, full stop.
- Clearer working-directory-as-env convention; lower chance of "wrong env" errors.
- Per-env secrets live in per-env `.tfvars.example` files.
- CI matrix jobs (`env: [stg, prod]`) map directly to subdirectories.

### Negative / ongoing costs

- Adding a new env (e.g., `qa` in the future) requires copying `envs/prod/` and adjusting. Accepted — new envs are rare and the copy is ~5 files, all thin.
- Child module API changes require coordinated bumps in both env roots. Mitigated by the module version being checked in (no registry), so a single PR updates both wires.

### Failure modes explicitly considered

| Question | Answer |
|---|---|
| What happens when `terraform apply` fails on stg mid-run? | Only `hetzner/stg.tfstate` is partial. Prod state is untouched. Re-run `envs/stg` apply; it's idempotent. |
| What happens if only prod is targeted and stg is forgotten? | Each env is independently applicable. Nothing implicit links them — this is the intended contract. `deploy#84` promotion workflow treats each env as an independent target. |
| What happens if state drift appears between envs? | Drift shows in the env that drifted, during its own `terraform plan`. No cross-env leakage. |
| What happens if backend credentials rotate? | Same credentials are used for both envs (same B2 bucket, same AWS_*  env vars). Rotation is a single-step operation documented in the README. |
| What happens if the child module has a bug? | Caught in whichever env runs plan first (stg goes first by convention — see `deploy#84`). Prod is shielded by the promotion sequencing. |

## Follow-up issues

- `deploy#86` — decommission the hand-made `isnad-graph-prod` VPS.
- `deploy#83` — Cloudflare stg subdomain wiring; consumes the outputs defined in this ADR.
- `deploy#84` — promotion workflow; consumes the per-env SSH target hosts.
- `deploy#87` — verify-deploy split; consumes the per-env health endpoints.
