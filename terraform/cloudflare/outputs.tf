output "root_record_hostname" {
  description = "Hostname of the root A record"
  value       = cloudflare_record.root_a.hostname
}

output "subdomain_hostnames" {
  description = "Map of subdomain names to their full hostnames"
  value       = { for k, v in cloudflare_record.subdomains : k => v.hostname }
}

output "ssl_mode" {
  description = "Current SSL/TLS mode for the zone"
  value       = cloudflare_zone_settings_override.ssl.settings[0].ssl
}
