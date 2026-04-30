locals {
  name_prefix = "noorinalabs-${var.env}"
  labels = {
    project     = "noorinalabs"
    environment = var.env
  }
}

resource "hcloud_ssh_key" "deploy" {
  name       = "${local.name_prefix}-deploy"
  public_key = sensitive(chomp(file(var.ssh_public_key_path)))
  labels     = local.labels
}

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

  ssh_keys     = [hcloud_ssh_key.deploy.id]
  firewall_ids = [hcloud_firewall.web.id]

  user_data = templatefile("${path.module}/cloud-init.yaml.tpl", {
    ssh_public_key          = sensitive(chomp(file(var.ssh_public_key_path)))
    ghcr_auth_b64           = var.ghcr_auth_b64
    user_postgres_password  = var.user_postgres_password
    user_redis_password     = var.user_redis_password
    user_service_jwt_secret = var.user_service_jwt_secret
  })

  labels = local.labels
}
