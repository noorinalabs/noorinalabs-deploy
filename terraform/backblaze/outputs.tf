output "bucket_id" {
  description = "B2 bucket ID for noorinalabs-pipeline"
  value       = b2_bucket.pipeline.bucket_id
}

output "bucket_name" {
  description = "B2 bucket name"
  value       = b2_bucket.pipeline.bucket_name
}

output "s3_endpoint" {
  description = "S3-compatible endpoint for pipeline bucket access"
  value       = "https://s3.us-east-005.backblazeb2.com"
}

output "pipeline_rw_key_id" {
  description = "Application key ID with read/write access to the pipeline bucket"
  value       = b2_application_key.pipeline_rw.application_key_id
  sensitive   = true
}

output "pipeline_rw_key" {
  description = "Application key secret with read/write access to the pipeline bucket"
  value       = b2_application_key.pipeline_rw.application_key
  sensitive   = true
}

output "pipeline_ro_key_id" {
  description = "Application key ID with read-only access to the pipeline bucket"
  value       = b2_application_key.pipeline_ro.application_key_id
  sensitive   = true
}

output "pipeline_ro_key" {
  description = "Application key secret with read-only access to the pipeline bucket"
  value       = b2_application_key.pipeline_ro.application_key
  sensitive   = true
}
