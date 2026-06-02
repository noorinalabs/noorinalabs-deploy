# Runbook: Kafka KRaft cluster-id re-bootstrap (deploy#393)

**Scope:** the pipeline Kafka broker (`kafka` service in `compose/docker-compose.prod.yml`) failing its healthcheck on a stg or prod deploy because the persisted `kafka_data` volume was formatted with a cluster ID that no longer matches the `KAFKA_CLUSTER_ID` the broker is started with. This is the documented post-migration step from the Bitnami → `apache/kafka:3.9.2` migration (deploy#100 / PR #385) that re-bootstraps the log directory under the new image's storage format.

**NOT in scope:**
- Routine deploys where the broker is already healthy — this runbook only applies to a cluster-id / storage-format mismatch.
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
   Expect the 5 pipeline topics: `pipeline.raw.new`, `pipeline.dedup.done`, `pipeline.enrich.done`, `pipeline.norm.done`, `pipeline.dlq`.

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
| Topics do not recreate after wipe | Lucas.Ferreira | Nurul.Hakim (Observability) |

## Related issues

- **deploy#393** — staging deploy red on kafka healthcheck; this runbook authored alongside.
- **deploy#100 / PR #385** — Bitnami → `apache/kafka:3.9.2` migration. Side-effects table item 1 deferred this re-bootstrap to a post-merge Test Plan step that was never executed — the proximate cause of #393.
- **docs/pipeline-kafka.md** — broker topology, topic inventory, cluster-id stability requirement, observability.
