output "env" {
  description = "Environment identifier (stg or prod) — echoes var.env for downstream consumers."
  value       = var.env
}

output "server_name" {
  description = "VPS instance name (e.g., noorinalabs-stg, noorinalabs-prod). Authoritative host identifier for Cloudflare / promotion / verify consumers."
  value       = hcloud_server.app.name
}

output "server_ip" {
  description = "Public IPv4 address of the VPS. DNS A-record target for deploy#83."
  value       = hcloud_server.app.ipv4_address
}

output "server_ipv6" {
  description = "Public IPv6 address of the VPS. Optional AAAA-record target."
  value       = hcloud_server.app.ipv6_address
}

output "server_status" {
  description = "Current server status."
  value       = hcloud_server.app.status
}

output "ssh_target" {
  description = "deploy@<ipv4> — the SSH target the promotion workflow (deploy#84) connects to."
  value       = "deploy@${hcloud_server.app.ipv4_address}"
}

output "labels" {
  description = "Hetzner labels applied to all resources — { project, environment }. Used by downstream tooling to discover per-env resources."
  value       = local.labels
}
