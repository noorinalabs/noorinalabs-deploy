---
name: feedback_shared_store_scope_the_destroyer_and_the_monitor
description: When two environments share one store, the DESTRUCTIVE job and the MONITOR must both be scoped — or one silently deletes the other's data and the other reports the other's health as yours.
metadata:
  type: feedback
---

stg and prod share **one B2 bucket** (`BACKUP_B2_BUCKET` is a single *repo-level* secret, read by
both `deploy-stg.yml` and `deploy-prod.yml`). Every remote path was built as
`${B2_BUCKET}/${category}/${date}` — **no environment anywhere in it** (deploy#632).

Nothing had collided only because prod had never taken a backup. The moment prod's timer was
enabled, two things would have happened, and the quiet one is worse than the loud one:

**1. The destructive job deletes the other environment's data.** `prune_old_backups` listed
`${B2_BUCKET}/${category}/` and `rclone purge`d every date dir past the cutoff — *not scoped to
the environment that ran it*. A backup system whose retention job destroys the other
environment's backups is worse than no backup system, because **it reports success while doing
it.**

Be precise about the mechanism, because the obvious guess is wrong. Retention is **age**-based,
not count-based: `prune_old_backups` purges any date dir older than `now − DAILY_RETAIN` days. It
does **not** keep "the newest N dirs". Both environments write into the same UTC `daily/<date>/`
and both inherit `DAILY_RETAIN=7`, so with identical settings they compute the **identical cutoff**
and make **identical purge decisions** — *nothing is halved, and no window is shortened*. (An
earlier draft of this file said each "silently halved the other's retention window." That presumes
count-based retention, and a future reader would have reasoned wrongly about every retention
question thereafter. Caught by Lucas Ferreira and Nino Kavtaradze on deploy#641.)

The loss is **conditional — and both conditions were live**:

* **(a) A failing environment stops pruning, and then loses everything.** `backup.sh` exits
  non-zero on a failed run *before* it reaches `prune_old_backups`. So an environment whose
  backups are failing never prunes, and its date dirs age past the cutoff. **That is exactly where
  stg was.** The moment prod's timer was enabled, prod's **first** prune would have purged every
  stg dir older than 7 days — **all of stg's remaining backups, leaving it with none** — while
  stg's own prune, which would have been the only thing to notice, had not run and would not run.
* **(b) Any divergence in retention config becomes silent cross-environment deletion.** The instant
  prod and stg hold different `DAILY_RETAIN`/`WEEKLY_RETAIN` — and prod wanting a *longer* window
  than stg is the obvious, likely change — the environment with the **shorter** window deletes the
  other's older backups on a schedule, with no bug and no attacker.
* **(c) And the shared date dir can take usable restores to zero anyway.** Two environments writing
  into one `daily/<date>/` means two run-ids in it, so `count_runs` > 1 and `restore.sh` trips the
  torn-restore refusal (deploy#589). The shared prefix does not shorten your retention window —
  **it can make the window unusable**, at the one moment you are looking: a DR restore.

**2. The monitor reports the other environment's health as yours.** `verify-backup-artifact.yml`
runs a `[stg, prod]` matrix — the *right* shape — but scanned the bucket **ROOT**, so both legs
read the **same objects**. With stg's first-ever backup in the bucket, **the prod leg would have
reported `fresh` on the strength of STAGING's artifact.** A false green on the one check that
exists precisely because every other backup signal reads a host-local gauge and can lie. It was
invisible only while the bucket was empty; the first successful backup is what *armed* it.

**Why:** a shared store makes the environment a property of the *path*, not of the *credential* or
the *host*. Nothing else enforces the boundary. And the two failure modes point in opposite
directions — one destroys and stays silent, one stays silent and reassures — so neither can catch
the other.

**How to apply:**

1. **Find every consumer of the shared path, and rule on each one individually.** Not just the
   writer. Census the *destructive* calls first (`purge`, `delete`, `rm`, `DROP`, `TRUNCATE`) —
   those need no attacker and no bug to fire; they are **scheduled**. Then the *monitors*, which
   are the ones that will tell you everything is fine.
2. **Fail closed on the namespace. No default.** `BACKUP_PREFIX` is REQUIRED in `backup.sh` and
   `restore.sh` and has *no* default: `""` silently restores the shared prefix and `prod` would be
   catastrophic on stg. An un-deployed host must fail **loudly** rather than collide quietly.
   Refuse a prefix containing `/` or `..` outright rather than sanitizing it — on the RESTORE side
   an escaping prefix is how you load the stg graph into prod.
3. **Scope the monitor to the same key the writer uses.** Here `matrix.env` was *already* exactly
   the prefix name; the fix was one line. If the monitor's scope and the writer's scope are not
   literally the same string, they will drift.
4. **A guard built for a different bug may be the only thing saving you — do not count on it.**
   The torn-restore guard (deploy#589) means two run-ids in one date dir makes `count_runs`
   **refuse** rather than silently restore the wrong environment's data. That saved us from the
   worst outcome by accident. It also means the failure surfaces *during a DR incident*, which is
   the one moment it must not.
5. **Say what you actually have.** Post-fix, the backups are **namespaced, not isolated** — one
   bucket, one read-write key, so a compromised stg box can still delete prod's backups. Name that
   in the issue and the runbook rather than letting "namespaced" be read as containment. Per-env
   `namePrefix`-scoped keys close it (deploy#634, no second bucket needed) — but the root-level
   preflight canary must move first (deploy#638), or a scoped key 401s it and takes **both** hosts
   into a total backup outage reported as a credential fault.

6. **Verify the DESTRUCTIVE leg with an experiment that can fail — and beware the one that
   cannot.** The first thing I claimed as proof was: *"after prod ran retention twice, stg's 9
   objects were still there."* **That was inert.** Today was `2026-07-14` and `DAILY_RETAIN=7` put
   the cutoff at `2026-07-07`, so the bucket's only date dir (`2026-07-14`) was **never
   purge-eligible**. Prod's retention runs were no-ops. **An unfixed `backup.sh` would have
   produced the identical observation.** A mutation that cannot change the outcome, recorded as a
   kill — `feedback_calibrate_the_mutation_before_counting_it`, sitting in this same corpus, names
   exactly that. (Caught by Nino Kavtaradze and Lucas Ferreira on deploy#641.)

   The experiment with discriminating power — **plant a BACK-DATED canary under BOTH prefixes** (a
   date older than the cutoff), then run the real prune with `BACKUP_PREFIX=prod`:

   ```
   POSITIVE CONTROL  prod/daily/2026-07-01/  -> GONE      the purge FIRED and was eligible
   THE PROPERTY      stg/daily/2026-07-01/   -> PRESENT   it CANNOT cross the prefix
   ```

   **Both halves, or you have measured nothing.** "stg survived" alone is satisfied by a purge that
   never ran. This one passed — and it could have failed.

**Verified in production by the canary experiment above** (deploy#641), not by the inert
observation that preceded it.

See [[feedback_prod_hardening_unreachable_in_ci]], [[reference_b2_preflight_discriminator]],
[[feedback_calibrate_the_mutation_before_counting_it]],
[[feedback_your_own_verification_script_must_not_testify]]. deploy#632/#633/#641.
