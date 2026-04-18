terraform {
  required_version = ">= 1.5.0"

  required_providers {
    b2 = {
      source  = "Backblaze/b2"
      version = "~> 0.10"
    }
  }

  backend "s3" {
    bucket                      = "noorinalabs-terraform-state"
    key                         = "backblaze/terraform.tfstate"
    region                      = "us-east-005"
    endpoint                    = "https://s3.us-east-005.backblazeb2.com"
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
  }
}
