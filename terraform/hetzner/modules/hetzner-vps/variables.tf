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

variable "ssh_public_key_path" {
  description = "Path to SSH public key file uploaded to Hetzner and authorized for the deploy user."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
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
