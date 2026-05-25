variable "hcloud_token" {
  description = "Hetzner Cloud API token."
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.hcloud_token)) > 0
    error_message = "hcloud_token must not be empty — set the HCLOUD_TOKEN secret in the 'staging' environment (wired as TF_VAR_hcloud_token in the Plan/Apply (hetzner/stg) jobs)."
  }
}

variable "deploy_ssh_public_key_path" {
  description = "Path to the stg DEPLOY SSH pubkey (authorizes the `deploy` user). Private half is the stg-scoped CI DEPLOY_SSH_PRIVATE_KEY secret + workstation ~/.ssh/noorinalabs_stg_deploy. Default points at the canonical checked-in pubkey so cold-rebuild/CI provisions work without an operator-local path; override per ADR 0006 once the real stg deploy key is minted."
  type        = string
  default     = "../../modules/hetzner-vps/deploy.pub"
}

variable "root_ssh_public_key_path" {
  description = "Path to the stg ROOT SSH pubkey (authorizes the `root` user). Per ADR 0006 this is owner-workstation-only (~/.ssh/noorinalabs_stg_root) and its private half MUST NOT be in any GH secret. Default points at the checked-in placeholder root pubkey for CI provisioning; override with the real stg root pubkey path at apply time."
  type        = string
  default     = "../../modules/hetzner-vps/root.pub"
}

variable "ssh_source_ips" {
  description = "CIDR ranges allowed to SSH. Restrict to operator IPs or VPN in stg too."
  type        = list(string)
  default     = ["0.0.0.0/0", "::/0"]
}

variable "ghcr_auth_b64" {
  description = "Base64-encoded 'username:token' for GHCR Docker authentication on the stg VPS."
  type        = string
  sensitive   = true
  default     = ""
}

variable "user_postgres_password" {
  description = "Password for the stg user-service PostgreSQL database (min 16 chars when set)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "user_redis_password" {
  description = "Password for the stg user-service Redis (min 16 chars when set)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "user_service_jwt_secret" {
  description = "JWT signing secret for stg user-service (min 32 chars when set)."
  type        = string
  sensitive   = true
  default     = ""
}
