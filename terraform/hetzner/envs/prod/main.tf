provider "hcloud" {
  token = var.hcloud_token
}

module "vps" {
  source = "../../modules/hetzner-vps"

  env         = "prod"
  server_type = "cpx41"
  location    = "ash"

  deploy_ssh_public_key_path = var.deploy_ssh_public_key_path
  root_ssh_public_key_path   = var.root_ssh_public_key_path
  ssh_source_ips             = var.ssh_source_ips

  ghcr_auth_b64           = var.ghcr_auth_b64
  user_postgres_password  = var.user_postgres_password
  user_redis_password     = var.user_redis_password
  user_service_jwt_secret = var.user_service_jwt_secret
}
