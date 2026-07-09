# Runbook — Backup alerts

Covers `BackupNeverSucceeded`, `BackupStale`, and `BackupFailed`
(`infra/prometheus/alerts.yml` § `backup`). Introduced by deploy#565.

## Background: why these three rules exist

A single rule, `BackupFailure`, previously covered this surface:

```yaml
expr: |
  isnad_backup_last_success_timestamp_seconds > 0
  and
  (time() - isnad_backup_last_success_timestamp_seconds) > 86400
```

Nothing ever wrote `isnad_backup_last_success_timestamp_seconds`. The `> 0`
guard therefore evaluated against an empty vector, and the expression was empty
forever. A backup that had **never succeeded** — the actual state of both hosts
for months (deploy#558) — was precisely the state this rule reported as healthy.
The guard was presumably meant to suppress noise before the first backup; it
also suppressed the alert permanently if the first backup never happened.

`promtool check rules` passed the whole time. It only proves the YAML parses.
The rules are now covered by `promtool test rules`
(`infra/prometheus/alerts_test.yml`, run in CI), which asserts both directions:
each rule fires on the state it claims to detect, and goes quiet on a healthy
one.

## Metric sources

Both gauges are node-exporter textfile-collector files in
`/var/lib/node_exporter/textfile_collector/` — the directory node-exporter is
pointed at via `--collector.textfile.directory` and the only one bind-mounted
into its container (`compose/docker-compose.prod.yml`).

| Metric | File | Written by |
|---|---|---|
| `isnad_backup_last_success_timestamp_seconds` | `isnad_backup_success.prom` | `scripts/backup.sh`, on a fully-successful run only |
| `isnad_backup_last_failure_timestamp_seconds` | `isnad_backup_failure.prom` | `scripts/emit-backup-failure-marker.sh`, via `isnad-backup.service`'s `OnFailure=` |

A successful run also removes the failure marker, so `BackupFailed` resolves on
the next scrape rather than lingering for its full 24h window.

> Before deploy#565 the failure marker wrote to `/var/lib/node_exporter/` — one
> level above the collector directory. It was never scraped. If you are
> debugging a missing metric, check the path first: a `.prom` file in the parent
> is invisible to Prometheus.

## Verifying the metrics on a host

```bash
ssh <host>
ls -l /var/lib/node_exporter/textfile_collector/
cat /var/lib/node_exporter/textfile_collector/isnad_backup_success.prom

# Confirm node-exporter is actually serving them (the collector only reads *.prom
# and the files must be world-readable — backup.sh runs under umask 077 and
# chmods explicitly for exactly this reason).
curl -s http://127.0.0.1:9100/metrics | grep isnad_backup
```

An empty `grep` here with the file present on disk means a permissions or path
problem, not an absent backup.

---

## BackupNeverSucceeded

**Severity:** critical. **Meaning:** node-exporter is up and scraping, but
`isnad_backup_last_success_timestamp_seconds` has never been written. There is
no restorable backup of this host.

The rule is guarded on `up{job="node-exporter"} == 1` so that the absence is a
real absence, not an artefact of a dead exporter. If node-exporter is down,
`ServiceDown` is the correct page and this one stays quiet.

**Triage:**

1. Has the timer ever run?
   ```bash
   systemctl status isnad-backup.timer
   systemctl list-timers isnad-backup.timer
   journalctl -u isnad-backup.service --no-pager -n 200
   ```
2. If the timer is inactive or absent, the host was never converged — see
   deploy#558 and `scripts/bootstrap-vps.sh` / `scripts/converge_host.sh`.
3. If the timer has run and failed, `BackupFailed` should also be firing.
   Triage that first; this alert clears once a complete backup lands.
4. Do **not** silence this alert to make the board green. It is reporting the
   literal truth that a restore is currently impossible.

## BackupStale

**Severity:** critical. **Meaning:** a backup succeeded at some point, but not
within the last 24 hours. The daily timer (`OnCalendar=*-*-* 03:00:00 UTC`,
`RandomizedDelaySec=300`) either did not run or did not finish.

**Triage:**

1. `systemctl list-timers isnad-backup.timer` — is the next elapse sane?
2. `journalctl -u isnad-backup.service --since '48 hours ago'` — did the last
   run abort partway? `backup.sh` exits non-zero on a *partial* backup (one
   store dumped, another failed) and deliberately does **not** advance the
   success timestamp, so a run that "mostly worked" still surfaces here.
3. Check disk on the staging dir: `df -h /var/lib/noorinalabs-backups`.

## BackupFailed

**Severity:** critical. **Meaning:** `isnad-backup.service` exited non-zero
within the last 24 hours and no later success has replaced it.

**Triage:**

1. ```bash
   journalctl -u isnad-backup.service -n 100 --no-pager
   journalctl -t BACKUP_FAILURE -n 20 --no-pager
   ```
2. Most common causes, in order:
   - **Missing/invalid B2 credentials.** `backup.sh` requires `B2_KEY_ID`,
     `B2_APP_KEY`, `B2_BUCKET` and fails its preflight without them. See
     deploy#559.
   - Neo4j did not stop within 30s, so the offline dump was skipped.
   - Upload to B2 failed (network, bucket policy, key capability).
   - Partial dump — see `BackupStale` triage step 2.
3. Fix forward and re-run manually to confirm:
   ```bash
   systemctl start isnad-backup.service
   journalctl -u isnad-backup.service -f
   ```
   A successful run writes the success gauge and deletes the failure marker;
   both alerts resolve within one scrape interval.

## Testing a change to these rules

Never merge an alert-rule change on the strength of `promtool check rules`
alone. Add or update a case in `infra/prometheus/alerts_test.yml` and run:

```bash
docker run --rm --entrypoint promtool \
  -v "$PWD/infra/prometheus:/etc/prometheus:ro" -w /etc/prometheus \
  prom/prometheus:v3.4.0 test rules alerts_test.yml
```

Then confirm the test is not vacuous: break the rule on purpose and check that
the test fails. A test that passes against a rule that cannot fire is worse than
no test, because it launders the absence of coverage into a green check.

CI runs both `check rules` and `test rules` in the `promtool check (rules +
config)` job of `.github/workflows/compose-validate.yml`, mirrored locally by
the `promtool-check-rules` / `promtool-test-rules` pre-commit hooks.
