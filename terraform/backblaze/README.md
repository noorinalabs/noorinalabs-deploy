# Backblaze B2 — Pipeline Bucket

Provisions the `noorinalabs-pipeline` B2 bucket used by the data ingestion pipeline, plus
two scoped application keys (read/write for workers, read-only for monitoring).

## Prefix Layout

```
noorinalabs-pipeline/
  raw/{source}/{YYYY-MM-DD}/{filename}
  dedup/{source}/{batch-id}/{filename}
  enriched/{source}/{batch-id}/{filename}
  normalized/{batch-id}/{filename}
  staged/{batch-id}/{filename}
```

Stage ownership:

| Prefix | Writer | Reader |
|---|---|---|
| `raw/` | `noorinalabs-data-acquisition` | dedup worker |
| `dedup/` | dedup worker | enrich worker |
| `enriched/` | enrich worker | normalize worker |
| `normalized/` | normalize worker | graph-load worker |
| `staged/` | graph-load worker | — (archival + audit) |

## Prerequisites

1. A Backblaze B2 account with billing enabled.
2. A master application key with `writeBuckets` + `writeKeys` capabilities. Create
   it in the [B2 console → App Keys](https://secure.backblaze.com/app_keys.htm).
3. Terraform 1.5+ and credentials for the S3 state backend (same
   `noorinalabs-terraform-state` bucket as the other modules).

## Apply

**NOTE:** This module is **not applied in CI** — run it once, manually, from an
operator workstation. The resulting keys are fed into GitHub Actions secrets.

```bash
cd terraform/backblaze

# Authenticate to the S3 state backend (same Backblaze account as app keys).
export AWS_ACCESS_KEY_ID=<state-bucket-key-id>
export AWS_SECRET_ACCESS_KEY=<state-bucket-key>

# Provide the master B2 key Terraform will use to create the bucket and app keys.
export TF_VAR_b2_application_key_id=<master-key-id>
export TF_VAR_b2_application_key=<master-key>

terraform init
terraform plan
terraform apply
```

After apply, export the scoped keys (they are marked sensitive):

```bash
terraform output -raw pipeline_rw_key_id
terraform output -raw pipeline_rw_key
terraform output -raw pipeline_ro_key_id
terraform output -raw pipeline_ro_key
```

## Feeding keys to deployments

Add the scoped keys to the `noorinalabs-deploy` GitHub repo secrets (or the
pipeline repo's secrets, once the ingest-platform workflow exists):

| Secret | Value |
|---|---|
| `PIPELINE_B2_KEY_ID` | `pipeline_rw_key_id` output |
| `PIPELINE_B2_KEY` | `pipeline_rw_key` output |
| `PIPELINE_B2_BUCKET` | `noorinalabs-pipeline` |
| `PIPELINE_B2_ENDPOINT` | `https://s3.us-east-005.backblazeb2.com` |

The compose `.env` on the VPS is then populated via the existing SSH-based
deploy workflow (see `scripts/verify_deployment.sh` for the env injection
pattern).

## Lifecycle

The module configures two lifecycle rules:

1. **Unfinished multipart uploads** — abandoned after
   `lifecycle_days_unfinished_uploads` days (default 7).
2. **Old versions** — hidden after `lifecycle_days_hide_old_versions` days
   (default 30), deleted after an additional
   `lifecycle_days_delete_hidden` days (default 90).

`raw/` is the only stage where the pipeline expects archival retention;
downstream stages are rebuildable from `raw/` via `/ontology-librarian
pipeline reset levels`.

## Local development

See `../../compose/docker-compose.minio.yml` for a MinIO-backed alternative
that mirrors this prefix layout for local pipeline work.
