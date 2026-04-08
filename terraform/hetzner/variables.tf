variable "hcloud_token" {
  description = "Hetzner Cloud API token"
  type        = string
  sensitive   = true
}

variable "ssh_public_key_path" {
  description = "Path to SSH public key file"
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "server_type" {
  description = "Hetzner Cloud server type"
  type        = string
  default     = "cpx41"
}

variable "server_name" {
  description = "Name for the VPS instance"
  type        = string
  default     = "noorinalabs-isnad-graph-prod"
}

variable "location" {
  description = "Hetzner Cloud location"
  type        = string
  default     = "ash"
}

variable "ssh_source_ips" {
  description = "CIDR ranges allowed to SSH. Restrict to operator IPs or VPN in production."
  type        = list(string)
  default     = ["0.0.0.0/0", "::/0"]
}

variable "ghcr_auth_b64" {
  description = "Base64-encoded 'username:token' for GHCR Docker authentication"
  type        = string
  sensitive   = true
  default     = ""
}

# ---------------------------------------------------------------------------
# User Service variables
# ---------------------------------------------------------------------------

variable "user_postgres_password" {
  description = "Password for the user-service PostgreSQL database"
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = var.user_postgres_password == "" || length(var.user_postgres_password) >= 16
    error_message = "user_postgres_password must be at least 16 characters when set."
  }
}

variable "user_redis_password" {
  description = "Password for the user-service Redis instance"
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = var.user_redis_password == "" || length(var.user_redis_password) >= 16
    error_message = "user_redis_password must be at least 16 characters when set."
  }
}

variable "user_service_jwt_secret" {
  description = "JWT signing secret for user-service authentication"
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = var.user_service_jwt_secret == "" || length(var.user_service_jwt_secret) >= 32
    error_message = "user_service_jwt_secret must be at least 32 characters when set."
  }
}
