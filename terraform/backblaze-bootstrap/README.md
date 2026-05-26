# `terraform/backblaze-bootstrap/` — state-bucket IaC bootstrap

Implements [ADR 0004](../../docs/adr/0004-b2-state-bucket-and-key-management.md)
**Decision A2** (deploy#331). This root module brings the
`noorinalabs-terraform-state` B2 bucket — the bucket every other Terraform root
stores its state in — under IaC management.

## What it manages

- `b2_bucket.terraform_state` — the state bucket, with the lifecycle rule
  (`days_from_hiding_to_deleting = 7`) transcribed verbatim from the
  spec-of-record runbook [`docs/runbooks/state-bucket-lifecycle.md`](../../docs/runbooks/state-bucket-lifecycle.md).
- `b2_application_key.tf_state_writer` — the bucket-scoped read/write key
  (ADR 0004 Decision B capability set).

## Why a `local` backend (not B2)

Every other root in `terraform/` uses `backend "s3"` pointed at this very
bucket. This module **creates** that bucket, so it cannot store its own state
inside it — the bounded chicken-and-egg ADR 0004 Decision A2 ratified. State
lives in a local `terraform.tfstate` next to this README.

**`terraform.tfstate` handling:** do NOT commit it by default — it contains the
`tf_state_writer` application key secret. Commit the state only if the team has
**pre-arranged** an encrypted-at-rest path (`git-crypt`/`sops`), per ADR 0004
Decision A2's two accepted options. Absent that arrangement, treat this as a
re-`import`-on-each-run module (the bucket already exists, so a fresh run
imports it — see below); the local state is a working artifact, not a
committed source of truth. `.gitignore` already excludes `*.tfstate` repo-wide.

## How to run (operator workstation only — NOT CI)

Per ADR 0004 Decision D, the **master key** this module needs (account-wide
`writeBuckets`+`writeKeys`) MUST NOT exist in CI. Run from an operator
workstation with the master key exported:

```bash
cd terraform/backblaze-bootstrap
export TF_VAR_b2_application_key_id="<master key id>"
export TF_VAR_b2_application_key="<master key secret>"

terraform init

# FIRST RUN ONLY — the bucket already exists (console-created during repo
# bootstrap), so import it before the first apply or apply will fail trying to
# create a bucket that's already there:
terraform import b2_bucket.terraform_state noorinalabs-terraform-state

terraform plan    # expect: in-place reconciliation of lifecycle rule, +1 key
terraform apply
```

Cadence: this is a **once-per-DR-event / once-per-rotation** module, not a
per-PR target. It is not wired into `.github/workflows/terraform.yml`.

## Validation without credentials

`terraform validate` runs without a B2 account and is the CI-checkable gate.
`terraform plan`/`apply` need the live master key and are documented as
untested-without-creds (ADR 0004 acceptance; `feedback_runtime_gate_scoping`).

## Related

- Annual key-rotation procedure: [`docs/runbooks/state-key-annual-rotation.md`](../../docs/runbooks/state-key-annual-rotation.md)
- Lifecycle spec-of-record + DR fallback: [`docs/runbooks/state-bucket-lifecycle.md`](../../docs/runbooks/state-bucket-lifecycle.md)
- Strategy: [ADR 0004](../../docs/adr/0004-b2-state-bucket-and-key-management.md)
