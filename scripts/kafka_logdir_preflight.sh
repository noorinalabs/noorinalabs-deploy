#!/bin/sh
# kafka_logdir_preflight.sh — detect stray non-topic directories in a Kafka
# KRaft log-dir root (deploy#428).
#
# apache/kafka's LogManager fatally rejects ANY directory at the log-dir root
# that is not a Kafka topic-partition directory. A valid topic-partition dir is
# named "<topic>-<partition>" (partition = non-negative integer), optionally
# with a ".<uniqueId>-delete" or ".<uniqueId>-future" suffix for in-progress
# deletes / future replicas. Everything else — a Bitnami-era "config/", a nested
# "data/", an ext4 "lost+found/" — makes the broker log:
#
#   Found directory /var/lib/kafka/data/config, 'config' is not in the form of
#   topic-partition or topic-partition.uniqueId-delete ...
#   Kafka's log directories (and children) should only contain Kafka topic data.
#
# and crash-loop, which aborts the WHOLE compose stack on kafka-init's
# `depends_on: condition: service_healthy` — surfacing only as the opaque
# "container noorinalabs-kafka-1 is unhealthy". The deploy preflight in
# `.github/actions/write-deploy-env` runs this scanner against the persisted
# volume BEFORE `compose up` so the operator gets the diagnosis (and the
# re-bootstrap runbook pointer) up front instead of reverse-engineering it.
#
# Only DIRECTORIES are subject to the rule. Files kafka legitimately keeps at
# the log-dir root (meta.properties, bootstrap.checkpoint, *-offset-checkpoint,
# *.checkpoint, .lock, ...) are ignored — this scanner never flags a file.
#
# POSIX sh on purpose: it runs inside the apache/kafka image (which ships
# /bin/sh + busybox/coreutils grep, no bash) via
#   docker run --rm -i --entrypoint /bin/sh -v <vol>:/data <img> -s /data < this
# so it must not use bashisms. It is also unit-tested directly against fixture
# directories (scripts/tests/test_kafka_logdir_preflight.py).
#
# Usage: kafka_logdir_preflight.sh <log-dir>
# Exit codes:
#   0  clean — no stray non-topic directories (also: log-dir absent/empty, i.e.
#      a fresh volume the broker will format on boot)
#   1  usage error (no log-dir argument)
#   2  stray non-topic directories found (one per line on stdout)
set -eu

dir="${1:-}"
if [ -z "$dir" ]; then
	echo "usage: kafka_logdir_preflight.sh <log-dir>" >&2
	exit 1
fi

# A non-existent log dir is not an error: a fresh volume has no log dir yet and
# the broker formats it on first boot. Treat as clean.
if [ ! -d "$dir" ]; then
	exit 0
fi

stray=""
for entry in "$dir"/* "$dir"/.*; do
	# POSIX sh has no nullglob: an unmatched glob stays literal, so guard on
	# actual existence before testing.
	[ -e "$entry" ] || continue
	# Only directories are subject to kafka's topic-partition naming rule.
	[ -d "$entry" ] || continue
	name="${entry##*/}"
	# Skip the self/parent entries the ".*" glob always yields.
	case "$name" in
	. | ..) continue ;;
	esac
	# Valid topic-partition dir: ends in "-<digits>", optionally followed by a
	# ".<uniqueId>-delete" or ".<uniqueId>-future" suffix. This mirrors kafka's
	# LogManager parse rule — "config", "data", "lost+found" have no "-<digits>"
	# tail and are therefore rejected by the broker (and by us).
	if printf '%s\n' "$name" | grep -Eq '^.+-[0-9]+(\.[0-9A-Za-z_-]+-(delete|future))?$'; then
		continue
	fi
	stray="$stray $name"
done

if [ -n "$stray" ]; then
	echo "Stray non-topic directories in Kafka log-dir '$dir':"
	for s in $stray; do
		echo "  - $s"
	done
	echo "apache/kafka's LogManager will reject these and crash-loop the broker."
	exit 2
fi

exit 0
