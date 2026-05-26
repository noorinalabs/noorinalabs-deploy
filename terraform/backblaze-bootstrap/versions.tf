terraform {
  required_version = ">= 1.6.0"

  required_providers {
    b2 = {
      source = "Backblaze/b2"
      # Same provider family as terraform/backblaze/ (ADR 0004 Part-2 / #331).
      version = "~> 0.10"
    }
  }

  # INTENTIONALLY `local`, NOT the s3/B2 backend every other root uses.
  # This module MANAGES the `noorinalabs-terraform-state` bucket that B2-backed
  # state lives in — it cannot store its own state in the bucket it creates
  # (ADR 0004 Decision A2, the bounded chicken-and-egg). State lives in a local
  # `terraform.tfstate` here; see README.md for the commit/handling policy.
  backend "local" {}
}
