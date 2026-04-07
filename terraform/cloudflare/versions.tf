terraform {
  required_version = ">= 1.5.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.43"
    }
  }

  backend "s3" {
    bucket                      = "noorinalabs-terraform-state"
    key                         = "cloudflare/terraform.tfstate"
    region                      = "us-east-005"
    endpoints                   = { s3 = "https://s3.us-east-005.backblazeb2.com" }
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_s3_checksum            = true
  }
}
