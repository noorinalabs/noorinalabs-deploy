# Runbook — Backup alerts

Covers two **different layers** of backup signal. They fail independently, and
the second one exists because the first cannot see the failure that matters
most.

| Layer | Signals | Reads | Section |
|---|---|---|---|
| Host-local gauges | `BackupNeverSucceeded`, `BackupStale`, `BackupFailed` (`infra/prometheus/alerts.yml` § `backup`) | a node-exporter textfile gauge **on the VPS** — i.e. that `backup.sh` RAN and `rclone` returned 0 | [below](#backupneversucceeded) (deploy#565) |
| The bucket itself | `BackupArtifactCheck` (`.github/workflows/verify-backup-artifact.yml`) | **`rclone lsf` against B2** — is there actually a restorable object in there? | [`BackupArtifactCheck`](#backupartifactcheck) (deploy#583) |

**The whole Prometheus stack above can be green while the bucket is empty.** The
gauges are the uploader's own opinion of its upload; nothing on the host looks in
the bucket, and a host that is down cannot report that its own backup is missing.
That is what `BackupArtifactCheck` is for, and it is why that signal is a failing
CI job rather than a metric — a metric would have to be scraped from the very
machine whose absence we are trying to detect.

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

---

## BackupArtifactCheck

**Severity:** critical. **Signal:** a failing run of `Verify backup artifact (B2)`
(`.github/workflows/verify-backup-artifact.yml`), nightly at 05:30 UTC over a
`[stg, prod]` matrix. **Script:** `scripts/verify_b2_backup_artifact.sh`.

This is the **only** check that looks inside the bucket. It asserts a
*restorable object exists* — not merely that a backup process ran.

The job's one machine-readable line:

```text
B2_BACKUP_ARTIFACT status=… reason=… dumps=N newest_age_hours=N bucket_objects=N undersized=N torn=N newest=…
```

Exit codes, and what each one licenses you to conclude:

| rc | status | Conclusion |
|---|---|---|
| 0 | `fresh` | A restorable backup exists and is recent. **Still check `torn`** — see [`TornBackup`](#tornbackup). |
| 1 | `absent` / `stale` / `incomplete` | **ALERT.** No fresh restorable backup object. The host-local gauges may well be green; they do not look in the bucket. |
| 2 | `instrument_error` | **We could not look.** This is *not* a claim that backups are missing. Fix the scan, then re-read. |

That third state is load-bearing. An empty `rclone lsf` listing exits 0 and prints
nothing — so "no backups" and "I cannot see the bucket" would otherwise be the same
string, and only one of them is a measurement.

### Reason codes — meaning and action

Ordered by status. `MAX_AGE_HOURS` defaults to 30 (one nightly plus slack);
`MIN_DUMP_BYTES` to 1024; the future-clock tolerance to 300s.

| status | reason | Meaning | Action |
|---|---|---|---|
| `fresh` | — | The newest attested run declares `complete=true`, all three required stores arrived, none is under the size floor, and it is within the age bound. | Nothing — **unless `torn>0`**, which is a producer fault on a green run. See [`TornBackup`](#tornbackup). |
| `stale` | `too_old` | A complete, intact run exists, but its newest dump is older than `MAX_AGE_HOURS`. The nightly has stopped landing. | Same triage as [`BackupStale`](#backupstale): `systemctl list-timers isnad-backup.timer`, then `journalctl -u isnad-backup.service --since '48 hours ago'`. |
| `absent` | `no_dumps` | Bucket reachable; **not one** dump object under the scanned scope. | **Check `bucket_objects` first.** If it is `> 0` the bucket has contents but nothing under the scanned prefix — a wrong `BACKUP_B2_BUCKET`/`B2_PREFIX` reads as `absent`, so verify the path before concluding data loss. If it is `0`, backups have never landed: triage as [`BackupNeverSucceeded`](#backupneversucceeded). |
| `absent` | `undersized_dumps` | Dumps exist, but **every one** is below `MIN_DUMP_BYTES`. A 0-byte file is what `pg_dump` leaves when it fails *after* the shell has created the target — it then uploads cleanly and `rclone` returns 0. | The **dump** leg is failing, not the upload leg. `journalctl -u isnad-backup.service`; check disk on `/var/lib/noorinalabs-backups`. |
| `incomplete` | `no_complete_backup` | Sized dumps exist, but **not one run attests `complete=true`** — including the case of no manifest at all (every pre-deploy#559 artifact). | A leg is failing every night. `backup.sh` uploads partials by design and honestly marks them `complete=false`; find which store is failing in the journal. |
| `incomplete` | `missing_stores,<list>` | The newest **torn** run attested `complete=true` but the named stores never arrived — **and no older run qualified either**. Producer's word and bucket disagree. | The upload was interrupted after the manifest object landed. The listed store(s) are what to chase. Nothing is restorable — this is the emergency form of [`TornBackup`](#tornbackup). |
| `incomplete` | `undersized_dumps` | A run attested `complete=true` but carries a dump under the floor — **attested *and* corrupt** — and nothing older qualified. | **Not the same fault as the `absent` row above, despite the identical reason string.** There, no dump is restorable anywhere; here, the *attested* run is corrupt. `complete=true` is the producer's word about what it *wrote*, and it cannot see a truncated file. Fix the producer. |
| `incomplete` | `manifest_ts_mismatch` | `_backup_manifest-<TS>.txt` declares a `timestamp=` that is **not** the `<TS>` it is filed under (or declares none at all), so it cannot attest the run it is named for and is refused. Nothing older qualified. | A producer bug in `backup.sh`'s manifest write. **Do not hand-edit the manifest to clear this** — that binding is the only thing preventing a torn restore (dumps of run X certified by an attestation about run Y). |
| `instrument_error` | `unreachable` | `rclone lsf` on the bucket (or the prefix) failed outright. Credentials, network, or a wrong bucket name. **Nothing is known about the backups.** | Not a backup alert — do not triage backups. Check `BACKUP_B2_KEY_ID` / `BACKUP_B2_APP_KEY` / `BACKUP_B2_BUCKET` for that environment; the job prints `rclone`'s stderr. Classify by **capability probe**, never by `rclone`'s message text — a read-only key's 401 reads as "failed to create bucket" (deploy#559). |
| `instrument_error` | `manifest_unreadable` | The manifest object could not be **read** — a transient 401, a throttle, a network blip. "I could not read it" is **not** "it does not attest". | Re-run the job; check credentials. Do **not** read this as an incomplete backup: the dumps may be perfectly fine. |
| `instrument_error` | `future_timestamp` | **THE VPS CLOCK IS WRONG. THIS IS NOT A BACKUP PROBLEM.** `rclone` preserves the source mtime, so an object's timestamp *is* the VPS clock at dump time. A timestamp more than 300s in the future means the box's clock is ahead. | **Fix NTP on the VPS.** `timedatectl status` on the box; confirm `systemd-timesyncd` (or `chrony`) is running and synchronized. **Left alone this latches:** `newest` is selected by max epoch, so a single future-dated object masks an entire stale bucket — and a one-sided age bound would report it `fresh` forever, permanently greening the one signal we built this job to trust. |

> **`bucket_objects` is the prefix discriminator.** An `absent` verdict with
> `bucket_objects > 0` means "the bucket has contents, just not where I looked" —
> check the prefix rather than hunting for a lost backup. On B2 a nonexistent
> *prefix* is indistinguishable by exit code from an empty bucket (`lsf -R` returns
> rc=0 and no output for both), which is why the reachability probe is on the
> **bucket** and this field exists.

### Reading the job

The scan self-tests against local fixtures before every real reading and refuses
to report a verdict from an uncalibrated scanner: a zero from a broken scanner and
a zero from an empty bucket are the same string. If the self-test fails, the job
exits 2 — an instrument error, not a backup verdict.

```bash
# The scanner, against fixtures — no B2 access needed. This is what the PR job runs.
bash scripts/verify_b2_backup_artifact.sh --self-test

# Against a real bucket (needs the read-scoped B2 key in the environment).
B2_ROOT="isnad:${B2_BUCKET}" bash scripts/verify_b2_backup_artifact.sh
```

**Never set `RCLONE_DUMP`.** The script refuses to run with it set, deliberately:
`rclone` would echo the `Authorization: Basic <base64>` header, and GitHub's secret
masking is an exact-substring match on the raw secret, so it does **not** catch the
base64 form. This job's output goes to a public log.

## TornBackup

**Severity:** high. **Signal:** `torn=N` with `N > 0` on the `B2_BACKUP_ARTIFACT`
result line — **including on a run that exits 0 and reports `status=fresh`.**

> **This is the one outcome here that is a fault on a job that PASSED.** Every other
> row above is reason→action off a non-zero exit code. This one is reason→action off
> a **green** run, and it is the reason to read the summary table of a job that
> succeeded.

### `skipped` vs `torn` — only the disagreement is a fault

| | Producer's word | The bucket | Verdict |
|---|---|---|---|
| **`skipped`** | `complete=false` — the run does **not** attest | dumps incomplete | **They agree.** An ordinary nightly partial; `backup.sh` emits these *by design* ("a partial backup beats none"). **Not a fault.** Logged as a `skipped` count, never counted in `torn`. |
| **`torn`** | `complete=true` — the run **attests** | dumps **not** all there | **They disagree.** The producer claimed completeness and lied. **This is the fault.** |

The two cannot collapse into each other: all three `torn` increments sit
*downstream* of the attests-complete gate, so a run that never attested can never
be counted torn. A run is counted torn when, having attested `complete=true`, it:

1. is **missing a required store** (`isnad-pg` / `isnad-userpg` / `isnad-neo4j`) —
   the upload was interrupted after the manifest object landed;
2. carries an **undersized dump** — attested *and* corrupt; or
3. has a **manifest timestamp** that disagrees with the run id it is filed under.

### Why the job is still green — and why it is still a fault

The scanner keeps looking, finds an *older* run that is complete and intact, and
exits 0. That is **correct**: a restorable backup does exist and `restore.sh latest`
will find it. Reporting "no complete backup" over a bucket that holds one is exactly
the false alarm this check exists to avoid.

But it has just observed a run that **claimed completeness and did not deliver**.
That is a defect in `backup.sh`'s attestation path, it will recur tonight, and today
it merely scrolls past in a log. `torn=1` on a green run means *tonight's backup tore
and yesterday's saved you.*

**Triage — treat `torn > 0` as a producer bug regardless of the exit code:**

1. Read the job log. The scanner logs a `WARNING` naming the torn run, its
   directory, and which store is missing / undersized / mismatched.
2. On the box: `journalctl -u isnad-backup.service --since '48 hours ago'`. The tear
   is between "`backup.sh` wrote the manifest" and "`rclone` finished the copy" —
   the manifest is written locally and the whole directory is copied afterwards, so a
   copy interrupted after the manifest object lands leaves exactly this in B2.
3. **The fix is in the producer** (`scripts/backup.sh`), not in the bucket. Do not
   delete the torn run to clear the field: you would be deleting the evidence, and it
   will be back tomorrow.

### `torn>0` vs `status=incomplete` — same faults, different emergency

The three faults above are the *same* three that produce the `incomplete` reason
codes. The difference is **whether an older run saved you**, and it decides how fast
you have to move:

| Observed | What it means | Urgency |
|---|---|---|
| `status=fresh` … `torn=1` | An older run **is** restorable. | Fix the producer. Not an outage. |
| `status=incomplete reason=missing_stores,…` | **Nothing is restorable.** The torn run's reason becomes the verdict precisely because nothing else qualified. | **Emergency.** A restore is currently impossible. |

> **Known gap — nothing pages on `torn` yet.** The job exits 0, and no Prometheus
> metric carries the field: the signal reaches a human only via the step summary and
> the run's warning annotation. Closing that needs a page-able carrier, which is a
> separate change from this runbook. Until then, **read the summary of a green run.**

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
