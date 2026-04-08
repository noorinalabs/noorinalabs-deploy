provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

# ---------------------------------------------------------------------------
# SSL/TLS configuration
# Full (Strict) — Cloudflare verifies the origin certificate.
# Caddy on the VPS provides a valid Let's Encrypt certificate, so strict works.
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

# ---------------------------------------------------------------------------
# Root domain A record — points directly to VPS IP (DNS only, not proxied)
# DNS-only because Caddy handles TLS termination and we don't need
# Cloudflare's proxy layer in front of it.
# ---------------------------------------------------------------------------
resource "cloudflare_record" "root_a" {
  zone_id = var.cloudflare_zone_id
  name    = "@"
  content = var.vps_ipv4_address
  type    = "A"
  ttl     = 1 # Auto TTL
  proxied = false
}

# ---------------------------------------------------------------------------
# www CNAME — redirects to root domain
# ---------------------------------------------------------------------------
resource "cloudflare_record" "www" {
  zone_id = var.cloudflare_zone_id
  name    = "www"
  content = var.domain
  type    = "CNAME"
  ttl     = 1
  proxied = false
}

# ---------------------------------------------------------------------------
# Subdomain CNAME records — each points to the root domain
# ---------------------------------------------------------------------------
resource "cloudflare_record" "subdomains" {
  for_each = var.subdomains

  zone_id = var.cloudflare_zone_id
  name    = each.key
  content = var.domain
  type    = "CNAME"
  ttl     = 1
  proxied = each.value
}
