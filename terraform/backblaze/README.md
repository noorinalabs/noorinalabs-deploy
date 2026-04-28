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

## Pre-flight: provisioning credentials

This module needs **two distinct B2 application key pairs** before `terraform apply`
will succeed. They are not interchangeable. Mixing them up is the most common first-run
failure (403 on state read, or scope errors when the b2 provider tries to mint scoped
keys).

### Two key pairs, two roles

| Env var pair | Role | Scope | Created where | Used by |
|---|---|---|---|---|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | **State-bucket key** (S3 backend auth) | Single bucket: `noorinalabs-terraform-state` | B2 console, restricted to that one bucket | Terraform's S3 backend, every time you `terraform init/plan/apply` any module in this repo |
| `TF_VAR_b2_application_key_id` / `TF_VAR_b2_application_key` | **Master b2 provider key** | Account-wide (`writeBuckets` + `writeKeys`) | B2 console, no bucket restriction | The `b2` provider in `main.tf`, only when applying this module — to create the pipeline bucket and mint the scoped `pipeline_rw` / `pipeline_ro` keys |

Mental model: the state-bucket key is a **boring, narrow, long-lived** credential you
will reuse across every TF root. The master key is a **powerful, short-lived** credential
you only need in your shell during a bootstrap apply, and which mints the actual
runtime credentials (the scoped pipeline keys) that the workers use. Workers never see
the master key.

> Rotate-direction guard: if you accidentally export the master key in the `AWS_*` slot,
> the S3 backend will reject it (the master key is not authorised against the
> `noorinalabs-terraform-state` bucket via the S3 protocol — B2 application keys
> require explicit bucket-name auth for the S3 endpoint). If you export the
> state-bucket key in the `TF_VAR_b2_*` slot, the b2 provider will appear to
> authenticate but will 401/403 on `b2_bucket` and `b2_application_key` create calls.
> Both failure modes are recoverable by re-exporting the right pair.

### Provisioning the master key

Required once per operator (or once ever, if you're willing to share the same master
key across operators via a shared password manager — discouraged, but supported).

