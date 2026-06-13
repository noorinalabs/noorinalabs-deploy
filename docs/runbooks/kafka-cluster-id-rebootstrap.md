# Runbook: Kafka KRaft log-dir re-bootstrap (deploy#393, deploy#428)

**Scope:** the pipeline Kafka broker (`kafka` service in `compose/docker-compose.prod.yml`) failing its healthcheck on a stg or prod deploy because the persisted `kafka_data` volume's KRaft log directory is in a state `apache/kafka:3.9.2` refuses to start on. Two variants are covered, both leftovers from the Bitnami → `apache/kafka:3.9.2` migration (deploy#100 / PR #385), and both fixed by the SAME re-bootstrap (wipe + reformat the log dir under the new image's storage format):

1. **Cluster-id mismatch** (deploy#393) — the volume was formatted with a cluster id that no longer matches the `KAFKA_CLUSTER_ID` the broker is started with → `InconsistentClusterIdException`. See § Why this happens.
2. **Stray non-topic directories** (deploy#428) — the log-dir root carries a leftover Bitnami-era `config/` dir (and/or a nested `data/`) that apache-kafka's LogManager fatally rejects. See § Variant: stray non-topic directories.

A volume can hit either or both; the recovery (§ Recovery) is identical.

**NOT in scope:**
- Routine deploys where the broker is already healthy — this runbook only applies to a cluster-id mismatch or a stray-non-topic-dir rejection in the log dir.
- App-tier (frontend / api / user-service) outages — Kafka is the ingestion-pipeline tier, not in the app request path. App health is independent (see `RUNBOOK.md`).
- Topic layout / retention — owned by `infra/kafka/init-topics.sh`, see `docs/pipeline-kafka.md`.

## Why this happens

`apache/kafka` runs `kafka-storage format` **only on a fresh (empty) log directory**, using the `CLUSTER_ID` env value. When the `kafka_data` volume already contains a `meta.properties` written by a *different* cluster ID — e.g. the Bitnami broker's id from before PR #385, or a `KAFKA_CLUSTER_ID` secret that was rotated — the broker refuses to start cleanly. It logs an `InconsistentClusterIdException` and never reaches `RUNNING`, so the container healthcheck (`kafka-broker-api-versions.sh`) never passes. Compose then aborts the whole stack on `kafka-init`'s `depends_on: condition: service_healthy`, surfacing as:

```
Container noorinalabs-kafka-1  Error
dependency failed to start: container noorinalabs-kafka-1 is unhealthy
```

This was masked from 2026-05-19 until 2026-06-01 because every staging deploy failed earlier at the frontend image pull (the GHCR-publish 401 bug, isnad-graph#840 → fixed in #940). Once the deploy got past the frontend pull it reached kafka and revealed this pre-existing mismatch (deploy#393).

**Recovery model:** the pipeline is replay-from-B2 by design — broker state (topic offsets, queued pointers) is reconstructable. Wiping `kafka_data` is therefore low-cost: producers re-emit pointers and consumers reprocess from B2. This is the same trade-off PR #385's side-effects table (item 1) and #100 accepted up front.

## Detecting the mismatch

The deploy composite (`.github/actions/write-deploy-env`) runs a pre-flight check that compares the persisted volume's `meta.properties` cluster id against the `KAFKA_CLUSTER_ID` in `.env` and **fails loud** with a pointer to this runbook before `docker compose up` recreates the broker. If you are reading this because that step failed, the diagnosis is already made for you — proceed to § Recovery.

To confirm manually on the VPS:

```bash
cd /opt/noorinalabs-deploy

# What the broker would be started with:
grep '^KAFKA_CLUSTER_ID=' .env

# What the persisted volume was actually formatted with:
docker run --rm -v noorinalabs_kafka_data:/data alpine \
    sh -c 'cat /data/meta.properties 2>/dev/null | grep -i cluster.id || echo "no meta.properties (fresh volume)"'
```

If the two `cluster.id` values differ, this runbook applies. If the volume has no `meta.properties`, the broker will format fresh on next boot — no action needed.

> The volume name is `noorinalabs_kafka_data` — compose project `noorinalabs` (the `-p noorinalabs` flag in the deploy scripts) prefixes the `kafka_data` volume declared in `compose/docker-compose.prod.yml`. Confirm with `docker volume ls | grep kafka_data`.

## Variant: stray non-topic directories (deploy#428)

This is a SEPARATE failure mode from the cluster-id mismatch above, with the same root cause class (a Bitnami-era volume layout the apache image rejects) and the same fix.

`apache/kafka`'s `LogManager` scans every entry at the log-dir root on startup and **fatally rejects any directory that is not a Kafka topic-partition dir** (`<topic>-<partition>`, optionally a `.<uniqueId>-delete` / `-future` suffix). A stray `config/` (empty, dated to the Bitnami era) and/or a nested `data/` left over on the volume make the broker abort with:

```
ERROR Encountered fatal fault: Error starting LogManager
org.apache.kafka.common.KafkaException: Found directory /var/lib/kafka/data/config,
'config' is not in the form of topic-partition or topic-partition.uniqueId-delete ...
Kafka's log directories (and children) should only contain Kafka topic data.
```

Like the cluster-id case, the broker never reaches healthy and compose aborts the whole stack on `kafka-init`'s `depends_on: condition: service_healthy`, surfacing only as the opaque `dependency failed to start: container noorinalabs-kafka-1 is unhealthy`. This was the proximate cause of the deploy#428 prod outage (observed 2026-06-12 during the deploy#420 prod rollout): prod's volume retained the old Bitnami directory layout, while stg had been migrated onto a fresh volume in the #385/#100 cutover and was clean.

**Note:** the cluster-id pre-flight does NOT catch this — the ids can match while a stray dir is still present. The stray-dir check is a distinct guard.

### Detecting stray directories

The deploy composite (`.github/actions/write-deploy-env`) runs a second pre-flight — `scripts/kafka_logdir_preflight.sh` piped into the `apache/kafka:3.9.2` image over the mounted volume — that scans the log-dir root and **fails loud with a pointer to this section** before `docker compose up`. If you are reading this because that step failed, the offenders are already named in the failure output — proceed to § Recovery.

To confirm manually on the VPS:

```bash
cd /opt/noorinalabs-deploy

# List the log-dir root; anything that is NOT a "<topic>-<partition>" dir
# (or a legitimate file: meta.properties, *.checkpoint, *-offset-checkpoint,
# bootstrap.checkpoint, .lock) is a stray that will crash the broker.
docker run --rm --entrypoint /bin/sh \
    -v noorinalabs_kafka_data:/data apache/kafka:3.9.2 \
    -c 'ls -la /data'

# Or run the same scanner the pre-flight uses (exit 2 ⇒ stray dirs found):
docker run --rm -i --entrypoint /bin/sh \
    -v noorinalabs_kafka_data:/data apache/kafka:3.9.2 \
    -s /data < scripts/kafka_logdir_preflight.sh
```

A `config/`, `data/`, or `lost+found/` directory at the root means this variant applies. A clean root (only `__cluster_metadata-0/`, topic-partition dirs, and the files above) means it does not.

### Remediation

Identical to the cluster-id case: **wipe + reformat the log dir** per § Recovery below. Wiping `noorinalabs_kafka_data` removes the stray dirs along with the broker state; `apache/kafka` re-formats the empty log dir on next boot and `kafka-init` re-creates the topics. The replay-from-B2 recovery model (§ Why this happens, "Recovery model") applies unchanged — this is **OWNER / pipeline-owner gated** (volume wipe); surface before applying.

## Recovery (stg first, then prod)

Do stg first. A stg failure is cheap and proves the procedure before touching prod.

1. **Stop the broker (and its dependents) only** — leave the app tier running:
   ```bash
   cd /opt/noorinalabs-deploy
   docker compose -p noorinalabs -f compose/docker-compose.prod.yml \
       --env-file .env stop kafka kafka-init kafka-ui kafka-exporter
   docker compose -p noorinalabs -f compose/docker-compose.prod.yml \
       --env-file .env rm -f kafka kafka-init kafka-ui kafka-exporter
   ```

2. **Wipe the persisted log directory** so `apache/kafka` re-formats it under the current `KAFKA_CLUSTER_ID`:
   ```bash
   docker volume rm noorinalabs_kafka_data
   ```
   (The volume is recreated empty by the next `compose up`. If `docker volume rm` reports the volume is in use, a container above did not stop — re-run step 1.)

3. **Bring the broker back up.** Easiest is to re-run the deploy workflow for the env (`deploy-stg.yml` / `deploy-prod.yml`), which writes `.env` and rolls the stack. Or, on the host directly:
   ```bash
   docker compose -p noorinalabs -f compose/docker-compose.prod.yml \
       --env-file .env up -d kafka
   ```

4. **Confirm the broker formats and reaches healthy:**
   ```bash
   docker compose -p noorinalabs -f compose/docker-compose.prod.yml \
       --env-file .env logs --tail=40 kafka     # expect "Formatting ... with cluster.id <id>" then no InconsistentClusterId
   docker inspect --format='{{.State.Health.Status}}' noorinalabs-kafka-1   # expect: healthy
   ```

5. **Let `kafka-init` recreate the topics** (it runs automatically on `compose up`; it is idempotent and uses `--if-not-exists`):
   ```bash
   docker compose -p noorinalabs -f compose/docker-compose.prod.yml \
       --env-file .env up -d kafka-init kafka-ui kafka-exporter
   docker compose -p noorinalabs -f compose/docker-compose.prod.yml \
       --env-file .env exec kafka \
       /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --list
   ```
   Expect the 5 pipeline topics: `pipeline.raw.landed`, `pipeline.dedup.done`, `pipeline.enrich.done`, `pipeline.normalize.done`, `pipeline.dlq`.

6. **Confirm observability re-populates:** `kafka-exporter` scrapes the broker over the wire protocol; the Grafana `kafka-pipeline` dashboard broker/topic gauges should come back within a scrape interval. Consumer-group panels stay empty until ingest-platform workers run — that is expected (see `docs/pipeline-kafka.md` § Observability).

7. **Re-run the deploy** for the env to confirm `deploy-stg.yml` (then `deploy-prod.yml`) completes green end-to-end.

## Avoiding recurrence

- **Do not rotate `KAFKA_CLUSTER_ID`** once a volume is formatted. The value must stay stable for the life of the volume (`docs/pipeline-kafka.md` § Cluster ID). Rotating it forces this whole procedure. There is no application reason to rotate it — it is an identity, not a secret in the credential sense.
- A fresh VPS (empty `kafka_data`) needs no special handling: the broker formats on first boot with whatever `KAFKA_CLUSTER_ID` is in the env, and that becomes the stable id.

## Escalation

| Failure | Primary | Secondary |
|---|---|---|
| Broker still unhealthy after re-bootstrap on stg | Lucas.Ferreira (SRE) | Aisha.Idrissi (SRE) |
| Broker unhealthy on prod | Bereket.Tadesse (IM) | Lucas.Ferreira |
| `meta.properties` cluster-id mismatch detector mis-fires | Lucas.Ferreira | Weronika.Zielinska (Platform Architect) |
| Stray-non-topic-dir pre-flight mis-fires (flags a valid topic dir) | Nurul.Hakim (Observability) | Bereket.Tadesse (IM) |
| Topics do not recreate after wipe | Lucas.Ferreira | Nurul.Hakim (Observability) |

## Related issues

- **deploy#393** — staging deploy red on kafka healthcheck (cluster-id mismatch); this runbook authored alongside.
- **deploy#428** — prod kafka crash-loop on Bitnami-era stray `config/`/`data/` dirs in `kafka_data`; added the stray-non-topic-dir pre-flight (`scripts/kafka_logdir_preflight.sh`) + the § Variant section above.
- **deploy#100 / PR #385** — Bitnami → `apache/kafka:3.9.2` migration. Side-effects table item 1 deferred this re-bootstrap to a post-merge Test Plan step that was never executed — the proximate cause of both #393 and #428.
- **docs/pipeline-kafka.md** — broker topology, topic inventory, cluster-id stability requirement, observability.
