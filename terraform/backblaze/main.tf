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
      expose_headers     = ["x-bz-content-sha1", "ETag"]
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

# ---------------------------------------------------------------------------
# Backups bucket (deploy#559)
# ---------------------------------------------------------------------------
# scripts/backup.sh and scripts/restore.sh have referenced a backups bucket since they
# were written, and no such bucket has ever existed in IaC or in the B2 account. The
# backup timer has never run (deploy#558), so the absence was never surfaced by a
# failing upload.
#
# Retention here is intentionally NOT the pipeline bucket's version-lifecycle. backup.sh
# manages its own retention by pruning date-stamped directories (7 daily, 4 weekly), so a
# bucket-side `days_from_uploading_to_hiding` would race that logic and could hide the
# only surviving copy. What we do enforce is the abandonment of stuck multipart uploads,
# which otherwise accumulate cost silently, and the prompt deletion of versions once
# backup.sh has hidden them by deleting the object.
resource "b2_bucket" "backups" {
  bucket_name = var.backups_bucket_name
  bucket_type = "allPrivate"

  # Once backup.sh's retention prune deletes an object, B2 keeps the hidden version
  # around. Purge it after a short grace window rather than paying for it forever.
  lifecycle_rules {
    file_name_prefix             = ""
    days_from_hiding_to_deleting = var.backups_days_from_hiding_to_deleting
  }

  lifecycle_rules {
    file_name_prefix                                       = ""
    days_from_starting_to_canceling_unfinished_large_files = var.lifecycle_days_unfinished_uploads
  }
}

# Write-capable key scoped to the backups bucket. This is the credential the VPS backup
# timer uses, delivered as the BACKUP_B2_* GitHub Environment secrets.
#
# `deleteFiles` is required: backup.sh prunes its own retention window with `rclone
# purge`. A read-only key here would 401 on the first upload, and rclone reports that
# 401 as "failed to create bucket" — a misleading error that has cost this project time
# before. If you rotate this key, rotate it to a key with these capabilities.
resource "b2_application_key" "backups_rw" {
  key_name  = "noorinalabs-backups-rw"
  bucket_id = b2_bucket.backups.bucket_id
  capabilities = [
    "listBuckets",
    "listFiles",
    "readFiles",
    "writeFiles",
    "deleteFiles",
  ]
}
