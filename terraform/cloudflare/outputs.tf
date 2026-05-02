output "prod_hostnames" {
  description = "Prod hostname map — consumed by deploy#87 verify and the cutover issue #156. Keys are canonical service identifiers."
  value = {
    landing = cloudflare_record.prod_apex_a.hostname
    www     = cloudflare_record.www_a.hostname
    isnad   = cloudflare_record.prod_isnad_cname.hostname
    users   = cloudflare_record.prod_users_cname.hostname
  }
}

output "stg_hostnames" {
  description = "Stg hostname map — consumed by deploy#87 verify and the stg deploy workflow (deploy#155)."
  value = {
    landing = cloudflare_record.stg_apex_a.hostname
    isnad   = cloudflare_record.stg_isnad_cname.hostname
    users   = cloudflare_record.stg_users_cname.hostname
  }
}

# legacy_subdomain_hostnames output removed in #192 — legacy isnad-graph
# records are destroyed by this PR's apply (drop-legacy-immediately).

output "ssl_mode" {
  description = "Current SSL/TLS mode for the zone."
  value       = cloudflare_zone_settings_override.ssl.settings[0].ssl
}
