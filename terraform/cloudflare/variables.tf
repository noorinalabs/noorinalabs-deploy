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

# Per-env Hetzner VPS IPs are read from the hetzner env-root tfstates via
# `data "terraform_remote_state"` in main.tf — no input vars. Eliminates
# the manual var-passing footgun where cloudflare DNS could drift from a
# hetzner IP change. See main.tf locals block.
#
# Legacy subdomains variable removed in #192 (drop-legacy-immediately per
# owner ruling 2026-05-02). isnad-graph.noorinalabs.com A/AAAA records are
# imported and then destroyed by this PR's apply; Caddy binding moved to
# isnad.{$BASE_DOMAIN} in the same PR.
