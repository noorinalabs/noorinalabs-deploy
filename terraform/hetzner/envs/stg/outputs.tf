output "env" {
  description = "Environment identifier."
  value       = module.vps.env
}

output "server_name" {
  description = "VPS instance name — authoritative for downstream DNS/promotion/verify consumers."
  value       = module.vps.server_name
}

output "server_ip" {
  description = "Public IPv4 address of the stg VPS."
  value       = module.vps.server_ip
}

output "server_ipv6" {
  description = "Public IPv6 address of the stg VPS."
  value       = module.vps.server_ipv6
}

output "ssh_target" {
  description = "deploy@<ipv4> — SSH target for the promotion workflow (deploy#84)."
  value       = module.vps.ssh_target
}

output "labels" {
  description = "Hetzner labels applied to stg resources."
  value       = module.vps.labels
}

output "server_status" {
  description = "Current stg server status."
  value       = module.vps.server_status
}
