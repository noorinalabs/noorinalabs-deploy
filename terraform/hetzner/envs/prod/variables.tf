variable "hcloud_token" {
  description = "Hetzner Cloud API token."
  type        = string
  sensitive   = true
}

variable "ssh_public_key_path" {
  description = "Path to SSH public key file."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
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
