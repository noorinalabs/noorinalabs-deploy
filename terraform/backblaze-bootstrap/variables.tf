# Master B2 key (account-wide writeBuckets + writeKeys). Per ADR 0004
# Decision D this key is operator-workstation-only and MUST NOT exist in CI.
# Same provider-auth pattern as terraform/backblaze/.
variable "b2_application_key_id" {
  description = "B2 master application key ID (account-wide writeBuckets+writeKeys; operator workstation only — MUST NOT be set in CI per ADR 0004 Decision D)."
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.b2_application_key_id)) > 0
    error_message = "b2_application_key_id must not be empty — export the master key id (operator workstation only; never a CI secret per ADR 0004 Decision D)."
  }
}

variable "b2_application_key" {
  description = "B2 master application key secret (account-wide; operator workstation only — MUST NOT be set in CI per ADR 0004 Decision D)."
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.b2_application_key)) > 0
    error_message = "b2_application_key must not be empty — export the master key secret (operator workstation only; never a CI secret per ADR 0004 Decision D)."
  }
}

variable "state_bucket_name" {
  description = "Name of the Terraform state bucket this module manages. Matches the bucket every other root's `backend \"s3\"` block references."
  type        = string
  default     = "noorinalabs-terraform-state"

  validation {
    condition     = length(trimspace(var.state_bucket_name)) > 0
    error_message = "state_bucket_name must not be empty."
  }
}
