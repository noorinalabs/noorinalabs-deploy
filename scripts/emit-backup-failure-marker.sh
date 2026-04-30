#!/usr/bin/env bash
# =============================================================================
# emit-backup-failure-marker.sh — OnFailure= hook for isnad-backup.service
#
# Emits two failure signals:
#   1. A journal line under SyslogIdentifier=BACKUP_FAILURE that Loki/Promtail
#      can match for alerting.
#   2. A node-exporter textfile-collector .prom file under /var/lib/node_exporter
#      so Prometheus picks up the failure timestamp on its next scrape.
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
TEXTFILE_DIR="/var/lib/node_exporter"
TEXTFILE="${TEXTFILE_DIR}/isnad_backup_failure.prom"

# Journal marker — distinct identifier for Loki alert rules.
echo "BACKUP_FAILURE: unit=${FAILED_UNIT} exited non-zero at ${NOW_ISO}" \
    | systemd-cat -t BACKUP_FAILURE -p err

# Textfile-collector metric — atomic write via mktemp + mv so prometheus
# never observes a half-written file mid-scrape.
install -d -m 0755 "$TEXTFILE_DIR"
TMP="$(mktemp "${TEXTFILE_DIR}/isnad_backup_failure.prom.XXXXXX")"
cat > "$TMP" <<EOF
# HELP isnad_backup_last_failure_timestamp_seconds Unix timestamp of the most recent isnad-backup failure.
# TYPE isnad_backup_last_failure_timestamp_seconds gauge
isnad_backup_last_failure_timestamp_seconds ${NOW_EPOCH}
EOF
chmod 644 "$TMP"
mv "$TMP" "$TEXTFILE"
