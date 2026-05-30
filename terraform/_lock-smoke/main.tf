# Lock-smoke root — ADR 0005 (#334) concurrency-group serialization proof.
#
# This root exists ONLY to exercise the GitHub Actions `concurrency:` group
# that protects real Terraform applies (see .github/workflows/terraform-lock-smoke.yml).
# It provisions NO real infrastructure: a single `null_resource` runs a local
# `sleep` so a same-key second run observably queues behind the first, and a
# different-key run observably parallelizes.
#
# Local backend on purpose — no B2 state, no credentials, no provider auth.
# Not wired into the terraform.yml validate/tflint/plan/apply matrices; it is
# only ever run by the dedicated lock-smoke workflow.

terraform {
  required_version = ">= 1.6"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

variable "hold_seconds" {
  description = "Seconds the apply holds, so a same-concurrency-key second run visibly queues behind the first."
  type        = number
  default     = 45
}

variable "run_label" {
  description = "Free-form label echoed into the apply log to correlate a run with its workflow dispatch (e.g. same-key-a / same-key-b / different-key)."
  type        = string
  default     = "unlabelled"

  validation {
    condition     = length(trimspace(var.run_label)) > 0
    error_message = "run_label must not be empty."
  }
}

resource "null_resource" "lock_smoke" {
  # Re-run on every apply so two dispatched runs both do real work and the
  # second cannot short-circuit as a no-op.
  triggers = {
    run_label = var.run_label
    timestamp = timestamp()
  }

  provisioner "local-exec" {
    command = "echo \"lock-smoke apply START label=${var.run_label} at $(date -u +%FT%TZ)\"; sleep ${var.hold_seconds}; echo \"lock-smoke apply END   label=${var.run_label} at $(date -u +%FT%TZ)\""
  }
}

output "run_label" {
  value = var.run_label
}
