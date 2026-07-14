#!/usr/bin/env bash
# =============================================================================
# emit-backup-failure-marker.sh — OnFailure= hook for isnad-backup.service
#
# Emits two failure signals:
#   1. A journal line under SyslogIdentifier=BACKUP_FAILURE that Loki/Alloy
#      can match for alerting.
#   2. A node-exporter textfile-collector .prom file under
#      /var/lib/node_exporter/textfile_collector so Prometheus picks up the
#      failure timestamp on its next scrape. (Before deploy#565 this wrote to
#      the PARENT directory, which node-exporter does not read — the metric was
#      emitted faithfully and scraped never.)
#
# Invoked by isnad-backup-failure-marker.service when isnad-backup.service
# exits non-zero. The lifted-out helper script is here (rather than inline in
# the unit's ExecStart=) because the inline %% / $$ quoting around systemd
# specifiers and shell-arithmetic-pid bites very easily — see deploy#121
# Bug D for the rendered "/usr/bin/bash" instead of an integer-epoch the
# inline form produced.
#
# Environment (set by systemd for OnFailure= triggers):
#   MONITOR_UNIT       — full unit name of the failed parent (e.g. isnad-backup.service)
#   MONITOR_EXIT_CODE  — exit code, when applicable (oneshot exit-code reads as exit_code/value)
#
# Both fall back to "unknown" if the script is invoked directly (manual test).
# =============================================================================
set -euo pipefail

FAILED_UNIT="${MONITOR_UNIT:-unknown}"
NOW_ISO="$(date -u --iso-8601=seconds)"
NOW_EPOCH="$(date -u +%s)"
# node-exporter is started with
# `--collector.textfile.directory=/var/lib/node_exporter/textfile_collector` and
# bind-mounts ONLY that subdirectory (compose/docker-compose.prod.yml). Writing
# to the parent — as this script did until deploy#565 — produced a .prom file
# that no collector ever read, so the failure metric never reached Prometheus.
TEXTFILE_DIR="/var/lib/node_exporter/textfile_collector"
TEXTFILE="${TEXTFILE_DIR}/isnad_backup_failure.prom"

# Journal marker — distinct identifier for Loki alert rules.
echo "BACKUP_FAILURE: unit=${FAILED_UNIT} exited non-zero at ${NOW_ISO}" \
    | systemd-cat -t BACKUP_FAILURE -p err

# Textfile-collector metric — atomic write via mktemp + mv so prometheus
# never observes a half-written file mid-scrape. The parent is named explicitly
# (`-p`) rather than implied by the template: see the note at backup.sh's
# emit_success_metric — in a hardened script, every mktemp names its parent.
install -d -m 0755 "$TEXTFILE_DIR"
TMP="$(mktemp -p "$TEXTFILE_DIR" isnad_backup_failure.prom.XXXXXX)"
cat > "$TMP" <<EOF
# HELP isnad_backup_last_failure_timestamp_seconds Unix timestamp of the most recent isnad-backup failure.
# TYPE isnad_backup_last_failure_timestamp_seconds gauge
isnad_backup_last_failure_timestamp_seconds ${NOW_EPOCH}
EOF
chmod 644 "$TMP"
mv "$TMP" "$TEXTFILE"
