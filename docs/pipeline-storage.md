# Pipeline Object Storage

The data ingestion pipeline stores all intermediate artifacts in a single
S3-compatible bucket. In production this is a Backblaze B2 bucket
(`noorinalabs-pipeline`); in local development it is a MinIO container that
mirrors the same prefix layout.

Single bucket, prefix-per-stage, so pipeline code only has to understand
`{stage}/{key}` and not an N-bucket topology.

## Layout

```
noorinalabs-pipeline/
  raw/{source}/{YYYY-MM-DD}/{filename}
  dedup/{source}/{batch-id}/{filename}
  enriched/{source}/{batch-id}/{filename}
  normalized/{batch-id}/{filename}
  staged/{batch-id}/{filename}
```

| Prefix | Written by | Read by | Notes |
|---|---|---|---|
| `raw/` | `noorinalabs-data-acquisition` | dedup worker | Archival — never deleted unless source is reset |
| `dedup/` | dedup worker | enrich worker | Rebuildable from `raw/` |
| `enriched/` | enrich worker | normalize worker | Rebuildable from `dedup/` |
| `normalized/` | normalize worker | graph-load worker | Rebuildable from `enriched/` |
| `staged/` | graph-load worker | — | Audit trail of what was loaded into Neo4j |

See `ontology/repos/ingestion.yaml` and
`ontology/repos/isnad-ingest-platform.yaml` `reset_levels` for how pipeline
resets map onto prefix deletion.

## Access pattern

All pipeline workers access the bucket via the S3-compatible API. Prod and
dev share the same code path — only the endpoint, credentials, and region
vary. Pipeline containers receive the following env vars:

| Env var | Prod value | Dev value |
|---|---|---|
| `PIPELINE_B2_KEY_ID` | Scoped RW key from Terraform output | `minioadmin` |
| `PIPELINE_B2_KEY` | Scoped RW key secret | `minioadmin-change-me` |
| `PIPELINE_B2_BUCKET` | `noorinalabs-pipeline` | `noorinalabs-pipeline` |
| `PIPELINE_B2_ENDPOINT` | `https://s3.us-east-005.backblazeb2.com` | `http://minio:9000` |
| `PIPELINE_B2_REGION` | `us-east-005` | `us-east-005` |

Two application keys exist — a read/write key for pipeline workers and a
read-only key for monitoring/audit tools. Monitoring code should never see
the RW key.

## Provisioning

- **Production:** `terraform/backblaze/` module. See its README for one-time
  apply instructions. Bucket creation is **not** in CI — it is an operator
  action, run once per environment.
- **Local dev:** `compose/docker-compose.minio.yml`. The `minio-setup`
  one-shot creates the bucket and seeds empty stage prefixes.

## Secret flow

```
B2 console → master key (in operator shell only)
  → terraform apply → scoped rw/ro keys (outputs)
    → GitHub Actions secrets (PIPELINE_B2_KEY_ID, PIPELINE_B2_KEY)
      → deploy workflow SSH → VPS .env
        → docker compose env → pipeline workers
```

The master key never leaves the operator's workstation. Only the scoped keys
travel to CI, and only the rw scoped key reaches workers.

## Rotation

To rotate the RW key (e.g. after a suspected leak):

1. In B2 console, revoke the old `noorinalabs-pipeline-rw` key.
2. `terraform apply` — Terraform sees the key is missing and recreates it.
3. Update the GitHub Actions `PIPELINE_B2_KEY` and `PIPELINE_B2_KEY_ID`
   secrets with the new outputs.
4. Trigger a redeploy so workers pick up the new creds.
