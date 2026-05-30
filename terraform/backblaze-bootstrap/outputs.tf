output "state_bucket_id" {
  description = "B2 bucket ID for the Terraform state bucket."
  value       = b2_bucket.terraform_state.bucket_id
}

output "state_bucket_name" {
  description = "B2 bucket name for the Terraform state bucket."
  value       = b2_bucket.terraform_state.bucket_name
}

output "s3_endpoint" {
  description = "S3-compatible endpoint for state-bucket access (matches every root's backend block)."
  value       = "https://s3.us-east-005.backblazeb2.com"
}

output "tf_state_writer_key_id" {
  description = "Application key ID for the state-bucket read/write key. Feed into the CI TF_STATE_B2_KEY_ID secret + operator AWS_ACCESS_KEY_ID per the annual-rotation runbook."
  value       = b2_application_key.tf_state_writer.application_key_id
  sensitive   = true
}

output "tf_state_writer_key" {
  description = "Application key secret for the state-bucket read/write key. Feed into the CI TF_STATE_B2_APP_KEY secret + operator AWS_SECRET_ACCESS_KEY per the annual-rotation runbook."
  value       = b2_application_key.tf_state_writer.application_key
  sensitive   = true
}
