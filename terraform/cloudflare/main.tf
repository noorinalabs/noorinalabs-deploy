provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

# ---------------------------------------------------------------------------
# Hetzner state — read VPS IPs from terraform/hetzner/envs/{prod,stg} outputs.
# Same B2 backend config as terraform/hetzner/envs/{prod,stg}/backend.tf.
# Requires AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (B2 keys) at init time.
# ---------------------------------------------------------------------------
data "terraform_remote_state" "hetzner_prod" {
  backend = "s3"
  config = {
    bucket = "noorinalabs-terraform-state"
    key    = "hetzner/prod.tfstate"
    region = "us-east-005"
    endpoints = {
      s3 = "https://s3.us-east-005.backblazeb2.com"
    }
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
  }
}

data "terraform_remote_state" "hetzner_stg" {
  backend = "s3"
  config = {
    bucket = "noorinalabs-terraform-state"
    key    = "hetzner/stg.tfstate"
    region = "us-east-005"
    endpoints = {
      s3 = "https://s3.us-east-005.backblazeb2.com"
    }
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
  }
}

# Prefer the explicit var override when set; otherwise read from remote state.
# Lets `terraform.tfvars` short-circuit remote_state for local-only applies.
locals {
  prod_vps_ipv4 = var.prod_vps_ipv4_address != "" ? var.prod_vps_ipv4_address : data.terraform_remote_state.hetzner_prod.outputs.server_ip
  prod_vps_ipv6 = var.prod_vps_ipv6_address != "" ? var.prod_vps_ipv6_address : try(data.terraform_remote_state.hetzner_prod.outputs.server_ipv6, "")
  stg_vps_ipv4  = var.stg_vps_ipv4_address != "" ? var.stg_vps_ipv4_address : data.terraform_remote_state.hetzner_stg.outputs.server_ip
  stg_vps_ipv6  = var.stg_vps_ipv6_address != "" ? var.stg_vps_ipv6_address : try(data.terraform_remote_state.hetzner_stg.outputs.server_ipv6, "")
}

# ---------------------------------------------------------------------------
# SSL/TLS configuration — applies to every record in the zone.
# Full (Strict) — Cloudflare verifies the origin certificate.
# Caddy on each VPS provides a valid Let's Encrypt cert, so strict works.
# ---------------------------------------------------------------------------
resource "cloudflare_zone_settings_override" "ssl" {
  zone_id = var.cloudflare_zone_id

  settings {
    ssl                      = "strict"
    always_use_https         = "on"
    min_tls_version          = "1.2"
    automatic_https_rewrites = "on"
  }
}

# ===========================================================================
# PROD records — point at `noorinalabs-prod` (VPS provisioned by
# terraform/hetzner/envs/prod, see #82).
# ===========================================================================

# Root apex — noorinalabs.com → prod VPS IPv4 (DNS only, not proxied).
resource "cloudflare_record" "prod_apex_a" {
  zone_id = var.cloudflare_zone_id
  name    = "@"
  content = local.prod_vps_ipv4
  type    = "A"
  ttl     = 1
  proxied = false
}

# Root apex IPv6 — optional. `prod_vps_ipv6_address = ""` disables the record.
resource "cloudflare_record" "prod_apex_aaaa" {
  count   = local.prod_vps_ipv6 == "" ? 0 : 1
  zone_id = var.cloudflare_zone_id
  name    = "@"
  content = local.prod_vps_ipv6
  type    = "AAAA"
  ttl     = 1
  proxied = false
}

# www → apex redirect (Caddy does the actual redirect; DNS just resolves).
resource "cloudflare_record" "www_cname" {
  zone_id = var.cloudflare_zone_id
  name    = "www"
  content = var.domain
  type    = "CNAME"
  ttl     = 1
  proxied = false
}

# isnad.noorinalabs.com → prod apex (isnad-graph app in prod).
resource "cloudflare_record" "prod_isnad_cname" {
  zone_id = var.cloudflare_zone_id
  name    = "isnad"
  content = var.domain
  type    = "CNAME"
  ttl     = 1
  proxied = false
}

# users.noorinalabs.com → prod apex (user-service in prod).
# Per main#212 Q2 ruling 2026-04-25: hostname is `users.*` (matches the
# noorinalabs-user-service repo and reflects combined auth + account-mgmt scope).
resource "cloudflare_record" "prod_users_cname" {
  zone_id = var.cloudflare_zone_id
  name    = "users"
  content = var.domain
  type    = "CNAME"
  ttl     = 1
  proxied = false
}

# Legacy subdomains (isnad-graph.noorinalabs.com currently) — CNAME to apex.
# Preserved by #83 to avoid traffic disruption; retired in #156 cutover.
resource "cloudflare_record" "legacy_subdomains" {
  for_each = var.legacy_subdomains

  zone_id = var.cloudflare_zone_id
  name    = each.key
  content = var.domain
  type    = "CNAME"
  ttl     = 1
  proxied = each.value
}

# ===========================================================================
# STG records — point at `noorinalabs-stg` (VPS provisioned by
# terraform/hetzner/envs/stg, see #82).
# ===========================================================================

# Stg subdomain apex — stg.noorinalabs.com → stg VPS IPv4.
resource "cloudflare_record" "stg_apex_a" {
  zone_id = var.cloudflare_zone_id
  name    = "stg"
  content = local.stg_vps_ipv4
  type    = "A"
  ttl     = 1
  proxied = false
}

# Stg subdomain apex IPv6 — optional.
resource "cloudflare_record" "stg_apex_aaaa" {
  count   = local.stg_vps_ipv6 == "" ? 0 : 1
  zone_id = var.cloudflare_zone_id
  name    = "stg"
  content = local.stg_vps_ipv6
  type    = "AAAA"
  ttl     = 1
  proxied = false
}

# isnad.stg.noorinalabs.com → stg subdomain apex (isnad-graph app in stg).
resource "cloudflare_record" "stg_isnad_cname" {
  zone_id = var.cloudflare_zone_id
  name    = "isnad.stg"
  content = "stg.${var.domain}"
  type    = "CNAME"
  ttl     = 1
  proxied = false
}

# users.stg.noorinalabs.com → stg subdomain apex (user-service in stg).
resource "cloudflare_record" "stg_users_cname" {
  zone_id = var.cloudflare_zone_id
  name    = "users.stg"
  content = "stg.${var.domain}"
  type    = "CNAME"
  ttl     = 1
  proxied = false
}
