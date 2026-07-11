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
| `grafana_data` | `grafana` | Grafana's `grafana.db` — dashboards, users/orgs, API keys, annotations | no | Dashboards **are** code (`infra/grafana/dashboards/*.json`, mounted `:ro`) and alerting lives in Prometheus, not Grafana. Measured on both hosts: 4 dashboards, all 4 provisioned from git, **0 UI-created**, 0 API keys, 0 annotations, 0 Grafana alert rules. Nothing here is unreproducible — but nothing *enforces* that. See deploy#566. |
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

### Orphaned volume twins — back up the wrong one and you get an empty archive

Both hosts carry **two** similarly-named volumes for the user-service stores. Only one of
each pair is mounted by a running container:

| Live (compose project `noorinalabs`) | Orphan — mounted by nothing |
|---|---|
| `noorinalabs_user_pg_data` | `user-postgres-data` |
| `noorinalabs_user_redis_data` | `user-redis-data` |

Verified 2026-07-09 on stg and prod: `docker ps -a --filter volume=user-postgres-data`
returns no containers, while `docker inspect noorinalabs-user-postgres-1` shows
`noorinalabs_user_pg_data:/var/lib/postgresql/data`. prod additionally carries a stale
`noorinalabs_kafka-data` (hyphen) alongside the live `noorinalabs_kafka_data`
(underscore).

The orphans are leftovers from an earlier compose project name. They are **not** deleted
here — that is a destructive operation and needs its own decision — but they are a live
trap: a backup aimed at `user-postgres-data` would succeed, upload, checksum cleanly, and
contain nothing. A green pipeline and an empty archive.

**Never resolve a data volume by name.** Resolve it from the running container of the
compose project you mean:

```bash
docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' \
  "$(docker compose -f "$COMPOSE_FILE" ps -aq user-postgres)"
```

`backup.sh` and `restore.sh` both resolve the Neo4j volume this way as of deploy#559/#560.
The previous `docker volume ls | grep -E '(neo4j_data|neo4j-data)$' | head -1` matched
across *every* compose project on the host — combined with `--overwrite-destination`, a
restore aimed at a scratch stack could have overwritten the production graph.

## Off-host stores

