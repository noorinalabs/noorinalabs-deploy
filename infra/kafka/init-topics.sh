#!/usr/bin/env bash
# Initialize pipeline topics on first boot. Idempotent — safe to re-run.
# Invoked by the kafka-init one-shot service in docker-compose.prod.yml.
#
# Topic retention (ms) comes from the pipeline design (issue #106):
#   pipeline.raw.new       7d   — upstream of dedup; replay window for missed events
#   pipeline.dedup.done    3d
#   pipeline.enrich.done   3d
#   pipeline.norm.done     3d
#   pipeline.dlq          30d   — failures across all workers; manual triage window
#
# Consumer-group naming convention: pipeline.<stage>.<worker-variant>
#   e.g. pipeline.dedup.v1, pipeline.enrich.v1
# Bumping the worker variant resets offsets without changing topic layout.

set -euo pipefail

BOOTSTRAP="${KAFKA_BOOTSTRAP:-kafka:9092}"
PARTITIONS="${KAFKA_DEFAULT_PARTITIONS:-3}"
REPLICATION="${KAFKA_REPLICATION_FACTOR:-1}"

MS_DAY=86400000

create_topic() {
    local name="$1"
    local retention_days="$2"
    local retention_ms=$(( retention_days * MS_DAY ))

    echo "kafka-init: ensuring topic ${name} (retention=${retention_days}d, partitions=${PARTITIONS})"

    # --if-not-exists makes creation idempotent. On subsequent boots this is a no-op.
    kafka-topics.sh \
        --bootstrap-server "${BOOTSTRAP}" \
        --create \
        --if-not-exists \
        --topic "${name}" \
        --partitions "${PARTITIONS}" \
        --replication-factor "${REPLICATION}" \
        --config "retention.ms=${retention_ms}" \
        --config "cleanup.policy=delete"

    # Reconcile retention when the topic already exists but retention has drifted
    # (e.g. we updated the desired value in this script). Safe no-op if unchanged.
    kafka-configs.sh \
        --bootstrap-server "${BOOTSTRAP}" \
        --entity-type topics \
        --entity-name "${name}" \
        --alter \
        --add-config "retention.ms=${retention_ms}" >/dev/null
}

wait_for_broker() {
    # Additional belt-and-braces — compose already waits for broker healthcheck,
    # but this script may be re-invoked manually against a cold cluster.
    local attempts=30
    while (( attempts > 0 )); do
        if kafka-broker-api-versions.sh --bootstrap-server "${BOOTSTRAP}" >/dev/null 2>&1; then
            return 0
        fi
        attempts=$(( attempts - 1 ))
        sleep 2
    done
    echo "kafka-init: broker at ${BOOTSTRAP} did not become reachable" >&2
    return 1
}

main() {
    wait_for_broker

    create_topic "pipeline.raw.new"       7
    create_topic "pipeline.dedup.done"    3
    create_topic "pipeline.enrich.done"   3
    create_topic "pipeline.norm.done"     3
    create_topic "pipeline.dlq"          30

    echo "kafka-init: topic inventory:"
    kafka-topics.sh --bootstrap-server "${BOOTSTRAP}" --list | sort
}

main "$@"
