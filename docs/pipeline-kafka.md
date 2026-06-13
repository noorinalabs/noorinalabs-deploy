# Pipeline Kafka (KRaft)

Single-broker Kafka running in KRaft mode (no ZooKeeper) that carries stage-to-stage pointers for the data ingestion pipeline. Deployed via `compose/docker-compose.prod.yml`; topics are provisioned by `infra/kafka/init-topics.sh`.

Consumers live in `noorinalabs-isnad-ingest-platform` (see `#107`). This repo only owns the broker + topic layout.

## Topic inventory

| Topic | Retention | Purpose |
|---|---|---|
| `pipeline.raw.landed` | 7d | A new file has landed in B2 `raw/{source}/{date}/`. Fan-in for dedup worker. |
| `pipeline.dedup.done` | 3d | Dedup finished → `dedup/{source}/{batch-id}/`. Consumed by enrich. |
| `pipeline.enrich.done` | 3d | Enrichment finished → `enriched/{source}/{batch-id}/`. Consumed by normalize. |
| `pipeline.normalize.done` | 3d | Normalization finished → `normalized/{batch-id}/`. Consumed by graph-load. |
| `pipeline.dlq` | 30d | Dead-letter queue. Any worker failure lands here with the original message plus `error_code` / `error_stage` headers. Manual triage. |

Topic names are the canonical set from the ingest-platform `workers/lib/topics.py` (naming locked in ip#192). They were reconciled here in deploy#440 — the prior `pipeline.raw.new` / `pipeline.norm.done` names predated that rename, and the deployed workers (which consume `pipeline.raw.landed` / `pipeline.normalize.done`) would never have found their topics with auto-create disabled.

Longer retention on `raw.landed` gives operators a replay window after a downstream stage is fixed. DLQ retention is long enough to diagnose intermittent issues without filling disk.

Defaults for every topic: 3 partitions, replication factor 1, `cleanup.policy=delete`. Retention is enforced by the init script — Kafka UI runs in read-only mode (`KAFKA_UI_READONLY=true`) so operators cannot drift retention out of source control.

Retention values are reapplied on every deploy via `kafka-init`, so out-of-band UI tweaks are intentionally overwritten.

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
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --list
```

The broker listener is on the `backend` Docker network (internal). It is not reachable from the host or the public internet.

## Cluster ID

`KAFKA_CLUSTER_ID` is required and must remain stable — changing it after the first boot will cause the broker to refuse to start against existing log directories. Generate once at VPS bootstrap time:

```
docker run --rm apache/kafka:3.9.2 /opt/kafka/bin/kafka-storage.sh random-uuid
```

(The broker runs on the official `apache/kafka` image — migrated off Bitnami in #100. The previous `bitnami/*` namespace was sunset Aug 2025 and the `bitnamilegacy/*` stopgap was itself on a sunset trajectory. The compose service supplies this value to the image as `CLUSTER_ID`, which the entrypoint uses to format the log dir on first boot.)

Store the output in the production `.env` file alongside the other secrets.

## Observability

Prometheus scraping is wired via the `kafka-exporter` sidecar (`compose/docker-compose.prod.yml`) on the `backend` network. The exporter exposes `/metrics` on `:9308`; the `kafka` scrape job in both `infra/prometheus/prometheus.{prod,stg}.yml` collects:

- Broker liveness (`up{job="kafka"}`, `kafka_brokers`) → drives `KafkaBrokerDown` (critical, 1m)
- Per-topic partition/ISR gauges → `kafka_topic_partitions`, `kafka_topic_partition_in_sync_replica`
- Per-topic producer rate (derived) → `rate(kafka_topic_partition_current_offset[5m])`
- Per-consumer-group offset/lag → `kafka_consumergroup_current_offset`, `kafka_consumergroup_lag` → drives `KafkaConsumerGroupLagHigh` (warning, 10k for 10m)
- DLQ growth → `KafkaDLQGrowing` (warning, growth-rate>0 for 15m)

Grafana dashboard: `infra/grafana/dashboards/kafka-pipeline.json` (uid `kafka-pipeline`).

Consumer-group metrics populate only when ingest-platform consumer workers are running — the dashboard panels for consumer rate/lag render empty-state until then, and that is intentional. Broker/topic/partition gauges are useful immediately.

## Follow-ups

- **JMX scraping:** not wired. The kafka-exporter sidecar covers the broker/topic/consumer-group surface for current pipeline needs; JMX-direct path is heavier and only worth the complexity if a future enrichment workload needs JVM-internal GC/heap visibility. Defer until kafka-exporter proves insufficient.
- **Consumer-lag empty-state validation pass:** when ingest-platform consumer workers (`pipeline.<stage>.<variant>` groups) are deployed, run a one-time validation that the Consumer Lag / Consumer Rate panels populate end-to-end. Tracked separately — see the W12 tracker filed alongside deploy#88.
- **Multi-broker / replication-factor > 1:** single-broker is acceptable for the ingest pipeline (stateless reprocessing from B2 is always possible). A move to 3-broker is a Phase 3 concern.
- **Schema registry:** the pointer payload is stable and small enough that a registry is overkill today. Revisit if a new producer joins.