| Store | Managed by | Contents | Backed up | Rationale |
|---|---|---|---|---|
| B2 `noorinalabs-terraform-state` | `terraform/backblaze-bootstrap/` | `hetzner/{stg,prod}.tfstate`, `cloudflare`, `backblaze` state | by the bucket itself | Superseded versions are retained 7 days (`days_from_hiding_to_deleting = 7`); the head of every key is retained indefinitely. Losing it means losing declarative control of the infrastructure. |
| B2 `noorinalabs-pipeline` | `terraform/backblaze/` | raw → dedup → enriched → normalized → staged pipeline artifacts | no separate copy | This *is* the source of truth from which the graph is rebuilt. It has lifecycle rules but no second copy. |
| B2 backups bucket | `terraform/backblaze/` (added by deploy#559) | the dumps this document describes | n/a | Did not exist before deploy#559. See the warning below. |

## The state of backups, as of 2026-07-10

Read this before trusting anything above.

1. **No backup has ever been taken.** `isnad-backup.timer` had never been installed on
   stg or prod (`systemctl list-unit-files 'isnad*'` → `0 unit files listed` on both,
   2026-07-09). deploy#558 fixes the install: cloud-init now enables the timer with
   `--now`, `scripts/converge_host.sh` installs it on already-provisioned hosts, and
   `scripts/assert_host_state.sh` asserts the result in `verify-deploy`. The timer
   therefore goes active on the next deploy to each host — and it will then **fail**,
   for the reason in 2.
2. **There is still no backup credential.** `.github/actions/write-deploy-env/action.yml`
   maps `BACKUP_B2_KEY_ID` / `BACKUP_B2_APP_KEY` / `BACKUP_B2_BUCKET` onto the bare
   `B2_*` names `backup.sh` requires. Those three secrets do not exist at repo scope or
   on either Environment, so `.env` on both hosts contains `B2_KEY_ID=''`, and
   `backup.sh` aborts at its credential preflight before dumping a byte.

   Combined with 1, this has an operational consequence worth stating plainly: once the
   timer is active, the 03:01 UTC run fails, `assert_host_state.sh` sees a failed run,
   `verify-stg` goes red, and the `stg-verify-result` gate **hard-blocks prod
   promotion**. That is the gate behaving exactly as designed — it is refusing to
   promote a host whose backups are failing. The missing piece is a credential only an
   owner can mint, not a check that needs softening.
3. **There is no destination bucket.** deploy#559 adds the Terraform; an owner must
   apply it (the apply needs the B2 master key) and set the three secrets.

Until 1–3 are resolved, every "yes" in the tables above describes what `backup.sh` *will*
dump, not what exists in a bucket. There is nothing to restore from today.

## Deliberate gaps worth revisiting

* **`grafana_data`.** Disposable as a matter of current *fact*, not of *enforcement*.
  Provisioning-as-code already exists and every dashboard on both hosts traces back to
  git. But the mount is `:ro`, so a UI edit lands as a **new** row in `grafana.db` and
  silently diverges — falsifying this table's row, discovered only when the volume is
  gone. The fix is a drift guard asserting `ui_created_not_in_git == 0`, **not** a volume
  tarball. deploy#566.

  > An earlier draft of this document asserted these dashboards were "hand-built and not
  > version-controlled." That was false, and it pointed at the wrong remedy. The
  > correction came from querying `grafana.db` on both hosts instead of trusting the
  > sentence.
* **`caddy_data`.** Low probability, but the Let's Encrypt rate limits make the recovery
  path slower than operators expect.
* **B2 `noorinalabs-pipeline`.** Single copy of the artifact the whole graph is rebuilt
  from. Worth a lifecycle/replication review.

## Verifying a restore

`scripts/restore.sh` **refuses an incomplete backup by default.** A store that is expected
but **absent** is now as fatal as one whose restore fails — pass `--allow-partial` to
restore anyway, which is a DR escape hatch and not a convenience.

That gate exists because the previous behaviour was the defect this document warns about,
committed in code. An absent dump logged a `WARNING`, recorded `skipped (no dump)`, and
never set `FAILED` — so the script printed `=== Restore complete ===` and exited **0** on a
backup with no accounts, no sessions and no `audit_log` in it. The all-empty case was
caught and the one-store-missing case was not: a guard on each side of the hole and none in
it. Two reviewers found it independently (deploy#577).

It was reachable by an artifact *this repo produces*: `backup.sh` deliberately uploads a
partial when a leg fails ("a partial backup beats none"), that directory lands in B2
date-stamped and checksums cleanly, and `latest` selected it by directory **name**. So the
night the `user-postgres` dump fails, `restore.sh latest` picked precisely the artifact
missing the one store nothing can rebuild — and said the restore was complete. The alerting
could not help: it watches the *backup*, and the backup correctly reported that it failed.
**The restore was the thing that lied.**

So **completeness is a property of the artifact, and the artifact declares it**:
`backup.sh` writes `_backup_manifest.txt` (`BACKUP_MANIFEST complete=<bool> stores=<csv>`),
and `resolve_latest` selects the newest backup that declares itself **complete**, naming any
it skipped. A directory with no manifest is not complete — every pre-deploy#559 backup
predates `user-postgres` coverage entirely.

The manifest is **not** the source of the expected set. `restore.sh` carries its own list of
required stores; if the expected set came from the artifact, a partial backup would declare
itself complete-for-whatever-it-happens-to-hold — the same circularity as a read-back count,
and just as invisible.

Do not read a zero exit as "the data is back" — `pg_restore --clean` can warn and succeed
while restoring nothing. Assert on content.

`scripts/restore_rehearsal.sh` does exactly that against a throwaway stack: it requires
`restore.sh` to **reject** six broken artifacts before it trusts an intact one, then
asserts row counts, sampled records, and the Neo4j node count. It runs in CI
(`.github/workflows/restore-rehearsal.yml`) on changes to the backup/restore path and
weekly. See deploy#560.
