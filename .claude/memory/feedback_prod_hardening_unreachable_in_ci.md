---
name: feedback_prod_hardening_unreachable_in_ci
description: CI's /tmp is writable and the hardened unit's is READ-ONLY — the whole test surface runs where the bug cannot happen; derive the sandbox from the unit file, never restate it.
metadata:
  type: feedback
---

**`systemd/isnad-backup.service` runs `ProtectSystem=strict` with `PrivateTmp` deliberately
unset (deploy#121 Bug A). `/tmp` is READ-ONLY on the host.** `ReadWritePaths=` grants only
`/var/lib/noorinalabs-backups`, `/opt/noorinalabs-deploy`, `/var/lib/node_exporter`.

A bare `mktemp` defaults to `/tmp`. **GitHub Actions runners, the rehearsal containers, and the
dev box all have a writable `/tmp`.** So the entire test surface — including the e2e tests that
drive the real `backup.sh` — runs in an environment where this class of bug **cannot occur**.
The tests were never weak. They were *structurally incapable of failing*.

**Why:** this is why backups NEVER once succeeded in this project's history, and why the same
bug shipped twice past green CI and four reviewers. Each instance was **a check that could not
run, reporting its own breakage as a fact about the system it was checking** — pointing the
operator at the wrong subsystem every time:

* **deploy#613** — `b2_preflight.sh` couldn't allocate scratch → `verdict=KEY_INVALID`. The B2
  key was perfectly good. Every backup ever attempted died here, blaming a credential.
* **deploy#617** — `backup.sh` addressed a phantom compose project (no `-p`; the *directory
  basename* decides). `restore.sh` had it too: a restore would have loaded into a **stray
  volume** and reported **success**. The rehearsal could never catch it — it supplied
  `COMPOSE_PROJECT_NAME`, the one input production omits.
* **deploy#623** — `compose_project.sh` reintroduced #613 → `Cannot reach Docker Compose — is
  the daemon running?` The daemon was healthy. **This shipped inside the fix for #617.**

**How to apply:**

1. **Never assume the CI environment is production's.** Before trusting a green suite about a
   host-only path, ask: *does the runner even have the constraint that production has?* If not,
   the suite is not testing what you think.
2. **Derive the sandbox from the unit file; never restate it.** A hand-written "read-only /tmp"
   job is a third place to encode the same assumption and goes stale the moment someone edits
   `ReadWritePaths=`. Parse `ProtectSystem=`/`ReadWritePaths=` out of the unit and run the real
   entry point under exactly those constraints (`unshare -rm` + a ro bind-mount, or
   `systemd-run --property=`). Then the test **cannot disagree with production about what
   production permits** — deploy#626/#627.
3. **A source-text scan is a proxy, not the property.** The property is *"writes nothing outside
   `ReadWritePaths=`"* — a runtime property. `mktemp` is only where it bit us. `> /tmp/foo`, a
   `cd /tmp`, a config read under `ProtectHome=yes`, a `python -c` calling `tempfile.mkstemp()`
   all fail identically and none is a `mktemp` line. (`rclone` escapes `ProtectHome=yes` today
   only because `backup.sh` configures it via `RCLONE_CONFIG_ISNAD_*` env vars instead of a
   config file. Nothing tests that.) Say *"no `mktemp` matched my regex"*, never *"the backup
   path is safe under hardening"* — the gap between those sentences is where all three defects
   lived.
4. **A check that cannot run must say so, and must not testify.** Give the instrument its own
   return code (rc=2 = *could not find out*, distinct from rc=1 = *not running*) and make the
   caller say **nothing** about the subsystem on rc=2. Collapsing them is how "Neo4j is NOT
   running — start it and re-run" gets printed at a healthy graph on a full disk, where
   re-running is the one action that makes it worse.

See [[feedback_measurement_is_the_thing_that_breaks]], [[feedback_calibrate_the_mutation_before_counting_it]],
[[reference_b2_preflight_discriminator]]. Org corpus: `feedback_silent_zero_is_not_a_measurement`.