1. Log in to the [B2 console → App Keys](https://secure.backblaze.com/app_keys.htm).
2. Click **Add a New Application Key**.
3. Set **Name of Key**: `noorinalabs-terraform-master-{operator-handle}` (the suffix
   helps audit which operator's key did what).
4. Set **Allow access to Bucket(s)**: *All* (the master key must be account-wide).
5. Set **Type of Access**: *Read and Write*.
6. Leave **File name prefix** and **Duration** blank.
7. Click **Create New Key**. The console will display **`keyID`** and
   **`applicationKey`** (the secret) on the next screen.

> **B2 displays the application key secret exactly once.** The `applicationKey` value
> is shown on the post-creation confirmation screen and never again. Copy both
> `keyID` and `applicationKey` into a password manager (1Password, Bitwarden, etc.)
> **before navigating away from the page**. If you lose the secret, your only recovery
> is to delete the key in the console and create a new one — there is no "show secret
> again" option.

Export for the apply session:

```bash
export TF_VAR_b2_application_key_id=<keyID>
export TF_VAR_b2_application_key=<applicationKey>
```

### Provisioning the state-bucket key

Required once per operator workstation. This key is what authenticates Terraform's S3
backend against the `noorinalabs-terraform-state` bucket (which lives in B2 via the
S3-compatible API). You'll reuse the resulting pair for every TF module in this repo,
not just this one.

You need an existing master key (above) to create it. From the same
[B2 console → App Keys](https://secure.backblaze.com/app_keys.htm) page:

1. Click **Add a New Application Key**.
2. **Name of Key**: `noorinalabs-tfstate-{operator-handle}`.
3. **Allow access to Bucket(s)**: select **`noorinalabs-terraform-state`** (single
   bucket — this is the scope restriction).
4. **Type of Access**: *Read and Write*.
5. Leave **File name prefix** and **Duration** blank.
6. Click **Create New Key**. Same one-time-display rule applies — copy both values
   into your password manager before navigating away.

Equivalent via the `b2` CLI (if installed and authenticated with the master key):

```bash
b2 key create \
  --bucket noorinalabs-terraform-state \
  noorinalabs-tfstate-$(whoami) \
  listBuckets,listFiles,readFiles,writeFiles,deleteFiles
```

Output is `keyID applicationKey` on a single line — capture both into a password
manager immediately.

Export for any TF session in this repo:

```bash
export AWS_ACCESS_KEY_ID=<keyID>
export AWS_SECRET_ACCESS_KEY=<applicationKey>
```

These can also live in `~/.aws/credentials` under a named profile if you prefer
(then `export AWS_PROFILE=noorinalabs-tfstate`); the S3 backend honours both styles.

## Prerequisites

1. A Backblaze B2 account with billing enabled.
2. The two key pairs above provisioned and exported (see
   [Pre-flight](#pre-flight-provisioning-credentials)).
3. Terraform 1.5+.

## Apply

**NOTE:** This module is **not applied in CI** — run it once, manually, from an
operator workstation. The resulting keys are fed into GitHub Actions secrets.

Confirm both credential pairs from the [Pre-flight](#pre-flight-provisioning-credentials)
section are exported in your shell, then:

```bash
cd terraform/backblaze

terraform init
terraform plan
terraform apply
```

If `init` fails with a 403 on the state backend, you have the wrong key in the `AWS_*`
slot — see the rotate-direction guard above.

## Retrieving sensitive outputs

The four pipeline-key outputs in `outputs.tf` are marked `sensitive = true`, which is
correct — it keeps them out of plan/apply console output and out of any logs scraped
from CI. The trade-off: a plain `terraform output pipeline_rw_key` prints
`<sensitive>` instead of the value.

To retrieve a value, use the `-raw` flag — that's the canonical incantation that
unlocks sensitive outputs for downstream consumption:

```bash
terraform output -raw pipeline_rw_key_id
terraform output -raw pipeline_rw_key
terraform output -raw pipeline_ro_key_id
terraform output -raw pipeline_ro_key
```

To pipe directly into GitHub Actions secrets without exposing the value to your
shell history, combine `-raw` with `gh secret set --body -`:

```bash
terraform output -raw pipeline_rw_key_id \
  | gh secret set PIPELINE_B2_KEY_ID --repo noorinalabs/noorinalabs-deploy --body -

terraform output -raw pipeline_rw_key \
  | gh secret set PIPELINE_B2_KEY    --repo noorinalabs/noorinalabs-deploy --body -

terraform output -raw pipeline_ro_key_id \
  | gh secret set PIPELINE_B2_KEY_ID_RO --repo noorinalabs/noorinalabs-deploy --body -

terraform output -raw pipeline_ro_key \
  | gh secret set PIPELINE_B2_KEY_RO    --repo noorinalabs/noorinalabs-deploy --body -
```

The `--body -` form reads the secret value from stdin, which keeps it out of your
shell history. Avoid `gh secret set FOO --body "$(terraform output -raw foo)"` —
that ends up in `~/.zsh_history` / `~/.bash_history` in plaintext.

To dump all outputs as JSON (useful for scripting; values are still raw, not
`<sensitive>`-redacted, in JSON output):

```bash
terraform output -json | jq -r '.pipeline_rw_key.value'
```

## Feeding keys to deployments

Add the scoped keys to the `noorinalabs-deploy` GitHub repo secrets (or the
pipeline repo's secrets, once the ingest-platform workflow exists). Use the
[Retrieving sensitive outputs](#retrieving-sensitive-outputs) `gh secret set` pattern
above to populate them without round-tripping through the shell.

| Secret | Value |
|---|---|
| `PIPELINE_B2_KEY_ID` | `pipeline_rw_key_id` output |
| `PIPELINE_B2_KEY` | `pipeline_rw_key` output |
| `PIPELINE_B2_BUCKET` | `noorinalabs-pipeline` |
| `PIPELINE_B2_ENDPOINT` | `https://s3.us-east-005.backblazeb2.com` |

The compose `.env` on the VPS is then populated via the existing SSH-based
deploy workflow (see `scripts/verify_deployment.sh` for the env injection
pattern).

## Secret flow

```
                    ┌──────────────────────────┐
                    │  B2 console (operator)   │
                    └────────────┬─────────────┘
                                 │ provisions, one-shot
                ┌────────────────┴────────────────┐
                ▼                                 ▼
   ┌────────────────────────┐         ┌────────────────────────┐
   │  state-bucket key      │         │  master b2 key         │
   │  (AWS_* env vars)      │         │  (TF_VAR_b2_* env vars)│
   │  scope: tfstate bucket │         │  scope: account-wide   │
   └───────────┬────────────┘         └───────────┬────────────┘
               │ S3 backend auth                  │ b2 provider auth
               │ (every TF root, every apply)     │ (this module only)
               ▼                                  ▼
        terraform init/plan/apply ──────────► b2_bucket.pipeline
                                              b2_application_key.pipeline_rw  (sensitive)
                                              b2_application_key.pipeline_ro  (sensitive)
                                                          │
                                       terraform output -raw │ gh secret set --body -
                                                          ▼
                                              GH Actions secrets
                                              (PIPELINE_B2_KEY*, etc.)
                                                          │
                                                          ▼
                                              VPS compose .env
                                                          │
                                                          ▼
                                              pipeline workers
```

The master key never leaves the operator's shell. The scoped keys never touch
plaintext on disk outside the password manager and GH-secrets store.

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
