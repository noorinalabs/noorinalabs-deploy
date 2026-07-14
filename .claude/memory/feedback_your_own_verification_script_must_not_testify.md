---
name: feedback_your_own_verification_script_must_not_testify
description: "A check that cannot run must not testify" applies to the throwaway shell you write to CHECK the fix, not just to production code — mine printed "*** DATA LOSS ***" about a backup that was perfectly intact.
metadata:
  type: feedback
---

Minutes after taking the first prod backup in the project's history, I ran an ad-hoc script to
prove that prod's retention purge had not eaten staging's backups (the deploy#632 thesis). It
printed:

```
=== STG BACKUP SURVIVED PROD RETENTION? (the whole point of #632) ===
  objects still under stg/daily/2026-07-14/: 0  (was 9)
  *** DATA LOSS ***
```

**The backup was completely intact.** All 9 objects were there, checksums unchanged.

What actually happened is in line 1 of the same output, which I had to force myself to read
before reacting to the verdict:

```
bash: line 1: /root/.b2-prod-backup.env: Permission denied
```

The script ran as `deploy`, not `root`. The credential file never sourced. `B2_KEY_ID` was
therefore the **empty string** from `.env`. Every `rclone` call failed to authenticate — and I had
written `2>/dev/null` on all of them, so the auth error vanished. `rclone lsf … | wc -l` then
turned *"I could not authenticate"* into the number **`0`**, and my own `[ "$n" -eq 9 ]` turned
`0` into **`*** DATA LOSS ***`**.

**Why:** this is exactly deploy#613 — `b2_preflight.sh` couldn't allocate scratch and reported
`verdict=KEY_INVALID`, blaming a credential that was fine — and exactly the rule I had spent the
whole night enforcing on production code and had *just* written into a memory file: **a check that
cannot run must say so, and must not testify.** I applied it to `backup.sh`, to `restore.sh`, to
`compose_project.sh`, to the CI guards — and then wrote a 20-line verification script with none of
it, and *believed the script over the system*. The throwaway shell you write to CHECK the fix is
an instrument too, and it is the one you trust most and calibrate least, precisely because it feels
too small to be wrong.

A silent zero is not a measurement. It is the single most dangerous thing an instrument can
return, because it is indistinguishable from good news being absent.

**How to apply:**

1. **Positive control FIRST, before any reading.** Prove the instrument can see the thing at all,
   and *exit* if it cannot:

   ```bash
   if ! rclone lsd "isnad:${B2_BUCKET}" >/dev/null 2>/tmp/e; then
       echo "*** INSTRUMENT FAILURE — nothing below is a measurement ***"; cat /tmp/e; exit 2
   fi
   ```
   Give it its own return code (`2` = *could not find out*), distinct from a real negative (`1`).
   The re-run with this control took thirty seconds and produced the true answer: **stg objects: 9.**
2. **Never `2>/dev/null` in a verification script.** The diagnostic *is* the finding on the failure
   path. `stderr` is COMMENTARY when a command succeeds and DATA when it does not — and a
   verification script exists precisely to handle the case where it does not.
3. **Distrust a count you did not prove could be non-zero.** `wc -l` on a failed listing returns
   `0` and cannot be told from an empty listing. Before believing a zero, prove the same pipeline
   returns non-zero for a case you *know* is populated.
4. **Read the whole output before reacting to the verdict line.** The permission error was line 1.
   I nearly reported a catastrophe on the strength of line 8 of the same block.
5. **Calibrate the PROBE, not just the pipeline.** The experiment I later wrote to *prove* the
   purge was scoped (deploy#641) failed for a **different instrument bug**, in the same script,
   the same night. Its probe was:

   ```bash
   present() { rclone lsf "$1" >/dev/null 2>&1 && echo PRESENT || echo GONE; }   # $1 = FULL OBJECT PATH
   ```

   **`rclone lsf` on a full object path returns rc=0 for a path that does not exist.** (This is
   already recorded in `reference_b2_preflight_discriminator` / the B2 notes as *"a full-object-path
   rclone probe is VACUOUS"* — I had written that down and still walked into it.) So `present()`
   answered `PRESENT` for **everything**, the verdict printed `*** INERT ***`, and the purge had in
   fact worked perfectly. I nearly reported a working fix as broken — the mirror image of nearly
   reporting an intact backup as destroyed, ninety minutes earlier.

   The fix is a **calibration step on the probe itself, before it is used to read anything**:

   ```
   probe on an object that EXISTS    -> PRESENT   (must be)
   probe on an object that DOES NOT  -> GONE      (must be)
   -> the probe separates. Readings below mean something.
   ```
   Probe the **directory** and look for the object in it; never `lsf` an object path. And take the
   **verdict before cleanup** — take 1 purged both canaries before I could tell which one the
   *prune* had deleted, destroying its own evidence.
6. **Assume this applies to whatever you are writing right now.** I wrote this class of bug into a
   verification script *while* fixing the same class of bug in production code, on the same night,
   having just documented it — **and then did it twice more.** Three instrument failures in one
   session, all mine, all in throwaway shell I was too confident to control. Knowing the lesson does
   not confer immunity; **running the control does.**

See [[feedback_prod_hardening_unreachable_in_ci]], [[feedback_calibrate_the_mutation_before_counting_it]],
[[feedback_stderr_is_commentary_not_data]], [[feedback_errexit_kills_assignment_guard]].
Org corpus: `feedback_silent_zero_is_not_a_measurement`. deploy#632/#612.
