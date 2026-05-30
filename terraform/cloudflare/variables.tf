variable "cloudflare_api_token" {
  description = "Cloudflare API token with Zone:DNS:Edit, Zone:Zone Settings:Edit, Zone:Zone:Read."
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.cloudflare_api_token)) > 0
    error_message = "cloudflare_api_token must not be empty — set repo Actions secret CLOUDFLARE_API_TOKEN (wired as TF_VAR_cloudflare_api_token in the Plan/Apply (cloudflare) jobs)."
  }
}

variable "cloudflare_zone_id" {
  description = "Cloudflare Zone ID for noorinalabs.com."
  type        = string

  validation {
    condition     = length(trimspace(var.cloudflare_zone_id)) > 0
    error_message = "cloudflare_zone_id must not be empty — set repo Actions variable CLOUDFLARE_ZONE_ID (wired as TF_VAR_cloudflare_zone_id in the Plan/Apply (cloudflare) jobs)."
  }
}

# Defensive-TLD zone IDs — `.net` and `.org` are dark zones owned for
# squatter prevention. They carry no productive DNS records; the only
# Cloudflare-managed configuration is the canonical-domain 301 redirect
# ruleset defined in redirects.tf. Zone IDs are public-DNS metadata, not
# secrets — sourced from the Cloudflare dashboard (Overview → Zone ID).
variable "noorinalabs_net_zone_id" {
  description = "Cloudflare Zone ID for noorinalabs.net (canonical-domain redirect target zone)."
  type        = string

  validation {
    condition     = length(trimspace(var.noorinalabs_net_zone_id)) > 0
    error_message = "noorinalabs_net_zone_id must not be empty — set repo Actions variable CLOUDFLARE_NOORINALABS_NET_ZONE_ID (wired as TF_VAR_noorinalabs_net_zone_id in the Plan/Apply (cloudflare) jobs). An empty zone id produced the malformed /zones//rulesets call in the PR #344 partial prod apply."
  }
}

variable "noorinalabs_org_zone_id" {
  description = "Cloudflare Zone ID for noorinalabs.org (canonical-domain redirect target zone)."
  type        = string

  validation {
    condition     = length(trimspace(var.noorinalabs_org_zone_id)) > 0
    error_message = "noorinalabs_org_zone_id must not be empty — set repo Actions variable CLOUDFLARE_NOORINALABS_ORG_ZONE_ID (wired as TF_VAR_noorinalabs_org_zone_id in the Plan/Apply (cloudflare) jobs). An empty zone id produced the malformed /zones//rulesets call in the PR #344 partial prod apply."
  }
}

variable "domain" {
  description = "Root domain name."
  type        = string
  default     = "noorinalabs.com"
}

# Existing dynamic-redirect phase-entrypoint ruleset IDs for the defensive
# TLDs. Cloudflare allows exactly one entrypoint ruleset per phase per zone,
# and both `.net` and `.org` already have one — so the canonical-redirect
# rulesets in redirects.tf must be IMPORTED (adopt the existing entrypoint),
# not created (see #348). The `import {}` blocks in imports.tf reference these.
#
# These IDs are NOT stored anywhere — they are discovered at CI time from the
# Cloudflare API (`GET /zones/<zone_id>/rulesets` → the entry whose
# `phase == "http_request_dynamic_redirect"`) and threaded in as `TF_VAR_*`
# by the discovery step in .github/workflows/terraform.yml.
#
# Default "" so that `terraform validate -backend=false` (the CI `validate`
# job, which runs no discovery and passes no vars) still parses. An empty id
# is only a problem at PLAN/APPLY time — those jobs (plan-cloudflare,
# apply-cloudflare) run the discovery step first, so the value is non-empty
# there. Do not rely on this default anywhere a plan/apply runs.
variable "net_redirect_ruleset_id" {
  description = "Existing http_request_dynamic_redirect entrypoint ruleset ID for noorinalabs.net (CI-discovered; see imports.tf)."
  type        = string
  default     = ""
}

variable "org_redirect_ruleset_id" {
  description = "Existing http_request_dynamic_redirect entrypoint ruleset ID for noorinalabs.org (CI-discovered; see imports.tf)."
  type        = string
  default     = ""
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
