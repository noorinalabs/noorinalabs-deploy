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

# ---------------------------------------------------------------------------
# Legacy subdomain map (for `isnad-graph.noorinalabs.com` and any ad-hoc
# CNAMEs that should alias to the prod apex). Preserved untouched by #83 —
# retirement of `isnad-graph` is tracked in #156 (cutover follow-up).
# ---------------------------------------------------------------------------

variable "legacy_subdomains" {
  description = "Map of legacy subdomain names → proxied flag. All CNAME to the prod apex. Empty the map to drop a legacy name once its cutover is complete."
  type        = map(bool)
  default = {
    "isnad-graph" = false
  }
}
