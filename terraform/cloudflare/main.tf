provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

# ---------------------------------------------------------------------------
# Per-env Hetzner VPS IPs — read from the hetzner env-root tfstates instead of
# input variables. Eliminates the manual var-passing footgun (cloudflare apply
# would otherwise drift whenever a hetzner apply changed an IP). The CI apply
# job orders `apply-cloudflare needs: apply` so the hetzner state we read here
# is always freshly converged.
#
# Backend config mirrors `terraform/hetzner/envs/{prod,stg}/versions.tf` —
# same B2 bucket, same S3-compat skip flags. Reader auth uses the same
# `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars (mapped from
# `TF_STATE_B2_KEY_ID` / `TF_STATE_B2_APP_KEY`) that the cloudflare module's
# own backend already needs.
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

locals {
  prod_vps_ipv4 = data.terraform_remote_state.hetzner_prod.outputs.server_ip
  prod_vps_ipv6 = data.terraform_remote_state.hetzner_prod.outputs.server_ipv6
  stg_vps_ipv4  = data.terraform_remote_state.hetzner_stg.outputs.server_ip
  stg_vps_ipv6  = data.terraform_remote_state.hetzner_stg.outputs.server_ipv6
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
# PROD records — point at the prod Hetzner VPS. Currently the hand-made
# 1box-prod (87.99.134.161 / 2a01:4ff:f0:be57::1); cuts over to the new
# TF-managed noorinalabs-prod via deploy#86. All proxied through Cloudflare
# for DDoS / WAF / edge cache (per owner ruling 2026-05-02). ssl=strict
# (set at zone level above) requires origin Caddy to present a valid LE
# cert — works through the proxy via ACME HTTP-01 path-passthrough.
# ===========================================================================

# Root apex — noorinalabs.com (proxied A + AAAA to prod origin).
# `name = var.domain` (FQDN) instead of `"@"` — Cloudflare's API normalizes
# `@` to the FQDN on read, so using FQDN here keeps the diff clean post-import.
resource "cloudflare_record" "prod_apex_a" {
  zone_id = var.cloudflare_zone_id
  name    = var.domain
  content = local.prod_vps_ipv4
  type    = "A"
  ttl     = 1
  proxied = true
}

resource "cloudflare_record" "prod_apex_aaaa" {
  count   = local.prod_vps_ipv6 == "" ? 0 : 1
  zone_id = var.cloudflare_zone_id
  name    = var.domain
  content = local.prod_vps_ipv6
  type    = "AAAA"
  ttl     = 1
  proxied = true
}

# www.noorinalabs.com — A/AAAA mirrors of apex (matches live; Caddy redirects
# to apex). Was a CNAME in earlier drafts; A/AAAA matches the hand-managed
# state in production and is the shape Cloudflare uses when imported.
resource "cloudflare_record" "www_a" {
  zone_id = var.cloudflare_zone_id
  name    = "www"
  content = local.prod_vps_ipv4
  type    = "A"
  ttl     = 1
  proxied = true
}

resource "cloudflare_record" "www_aaaa" {
  count   = local.prod_vps_ipv6 == "" ? 0 : 1
  zone_id = var.cloudflare_zone_id
  name    = "www"
  content = local.prod_vps_ipv6
  type    = "AAAA"
  ttl     = 1
  proxied = true
}

# isnad.noorinalabs.com → apex (isnad-graph app in prod, new-shaped name).
# Replaces the legacy isnad-graph.noorinalabs.com — Caddy binding moved to
# isnad.{$BASE_DOMAIN} in this same PR; legacy DNS records destroyed by
# this apply (drop-legacy-immediately per owner choice 2026-05-02).
resource "cloudflare_record" "prod_isnad_cname" {
  zone_id = var.cloudflare_zone_id
  name    = "isnad"
  content = var.domain
  type    = "CNAME"
  ttl     = 1
  proxied = true
}

# users.noorinalabs.com → apex (user-service in prod).
# Per main#212 Q2 ruling 2026-04-25: hostname is `users.*` (matches the
# noorinalabs-user-service repo and reflects combined auth + account-mgmt scope).
resource "cloudflare_record" "prod_users_cname" {
  zone_id = var.cloudflare_zone_id
  name    = "users"
  content = var.domain
  type    = "CNAME"
  ttl     = 1
  proxied = true
}

# ===========================================================================
# STG records — point at noorinalabs-stg (TF-managed Hetzner VPS).
#
# All stg records are gray-cloud (proxied=false). Cloudflare Universal SSL
# only covers `*.noorinalabs.com` (one level deep), so 3rd-level subdomains
# like `isnad.stg.noorinalabs.com` get a TLS handshake failure at the CF
# edge. Until/unless we enable Cloudflare Advanced Certificate Manager (paid,
# wildcard at any depth), stg lives outside the CF proxy. Origin Caddy has
# valid LE certs for the stg hostnames and serves directly. Stg loses the
# CF DDoS / WAF / cache benefits — acceptable for non-prod. Tracked as a
# tech-debt followup if owner ever wants to revisit ACM.
# ===========================================================================

resource "cloudflare_record" "stg_apex_a" {
  zone_id = var.cloudflare_zone_id
  name    = "stg"
  content = local.stg_vps_ipv4
  type    = "A"
  ttl     = 1
  proxied = false
}

resource "cloudflare_record" "stg_apex_aaaa" {
  count   = local.stg_vps_ipv6 == "" ? 0 : 1
  zone_id = var.cloudflare_zone_id
  name    = "stg"
  content = local.stg_vps_ipv6
  type    = "AAAA"
  ttl     = 1
  proxied = false
}

resource "cloudflare_record" "stg_isnad_cname" {
  zone_id = var.cloudflare_zone_id
  name    = "isnad.stg"
  content = "stg.${var.domain}"
  type    = "CNAME"
  ttl     = 1
  proxied = false
}

resource "cloudflare_record" "stg_users_cname" {
  zone_id = var.cloudflare_zone_id
  name    = "users.stg"
  content = "stg.${var.domain}"
  type    = "CNAME"
  ttl     = 1
  proxied = false
}
