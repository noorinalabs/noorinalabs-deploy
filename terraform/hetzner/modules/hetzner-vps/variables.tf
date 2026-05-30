variable "env" {
  description = "Environment identifier — stg or prod. Drives resource naming and labeling."
  type        = string

  validation {
    condition     = contains(["stg", "prod"], var.env)
    error_message = "env must be one of: stg, prod."
  }
}

variable "server_type" {
  description = "Hetzner Cloud server type (e.g., cpx21, cpx41)."
  type        = string

  validation {
    condition     = length(trimspace(var.server_type)) > 0
    error_message = "server_type must not be empty — set it in the module call from the env root (terraform/hetzner/envs/<env>/main.tf, e.g. server_type = \"cpx41\")."
  }
}

variable "location" {
  description = "Hetzner Cloud location (e.g., ash for Ashburn)."
  type        = string
  default     = "ash"
}

variable "image" {
  description = "Server image."
  type        = string
  default     = "ubuntu-24.04"
}

variable "deploy_ssh_public_key_path" {
  description = "Path to the per-env DEPLOY SSH public key, authorized for the `deploy` user via cloud-init. This is the key whose private half is the env-scoped CI `DEPLOY_SSH_PRIVATE_KEY` secret. Resolved relative to the env root module's working directory (envs pass their per-env path); the module-local default points at the checked-in canonical deploy pubkey for module-only `terraform validate` runs. See ADR 0006."
  type        = string
  default     = "./deploy.pub"
}

variable "root_ssh_public_key_path" {
  description = "Path to the per-env ROOT SSH public key, authorized for the `root` user via cloud-init. Per ADR 0006 this key is owner-workstation-only and its private half MUST NOT appear in any GH secret. Resolved relative to the env root module's working directory; the module-local default points at the checked-in placeholder root pubkey for module-only `terraform validate` runs (operators override per-env with their real root pubkey path)."
  type        = string
  default     = "./root.pub"
}

variable "ssh_source_ips" {
  description = "CIDR ranges allowed to SSH. Restrict to operator IPs or VPN in production."
  type        = list(string)
  default     = ["0.0.0.0/0", "::/0"]
}

variable "ghcr_auth_b64" {
  description = "Base64-encoded 'username:token' for GHCR Docker authentication on the VPS."
  type        = string
  sensitive   = true
  default     = ""
}

variable "user_postgres_password" {
  description = "Password for the user-service PostgreSQL database."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = var.user_postgres_password == "" || length(var.user_postgres_password) >= 16
    error_message = "user_postgres_password must be at least 16 characters when set."
  }
}

variable "user_redis_password" {
  description = "Password for the user-service Redis instance."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = var.user_redis_password == "" || length(var.user_redis_password) >= 16
    error_message = "user_redis_password must be at least 16 characters when set."
  }
}

variable "user_service_jwt_secret" {
  description = "JWT signing secret for user-service authentication."
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = var.user_service_jwt_secret == "" || length(var.user_service_jwt_secret) >= 32
    error_message = "user_service_jwt_secret must be at least 32 characters when set."
  }
}
