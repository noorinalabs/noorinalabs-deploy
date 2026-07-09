# Datastore inventory and backup coverage

Every datastore Noorina Labs runs, whether it is backed up, and — where it is not — why
that is a deliberate decision rather than an oversight.

> An undocumented omission and a deliberate exclusion look identical during an incident.
> That is the entire purpose of this file. If you add a stateful service, add a row here
> in the same change.

Enumerated from `compose/docker-compose.prod.yml` (named volumes and the services that
mount them), `terraform/backblaze/`, and `terraform/backblaze-bootstrap/`. Volume names
and mount points were cross-checked against the running stg and prod hosts on
2026-07-09. Tracked by deploy#559.

## On-host stores (`compose/docker-compose.prod.yml`)

Fourteen named volumes. Three are dumped by `scripts/backup.sh`.

| Volume | Mounted by | Contents | Backed up | Rationale |
|---|---|---|---|---|
| `user_pg_data` | `user-postgres` | accounts, roles, `user_roles`, sessions, `oauth_accounts`, `subscriptions`, `totp_secrets`, `verification_tokens`, `audit_log`, `alembic_version` | **yes** (`isnad-userpg-*.dump`) | **Not reconstructible from anything.** The pipeline artifact rebuilds the graph, not the users. Prod holds 9 accounts and 22 `audit_log` rows. Had **no** coverage before deploy#559. |
| `neo4j_data` | `neo4j` | the isnad graph | yes (`isnad-neo4j-*.dump.zst`) | Can be rebuilt from the published Parquet artifact via `graph-load`, but a rebuild takes hours and the artifact must exist. |
| `pg_data` | `postgres` | isnad relational metadata + pgvector embeddings | yes (`isnad-pg-*.dump`) | Can be rebuilt from the artifact plus a corpus re-embed, which is expensive. |
| `grafana_data` | `grafana` | dashboards, alert rules, Grafana users/orgs | **no** — see below | Hand-built and **not** version-controlled. This is the weakest "no" in the table. Tracked separately. |
| `caddy_data` | `caddy` | ACME account key, issued TLS certificates | no | Re-obtainable from Let's Encrypt. Note the rate limits (50 certs/week/domain, 5 duplicate certs/week) — a rebuild loop during an incident can lock you out of TLS for days. |
| `caddy_config` | `caddy` | Caddy's autosaved JSON config | no | Regenerated from `caddy/Caddyfile`, which is version-controlled. |
| `prometheus_data` | `prometheus` | metrics TSDB, 30-day retention | no | Observability history. Losing it costs history, not service. |
| `loki_data` | `loki` | log chunks | no | As above. |
| `kafka_data` | `kafka` | pipeline topic logs | no | Can be replayed from the B2 pipeline artifacts, which are the durable source. |
| `redis_data` | `redis` | API cache, rate limiting | no | `--maxmemory-policy allkeys-lru`; contents are cache by construction. It *does* hold a `dump.rdb` (RDB snapshotting is on by default in the image), but nothing in it is authoritative. |
| `user_redis_data` | `user-redis` | session state, rate limiting, verification tracking | no | Also holds a `dump.rdb`. Durable sessions live in `user_pg_data.sessions`; losing this forces re-login, not data loss. |
| `neo4j_logs` | `neo4j` | logs | no | Logs. |
| `loki_runtime` | `loki`, `api`, `loki-runtime-init` | generated runtime config | no | Created on every boot by `loki-runtime-init`. |
| `st_model_cache` | `isnad-graph-embed` | Hugging Face model cache | no | Re-downloadable; a cache. |

`compose/docker-compose.minio.yml` declares `minio_data`, but nothing deploys it — no
workflow or script references that file. It is a local-development convenience and is
out of scope.

## Off-host stores

| Store | Managed by | Contents | Backed up | Rationale |
|---|---|---|---|---|
| B2 `noorinalabs-terraform-state` | `terraform/backblaze-bootstrap/` | `hetzner/{stg,prod}.tfstate`, `cloudflare`, `backblaze` state | by the bucket itself | Superseded versions are retained 7 days (`days_from_hiding_to_deleting = 7`); the head of every key is retained indefinitely. Losing it means losing declarative control of the infrastructure. |
| B2 `noorinalabs-pipeline` | `terraform/backblaze/` | raw → dedup → enriched → normalized → staged pipeline artifacts | no separate copy | This *is* the source of truth from which the graph is rebuilt. It has lifecycle rules but no second copy. |
| B2 backups bucket | `terraform/backblaze/` (added by deploy#559) | the dumps this document describes | n/a | Did not exist before deploy#559. See the warning below. |

## The state of backups, as of 2026-07-09

Read this before trusting anything above.

1. **No backup has ever been taken.** `isnad-backup.timer` is not installed on stg or
   prod (`systemctl list-unit-files 'isnad*'` → `0 unit files listed` on both).
   Tracked by deploy#558.
2. **There is no backup credential.** `.github/actions/write-deploy-env/action.yml` maps
   `BACKUP_B2_KEY_ID` / `BACKUP_B2_APP_KEY` / `BACKUP_B2_BUCKET` onto the bare `B2_*`
   names `backup.sh` requires. Those three secrets do not exist at repo scope or on
   either Environment, so `.env` on both hosts contains `B2_KEY_ID=''`. `backup.sh`
   aborts at its `: "${B2_KEY_ID:?…}"` preflight before dumping a byte.
3. **There was no destination bucket.** deploy#559 adds the Terraform; an owner must
   apply it (the apply needs the B2 master key) and set the three secrets.

Until 1–3 are resolved, every "yes" in the tables above describes what `backup.sh` *will*
dump, not what exists in a bucket. There is nothing to restore from today.

## Deliberate gaps worth revisiting

* **`grafana_data`.** Dashboards and alert rules are hand-built and live nowhere else. The
  right fix is provisioning-as-code (`infra/grafana/provisioning/`) rather than a volume
  tarball, so this is filed as its own issue rather than bolted onto `backup.sh`.
* **`caddy_data`.** Low probability, but the Let's Encrypt rate limits make the recovery
  path slower than operators expect.
* **B2 `noorinalabs-pipeline`.** Single copy of the artifact the whole graph is rebuilt
  from. Worth a lifecycle/replication review.

## Verifying a restore

`scripts/restore.sh` exits non-zero if any dump present in the artifact fails to restore,
and refuses artifacts it cannot verify. Do not read a zero exit as "the data is back" —
`pg_restore --clean` can warn and succeed while restoring nothing. Assert on content.

`scripts/restore_rehearsal.sh` does exactly that against a throwaway stack: it requires
`restore.sh` to **reject** five broken artifacts before it trusts an intact one, then
asserts row counts, sampled records, and the Neo4j node count. It runs in CI
(`.github/workflows/restore-rehearsal.yml`) on changes to the backup/restore path and
weekly. See deploy#560.
