locals {
  name_prefix = "noorinalabs-${var.env}"
  labels = {
    project     = "noorinalabs"
    environment = var.env
  }
}

# hcloud_ssh_key.deploy was removed in #222 — Hetzner enforces SSH-key content
# uniqueness across the project, so per-env resources sharing one canonical
# pubkey hit `uniqueness_error 409`. cloud-init's user_data is now the sole
# path for injecting authorized_keys (writes both /home/deploy/.ssh/authorized_keys
# and /root/.ssh/authorized_keys with var.ssh_public_key_path content).

resource "hcloud_firewall" "web" {
  name   = "${local.name_prefix}-firewall"
  labels = local.labels

  # PRODUCTION: restrict ssh_source_ips to your operator IPs or VPN CIDR.
  # The default (0.0.0.0/0) is intentionally open for initial setup only.
  rule {
    description = "Allow SSH"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = var.ssh_source_ips
  }

  rule {
    description = "Allow HTTP"
    direction   = "in"
    protocol    = "tcp"
    port        = "80"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  rule {
    description = "Allow HTTPS"
    direction   = "in"
    protocol    = "tcp"
    port        = "443"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }
}

resource "hcloud_server" "app" {
  name        = local.name_prefix
  server_type = var.server_type
  location    = var.location
  image       = var.image

  firewall_ids = [hcloud_firewall.web.id]

  user_data = templatefile("${path.module}/cloud-init.yaml.tpl", {
    ssh_public_key          = sensitive(chomp(file(var.ssh_public_key_path)))
    ghcr_auth_b64           = var.ghcr_auth_b64
    user_postgres_password  = var.user_postgres_password
    user_redis_password     = var.user_redis_password
    user_service_jwt_secret = var.user_service_jwt_secret
  })

  labels = local.labels

  # ssh_keys + user_data are creation-time-only on Hetzner. The ssh_keys arg
  # was removed in #222 (cloud-init handles authorized_keys for both root and
  # deploy users via user_data — see cloud-init.yaml.tpl). The ignore_changes
  # entry is retained so the existing live-state ssh_keys reference (a now-
  # deleted hcloud_ssh_key.deploy id from #217's apply) doesn't cause
  # spurious reconciliation. user_data is ignored so cloud-init template
  # edits don't trigger destructive server replace on already-provisioned VPSes.
  #
  # IMPORTANT: changes to cloud-init.yaml.tpl are silently skipped on existing
  # servers. If a template change represents a baseline shift that must reach
  # live boxes (e.g., new SSH key, new auditd rule), use the taint/replace
  # procedure documented in docs/runbooks/cloud-init-template-changes.md.
  lifecycle {
    ignore_changes = [ssh_keys, user_data]
  }
}
