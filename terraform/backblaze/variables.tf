variable "b2_application_key_id" {
  description = "Backblaze B2 master application key ID (for provider auth; create in B2 console)"
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.b2_application_key_id)) > 0
    error_message = "b2_application_key_id must not be empty — set repo Actions secret B2_MASTER_KEY_ID (wired as TF_VAR_b2_application_key_id in the Plan/Apply (backblaze) jobs)."
  }
}

variable "b2_application_key" {
  description = "Backblaze B2 master application key (for provider auth; create in B2 console)"
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.b2_application_key)) > 0
    error_message = "b2_application_key must not be empty — set repo Actions secret B2_MASTER_APP_KEY (wired as TF_VAR_b2_application_key in the Plan/Apply (backblaze) jobs)."
  }
}

variable "bucket_name" {
  description = "Name of the pipeline bucket"
  type        = string
  default     = "noorinalabs-pipeline"
}

variable "lifecycle_days_unfinished_uploads" {
  description = "Days before incomplete multipart uploads are abandoned"
  type        = number
  default     = 7
}

variable "lifecycle_days_hide_old_versions" {
  description = "Days after which non-current object versions are hidden"
  type        = number
  default     = 30
}

variable "lifecycle_days_delete_hidden" {
  description = "Days after hide before hidden versions are deleted permanently"
  type        = number
  default     = 90
}

variable "cors_allowed_origins" {
  description = "Allowed origins for CORS (pipeline workers usually do not need CORS; keep empty for server-side-only access)"
  type        = list(string)
  default     = []
}
