"""Cross-file invariant tests for the backup textfile-collector metrics (deploy#565).

Two producers write node-exporter textfile metrics for the backup path:

    scripts/backup.sh ....................... isnad_backup_last_success_timestamp_seconds
    scripts/emit-backup-failure-marker.sh ... isnad_backup_last_failure_timestamp_seconds

and one consumer reads them: the node-exporter container, pointed at a directory
by ``--collector.textfile.directory`` in compose/docker-compose.prod.yml, which
also bind-mounts *only* that directory.

Before deploy#565 the failure marker wrote one level ABOVE the collector
directory. The file was produced faithfully, every run, and scraped never. No
test existed that compared the producer's destination against the consumer's
source, so the two drifted silently and the failure metric reached Prometheus
zero times.

These tests pin that invariant. They are deliberately static-analysis over the
real files rather than a mocked harness: the bug was *in the literal path
string*, so a fixture that supplied its own path would prove nothing (a guard
assertion no fixture can violate is inert). The assertions read the shipped
scripts and the shipped compose file, and would have failed on the pre-#565
tree.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKUP_SH = REPO_ROOT / "scripts" / "backup.sh"
FAILURE_MARKER_SH = REPO_ROOT / "scripts" / "emit-backup-failure-marker.sh"
COMPOSE_PROD = REPO_ROOT / "compose" / "docker-compose.prod.yml"
BOOTSTRAP_SH = REPO_ROOT / "scripts" / "bootstrap-vps.sh"

SUCCESS_METRIC = "isnad_backup_last_success_timestamp_seconds"
FAILURE_METRIC = "isnad_backup_last_failure_timestamp_seconds"


def _textfile_dir_assignment(script: Path) -> str:
    """The directory a script will actually write metrics to WHEN PRODUCTION RUNS IT.

    Accepts either form, and returns the same thing for both — the path used when nothing in
    the environment overrides it:

        TEXTFILE_DIR="/var/lib/node_exporter/textfile_collector"
        TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}"

    backup.sh took the second form so a test sandbox can point the gauge somewhere writable
    and OBSERVE what a run leaves on the box (deploy#618 review: the run that dumps everything
    and then fails to restart Neo4j used to exit 0 and `rm -f` its own failure marker — you
    cannot assert on that without a writable textfile dir). Nothing in production sets the
    variable: the systemd unit passes no such env and grants ReadWritePaths=/var/lib/node_exporter.

    So what this helper must return is the DEFAULT, not the literal text of the assignment.
    Returning the raw `${TEXTFILE_DIR:-...}` string would make the two assertions below compare
    a shell expression against a path and fail — while the property they exist to protect (the
    metric lands where node-exporter scrapes) is still perfectly true.
    """
    text = script.read_text()
    match = re.search(
        r'^TEXTFILE_DIR="(?:\$\{TEXTFILE_DIR:-(?P<default>[^}"]+)\}|(?P<literal>[^"$]+))"',
        text,
        re.MULTILINE,
    )
    assert match, f"{script.name} does not assign TEXTFILE_DIR"
    return match.group("default") or match.group("literal")


def _collector_directory_flag() -> str:
    """Extract node-exporter's --collector.textfile.directory=<path> from compose."""
    match = re.search(r"--collector\.textfile\.directory=([^\"'\s]+)", COMPOSE_PROD.read_text())
    assert match, "compose does not set --collector.textfile.directory"
    return match.group(1)


def test_collector_directory_is_configured() -> None:
    """The consumer side must actually declare a textfile directory."""
    assert _collector_directory_flag() == "/var/lib/node_exporter/textfile_collector"


def test_failure_marker_writes_into_the_scraped_directory() -> None:
    """The regression that motivated deploy#565.

    emit-backup-failure-marker.sh wrote to /var/lib/node_exporter — the PARENT of
    the directory node-exporter reads. A .prom file there is never scraped.
    """
    assert _textfile_dir_assignment(FAILURE_MARKER_SH) == _collector_directory_flag()


def test_backup_writes_success_metric_into_the_scraped_directory() -> None:
    """The success gauge must land where node-exporter will actually read it."""
    assert _textfile_dir_assignment(BACKUP_SH) == _collector_directory_flag()


def test_backup_emits_the_success_metric_the_alert_keys_on() -> None:
    """backup.sh must write the exact metric name BackupStale/BackupNeverSucceeded use.

    The pre-#565 alert keyed off a metric no producer ever wrote. Asserting the
    literal name here ties producer and alert together.
    """
    body = BACKUP_SH.read_text()
    assert f"{SUCCESS_METRIC} " in body, "backup.sh does not emit the success gauge"
    assert "# TYPE isnad_backup_last_success_timestamp_seconds gauge" in body


def test_failure_marker_emits_the_failure_metric_the_alert_keys_on() -> None:
    body = FAILURE_MARKER_SH.read_text()
    assert f"{FAILURE_METRIC} " in body
    assert "# TYPE isnad_backup_last_failure_timestamp_seconds gauge" in body


def test_success_metric_is_world_readable() -> None:
    """backup.sh runs under `umask 077`; without an explicit chmod the .prom file
    is 0600 and node-exporter (unprivileged uid, read-only bind mount) cannot
    read it. The metric would be written and silently never scraped."""
    body = BACKUP_SH.read_text()
    assert "umask 077" in body, "precondition changed — re-check the chmod rationale"
    assert re.search(r"chmod 644 \"\$tmp\"", body), "success .prom is not chmod 644"


def test_success_metric_written_atomically() -> None:
    """temp + rename, and the temp name must not end in .prom or the collector
    will scrape a half-written file."""
    body = BACKUP_SH.read_text()
    assert 'mktemp "${SUCCESS_TEXTFILE}.XXXXXX"' in body
    assert 'mv "$tmp" "$SUCCESS_TEXTFILE"' in body


def test_success_clears_the_failure_marker() -> None:
    """A successful run removes the failure .prom so BackupFailed resolves on the
    next scrape rather than lingering for its full 24h recency window."""
    assert 'rm -f "$FAILURE_TEXTFILE"' in BACKUP_SH.read_text()


def test_success_metric_not_emitted_on_partial_backup() -> None:
    """A partial backup exits non-zero BEFORE emit_success_metric is reached.

    Advancing the success timestamp on a partial dump would silence BackupStale
    while no complete, restorable backup exists.
    """
    body = BACKUP_SH.read_text()
    partial_exit = body.index('log "ERROR" "Partial backup')
    emit_call = body.rindex("\nemit_success_metric")
    assert partial_exit < emit_call, "emit_success_metric must follow the partial-backup exit"
    # And the guard must actually be an exit, not a warning.
    assert 'log "ERROR" "Partial backup — success metric NOT emitted"\n    exit 1' in body


def test_bootstrap_provisions_the_scraped_directory() -> None:
    """A bootstrapped (non-cloud-init) host must get the collector SUBDIR, not
    just its parent — otherwise the first .prom write lands in an unscraped dir."""
    assert (
        "install -d -m 0755 /var/lib/node_exporter/textfile_collector" in BOOTSTRAP_SH.read_text()
    )
