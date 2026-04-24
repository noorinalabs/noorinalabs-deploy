output "prod_hostnames" {
  description = "Prod hostname map — consumed by deploy#87 verify and the cutover issue #156. Keys are canonical service identifiers."
  value = {
    landing = cloudflare_record.prod_apex_a.hostname
    www     = cloudflare_record.www_cname.hostname
    isnad   = cloudflare_record.prod_isnad_cname.hostname
    auth    = cloudflare_record.prod_auth_cname.hostname
  }
}

output "stg_hostnames" {
  description = "Stg hostname map — consumed by deploy#87 verify and the stg deploy workflow (deploy#155)."
  value = {
    landing = cloudflare_record.stg_apex_a.hostname
    isnad   = cloudflare_record.stg_isnad_cname.hostname
    auth    = cloudflare_record.stg_auth_cname.hostname
  }
}

output "legacy_subdomain_hostnames" {
  description = "Legacy subdomains (e.g., isnad-graph.noorinalabs.com) preserved during cutover. Retired via #156."
  value       = { for k, v in cloudflare_record.legacy_subdomains : k => v.hostname }
}

output "ssl_mode" {
  description = "Current SSL/TLS mode for the zone."
  value       = cloudflare_zone_settings_override.ssl.settings[0].ssl
}
