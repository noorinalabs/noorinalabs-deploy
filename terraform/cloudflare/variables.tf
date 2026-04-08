variable "cloudflare_api_token" {
  description = "Cloudflare API token with Zone:DNS:Edit and Zone:Zone:Read permissions"
  type        = string
  sensitive   = true
}

variable "cloudflare_zone_id" {
  description = "Cloudflare Zone ID for noorinalabs.com"
  type        = string
}

variable "vps_ipv4_address" {
  description = "Public IPv4 address of the Hetzner VPS"
  type        = string
}

variable "domain" {
  description = "Root domain name"
  type        = string
  default     = "noorinalabs.com"
}

variable "subdomains" {
  description = "Map of subdomain names to their proxy status (false = DNS only, true = proxied)"
  type        = map(bool)
  default = {
    "isnad-graph" = false
  }
}
