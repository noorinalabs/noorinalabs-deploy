provider "b2" {
  application_key_id = var.b2_application_key_id
  application_key    = var.b2_application_key
}

# Pipeline bucket — holds raw → dedup → enriched → normalized → staged prefixes.
# Access is always server-side (pipeline workers) over the S3-compatible endpoint.
resource "b2_bucket" "pipeline" {
  bucket_name = var.bucket_name
  bucket_type = "allPrivate"

  lifecycle_rules {
    file_name_prefix              = ""
    days_from_uploading_to_hiding = var.lifecycle_days_hide_old_versions
    days_from_hiding_to_deleting  = var.lifecycle_days_delete_hidden
  }

  # Abort stuck multipart uploads to avoid silent cost growth.
  lifecycle_rules {
    file_name_prefix                                       = ""
    days_from_starting_to_canceling_unfinished_large_files = var.lifecycle_days_unfinished_uploads
  }

  dynamic "cors_rules" {
    for_each = length(var.cors_allowed_origins) > 0 ? [1] : []
    content {
      cors_rule_name     = "pipeline-cors"
      allowed_origins    = var.cors_allowed_origins
      allowed_operations = ["s3_get", "s3_head", "s3_put"]
      max_age_seconds    = 3600
      allowed_headers    = ["*"]
      expose_headers     = ["x-bz-content-sha1"]
    }
  }
}

# Read-write key scoped to the pipeline bucket — used by ingest platform workers.
resource "b2_application_key" "pipeline_rw" {
  key_name  = "noorinalabs-pipeline-rw"
  bucket_id = b2_bucket.pipeline.bucket_id
  capabilities = [
    "listBuckets",
    "listFiles",
    "readFiles",
    "writeFiles",
    "deleteFiles",
  ]
}

# Read-only key scoped to the pipeline bucket — used by monitoring/audit tools.
resource "b2_application_key" "pipeline_ro" {
  key_name  = "noorinalabs-pipeline-ro"
  bucket_id = b2_bucket.pipeline.bucket_id
  capabilities = [
    "listBuckets",
    "listFiles",
    "readFiles",
  ]
}
