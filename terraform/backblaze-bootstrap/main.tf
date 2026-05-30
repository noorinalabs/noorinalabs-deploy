provider "b2" {
  application_key_id = var.b2_application_key_id
  application_key    = var.b2_application_key
}

# The Terraform state bucket itself — the load-bearing root of every TF apply
# in this repo (holds hetzner/{stg,prod}.tfstate, cloudflare/terraform.tfstate,
# backblaze/terraform.tfstate). Created out-of-band in the B2 console during
# initial bootstrap; this resource brings it under IaC management per ADR 0004
# Decision A2. On the first real run against the live account it MUST be
# `terraform import`ed (it already exists) — see README.md + the annual-rotation
# runbook; a plan-without-import would propose creating a bucket that's already
# there and fail at apply.
resource "b2_bucket" "terraform_state" {
  bucket_name = var.state_bucket_name
  bucket_type = "allPrivate"

  # Lifecycle rule transcribed VERBATIM from the spec-of-record runbook
  # (docs/runbooks/state-bucket-lifecycle.md, deploy#194/#326). Keep these in
  # sync: that runbook is the spec, this is the implementation.
  #   - days_from_hiding_to_deleting = 7: a superseded (hidden) state version is
  #     purged 7 days after it's hidden — bounds plaintext-history accumulation
  #     while preserving a one-on-call-rotation recovery window.
  #   - days_from_uploading_to_hiding intentionally omitted (null): never
  #     auto-hide by upload age; the current head of every *.tfstate key stays
  #     accessible forever — only superseded versions age out.
  lifecycle_rules {
    file_name_prefix             = ""
    days_from_hiding_to_deleting = 7
  }
}

# State-bucket-scoped read/write key (ADR 0004 Decision B capability set:
# listBuckets,listFiles,readFiles,writeFiles,deleteFiles — bucket-scoped, no
# key-management capability). This is the IaC-declared peer of the per-operator
# `noorinalabs-tfstate-{handle}` and the CI `TF_STATE_B2_*` keys; minting/
# rotating those remains the operator/runbook path (Decision B3 + the annual
# rotation runbook). Declared here so the canonical writer key's scope is a
# reviewable artifact.
resource "b2_application_key" "tf_state_writer" {
  key_name  = "noorinalabs-tfstate-writer"
  bucket_id = b2_bucket.terraform_state.bucket_id
  capabilities = [
    "listBuckets",
    "listFiles",
    "readFiles",
    "writeFiles",
    "deleteFiles",
  ]
}
