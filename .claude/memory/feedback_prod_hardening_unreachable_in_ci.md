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

**Why:** this is why the same bug **shipped twice past green CI and four reviewers**, and why it
would still have blocked backups *after* the credentials and the timer were fixed. It is the
cause that **survived every check**.

**It is NOT "why backups never once succeeded" — do not let this file tell you that.** The
outage had **three independent causes**, and this class is only the third (deploy#612, measured
on both hosts 2026-07-13):

1. **prod: `isnad-backup.timer` was never installed.** Backups there did not fail — they never
   *ran*. No `/tmp`, no preflight, no `mktemp`; the code never executed.
2. **stg: every firing died in ~75 ms** at `backup.sh: line 62: B2_KEY_ID: B2_KEY_ID must be set`
   → `status=1/FAILURE` (journal: 07-10, 07-11, 07-12 — the `:?` guard on a credential that had
   genuinely never been provisioned to the host env). That is **upstream of `b2_preflight.sh`
   entirely**; the read-only `/tmp` was never reached.
3. **this class** — which is what you hit *once a working key exists*, and the only one that no
   test, no reviewer, and no green CI run could catch.

The monocausal version is seductive because this cause is the interesting one. It is also the
error this file exists to name: **a check that could not run, testifying about a system it never
touched.** The `/tmp` class could not run on prod at all — the timer wasn't installed — so it
cannot be the explanation for prod's history. Correcting this cost nothing and the lesson is
*stronger* without the overclaim: three independent causes, and only one of them survived every
green check. (deploy#613's own body makes the same leap — "this, **not missing credentials**, is
why zero backups have ever succeeded" — so this file inherited it honestly. The memory is the
artifact that gets auto-loaded into every future session, so the memory is the one that has to be
right. Caught by Nino Kavtaradze reviewing deploy#631.)

Each instance below was **a check that could not run, reporting its own breakage as a fact about
the system it was checking** — pointing the operator at the wrong subsystem every time:

* **deploy#613** — `b2_preflight.sh` couldn't allocate scratch → `verdict=KEY_INVALID`. The B2
  key was perfectly good. Every backup attempted **once a key had been provisioned** died here,
  blaming that freshly-provisioned credential. (The distinction is the whole point of this file:
  before 2026-07-13 there was no key to blame, and the run died at line 62 long before reaching
  this code.)
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
