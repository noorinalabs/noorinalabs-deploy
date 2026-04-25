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
# Per-env Hetzner VPS IPs — preferred source is terraform_remote_state
# (see main.tf data sources + locals). These vars are an override path for
# local applies that don't have hetzner B2 state access. Default empty means
# "read from remote state".
# ---------------------------------------------------------------------------

variable "prod_vps_ipv4_address" {
  description = "Public IPv4 override for the prod Hetzner VPS. Empty (default) reads from terraform_remote_state.hetzner_prod.outputs.server_ip."
  type        = string
  default     = ""
}

variable "prod_vps_ipv6_address" {
  description = "Public IPv6 override for the prod Hetzner VPS. Empty reads from remote state; if remote state also returns empty, the AAAA record is omitted."
  type        = string
  default     = ""
}

variable "stg_vps_ipv4_address" {
  description = "Public IPv4 override for the stg Hetzner VPS. Empty (default) reads from terraform_remote_state.hetzner_stg.outputs.server_ip."
  type        = string
  default     = ""
}

variable "stg_vps_ipv6_address" {
  description = "Public IPv6 override for the stg Hetzner VPS. Empty reads from remote state; if remote state also returns empty, the AAAA record is omitted."
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
