variable "cloudflare_api_token" {
  description = "Cloudflare API token with Zone:DNS:Edit, Zone:Zone Settings:Edit, Zone:Zone:Read."
  type        = string
  sensitive   = true
}

variable "cloudflare_zone_id" {
  description = "Cloudflare Zone ID for noorinalabs.com."
  type        = string
}

variable "domain" {
  description = "Root domain name."
  type        = string
  default     = "noorinalabs.com"
}

# ---------------------------------------------------------------------------
# Per-env Hetzner VPS IPs — consumed from terraform/hetzner/envs/{prod,stg}
# outputs (`server_ip`, `server_ipv6`). Passed in at plan/apply time as
# -var flags in CI, or via terraform.tfvars locally.
# ---------------------------------------------------------------------------

variable "prod_vps_ipv4_address" {
  description = "Public IPv4 of the prod Hetzner VPS (from terraform/hetzner/envs/prod output server_ip)."
  type        = string
}

variable "prod_vps_ipv6_address" {
  description = "Public IPv6 of the prod Hetzner VPS (from terraform/hetzner/envs/prod output server_ipv6). Empty string disables the AAAA record."
  type        = string
  default     = ""
}

variable "stg_vps_ipv4_address" {
  description = "Public IPv4 of the stg Hetzner VPS (from terraform/hetzner/envs/stg output server_ip)."
  type        = string
}

variable "stg_vps_ipv6_address" {
  description = "Public IPv6 of the stg Hetzner VPS (from terraform/hetzner/envs/stg output server_ipv6). Empty string disables the AAAA record."
  type        = string
  default     = ""
}

# Legacy subdomains variable removed in #192 (drop-legacy-immediately per
# owner ruling 2026-05-02). isnad-graph.noorinalabs.com A/AAAA records are
# imported and then destroyed by this PR's apply; Caddy binding moved to
# isnad.{$BASE_DOMAIN} in the same PR.
