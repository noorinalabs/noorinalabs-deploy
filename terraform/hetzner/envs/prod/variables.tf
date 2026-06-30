variable "hcloud_token" {
  description = "Hetzner Cloud API token."
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.hcloud_token)) > 0
    error_message = "hcloud_token must not be empty — set the HCLOUD_TOKEN secret in the 'production' environment (wired as TF_VAR_hcloud_token in the Plan/Apply (hetzner/prod) jobs)."
  }
}

variable "deploy_ssh_public_key_path" {
  description = "Path to the prod DEPLOY SSH pubkey (authorizes the `deploy` user). Private half is the prod-scoped CI DEPLOY_SSH_PRIVATE_KEY secret + workstation ~/.ssh/noorinalabs_prod_deploy. Default points at the canonical checked-in pubkey so cold-rebuild/CI provisions work without an operator-local path; override per ADR 0006 once the real prod deploy key is minted."
  type        = string
  default     = "../../modules/hetzner-vps/deploy.pub"
}

variable "root_ssh_public_key_path" {
  description = "Path to the prod ROOT SSH pubkey (authorizes the `root` user). Per ADR 0006 this is owner-workstation-only (~/.ssh/noorinalabs_prod_root) and its private half MUST NOT be in any GH secret. Default points at the checked-in placeholder root pubkey for CI provisioning; override with the real prod root pubkey path at apply time."
  type        = string
  default     = "../../modules/hetzner-vps/root.pub"
}

variable "ssh_source_ips" {
  description = "CIDR ranges allowed to SSH. Restrict to operator IPs or VPN in production."
  type        = list(string)
  default     = ["0.0.0.0/0", "::/0"]
}

variable "ghcr_auth_b64" {
  description = "Base64-encoded 'username:token' for GHCR Docker authentication on the prod VPS."
  type        = string
  sensitive   = true
  default     = ""
}

variable "user_postgres_password" {
  description = "Password for the prod user-service PostgreSQL database (min 16 chars when set)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "user_redis_password" {
  description = "Password for the prod user-service Redis (min 16 chars when set)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "user_service_jwt_secret" {
  description = "JWT signing secret for prod user-service (min 32 chars when set)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "test_user_email" {
  description = "Email of the prod NON-ADMIN QA test account (deploy#508). An identifier, not a secret; set via the `production` GH Environment variable TEST_USER_EMAIL. Empty disables the seed."
  type        = string
  default     = ""
}

variable "test_user_password" {
  description = "Password for the prod NON-ADMIN QA test account (deploy#508; min 16 chars when set in the module). Set via the `production` GH Environment secret TEST_USER_PASSWORD. Empty disables the seed."
  type        = string
  sensitive   = true
  default     = ""
}
