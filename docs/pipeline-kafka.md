# Pipeline Kafka (KRaft)

Single-broker Kafka running in KRaft mode (no ZooKeeper) that carries stage-to-stage pointers for the data ingestion pipeline. Deployed via `compose/docker-compose.prod.yml`; topics are provisioned by `infra/kafka/init-topics.sh`.

Consumers live in `noorinalabs-isnad-ingest-platform` (see `#107`). This repo only owns the broker + topic layout.

## Topic inventory

| Topic | Retention | Purpose |
|---|---|---|
| `pipeline.raw.new` | 7d | A new file has landed in B2 `raw/{source}/{date}/`. Fan-in for dedup worker. |
| `pipeline.dedup.done` | 3d | Dedup finished → `dedup/{source}/{batch-id}/`. Consumed by enrich. |
| `pipeline.enrich.done` | 3d | Enrichment finished → `enriched/{source}/{batch-id}/`. Consumed by normalize. |
| `pipeline.norm.done` | 3d | Normalization finished → `normalized/{batch-id}/`. Consumed by graph-load. |
| `pipeline.dlq` | 30d | Dead-letter queue. Any worker failure lands here with the original message plus `error_code` / `error_stage` headers. Manual triage. |

Longer retention on `raw.new` gives operators a replay window after a downstream stage is fixed. DLQ retention is long enough to diagnose intermittent issues without filling disk.

Defaults for every topic: 3 partitions, replication factor 1, `cleanup.policy=delete`. Retention is enforced by the init script — Kafka UI runs in read-only mode (`KAFKA_UI_READONLY=true`) so operators cannot drift retention out of source control.

## Message schema

Messages are lightweight pointers, not payloads. Workers fetch the actual data from B2 using `b2_path`.

```json
{
  "batch_id": "uuid",
  "source": "sunnah-api",
  "b2_path": "raw/sunnah-api/2026-04-13/hadiths.parquet",
  "timestamp": "ISO8601",
  "record_count": 1234
}
```

On failure, a worker republishes the inbound message to `pipeline.dlq` unchanged, and adds Kafka record headers:

| Header | Example |
|---|---|
| `error_stage` | `dedup`, `enrich`, `normalize`, `graph_load` |
| `error_code` | `schema_mismatch`, `b2_not_found`, `worker_panic` |
| `error_message` | Free-form, truncated to 2KB |
| `failed_at` | ISO 8601 timestamp |

## Consumer-group naming

`pipeline.<stage>.<variant>`

Examples: `pipeline.dedup.v1`, `pipeline.enrich.v1`, `pipeline.graph-load.v1`.

The `<variant>` suffix lets us deploy a breaking worker change by bumping the variant (`v1` → `v2`). The old group's offsets are left intact so a rollback is a plain container swap — no Kafka-side surgery. Variant bumps MUST be paired with a runbook entry and coordinated with `#108` pipeline-reset semantics.

**Do not** use generic names like `dedup` or `hostname`-based groups. The `pipeline.` prefix is used by monitoring to scope alerts.

## Operator access

Kafka UI is bound to `127.0.0.1:8085` on the VPS. Reach it via SSH tunnel:

```
ssh -L 8085:127.0.0.1:8085 deploy@<vps>
# then open http://localhost:8085 locally, log in with KAFKA_UI_{USER,PASSWORD}
```

Direct broker access (for `kafka-*.sh` CLI debugging) is possible from the VPS host:

```
docker compose -f compose/docker-compose.prod.yml exec kafka \
    kafka-topics.sh --bootstrap-server kafka:9092 --list
```

The broker listener is on the `backend` Docker network (internal). It is not reachable from the host or the public internet.

## Cluster ID

`KAFKA_CLUSTER_ID` is required and must remain stable — changing it after the first boot will cause the broker to refuse to start against existing log directories. Generate once at VPS bootstrap time:

```
docker run --rm bitnami/kafka:3.8.0 kafka-storage.sh random-uuid
```

Store the output in the production `.env` file alongside the other secrets.

## Follow-ups

- **JMX / Prometheus scraping:** not wired in this PR. When consumers land (`#107`), add a `kafka-exporter` sidecar and a `kafka` scrape job to `infra/prometheus/prometheus.yml`. Tracked as part of the observability polish pass.
- **Multi-broker / replication-factor > 1:** single-broker is acceptable for the ingest pipeline (stateless reprocessing from B2 is always possible). A move to 3-broker is a Phase 3 concern.
- **Schema registry:** the pointer payload is stable and small enough that a registry is overkill today. Revisit if a new producer joins.
