---
name: feedback_rc_is_not_an_authorization_signal
description: rclone returns rc=0 for a listing it is not permitted to see AND for a purge it could not enumerate — so an authorization boundary cannot be tested with an exit code in either direction. Verify with content + survival under a SECOND, differently-privileged identity.
metadata:
  type: feedback
---

Before migrating the B2 backup credentials to `namePrefix`-scoped keys (deploy#634), I minted a
throwaway scoped key and probed it against every access pattern the backup path uses. The harness
read **`rclone`'s exit code**. It reported:

```
[BAD]  purge ANOTHER prefix  keytest2/   -> SUCCEED, but MUST_FAIL
[BAD]  cannot even LIST      stg/        -> SUCCEED, but MUST_FAIL
```

…and, three lines later, in the same output:

```
decoy still present after the refused purge? -> 1  (must be 1)
```

**The purge "succeeded" and deleted nothing.** The isolation was working perfectly. The instrument
was lying.

## The mechanism — both directions are unsafe

* **A listing you are not permitted to see returns `rc=0` with EMPTY output.** Not `403`. Not a
  non-zero rc. `rclone lsf` on a prefix outside the key's `namePrefix` is indistinguishable, by exit
  code, from a prefix that is simply empty.
* **A `purge` that could not ENUMERATE anything returns `rc=0`, "success".** From rclone's point of
  view it purged an empty path and there was nothing to do. The permission denial happens at the
  *list* step, which it has already swallowed.

So the rc fails in **both** directions, and the second is the dangerous one:

| reality | rclone rc | naive verdict |
|---|---|---|
| boundary HOLDS, purge refused | `0` | *"it crossed the prefix — BROKEN"* (false alarm; what happened to me) |
| boundary BROKEN, but target happens to be empty | `0` | **"purge blocked — SAFE"** ← **ships the hole** |

An exit-code test of an authorization boundary is not a weak test. **It is not a test.** It returns
`0` whether the boundary holds or not.

## What actually measures it

Two things, and neither is an rc:

1. **CONTENT, against a target PROVEN populated first.** `0 objects` from a prefix you never
   confirmed had objects is vacuous. Confirm under a privileged identity first — `stg/` = 9 real
   objects, `prod/` = 18 — *then* an empty read by the scoped key means something.
2. **SURVIVAL, verified under a SECOND, differently-privileged identity.** The scoped key cannot
   tell you whether its own purge deleted anything; ask the admin key. And keep both halves:

   ```
   POSITIVE CONTROL  purge OWN   keytest/   -> admin re-count 0   the purge FIRES and is eligible
   THE PROPERTY      purge OTHER keytest2/  -> admin re-count 1   it CANNOT cross the prefix
   ```

   **Both, or you have measured nothing.** "The other prefix survived" alone is satisfied by a purge
   that never ran — the identical inert-experiment trap as deploy#641.

## The PRODUCTION consequence — not just a verification-harness lesson

Aisha Idrissi caught the sharper edge of this while fixing `prune_old_backups` (deploy#635). The
retention listing was `dirs=$(rclone lsf … 2>/dev/null || true)`, and the obvious fix is "capture
the rc and the stderr". **An rc check cannot close this class even in principle**, because:

> a key that is scoped *away* from a prefix returns **byte-for-byte what "no old backups" returns** —
> `rc=0`, empty output, no stderr.

So an rc-only fix closes the *loud* failures (bad credential, network fault, wrong bucket) and
leaves the **silent** one — which is the one that actually ships. And it stops being hypothetical
the moment the keys are `namePrefix`-scoped (deploy#634): a misconfigured prefix then produces
exactly a *permitted-but-empty* listing, and retention silently no-ops forever while every gauge
reads green.

Her answer is the right shape and worth stealing generally: **carry a positive control IN
PRODUCTION.** The run has *just uploaded* into its own prefix, so today's directory **must** appear
in the listing. If the listing cannot show the object the run just wrote, **the listing is lying**,
and the code refuses to prune from it. The control is not a test fixture — it is a fact the
production run itself manufactured one step earlier, which is what makes it impossible to fake.

Generalise: when an instrument can return a legitimate-looking empty, **give the production code
something it KNOWS must be there, and make it refuse when it cannot see it.**

## How to run it safely

* Aim the destructive probe at an **admin-planted decoy prefix**, never at real data. If the
  boundary turns out to be broken, you destroy a decoy and learn it — you do not destroy the
  backups you are trying to protect.
* Mint a **throwaway** key (short `validDurationInSeconds`), probe, delete it. Do not test a
  boundary by rotating the real secret and seeing what breaks.
* Prove the probe is not blind first: the scoped key **must** see its own prefix. If it reads `0`
  from its own namespace, every `0` below it is meaningless and the run must abort (rc=2, *could
  not find out* — never `0`).

**Why this keeps recurring:** the whole corpus already says *a silent zero is not a measurement*
and *a full-object-path rclone probe is VACUOUS* — and I wrote the harness that walked into it
anyway, minutes after re-reading both. Knowing the lesson does not confer immunity. **Running the
control does.** The generalisation worth carrying: **an exit code reports whether a tool completed
its work, never whether it was ALLOWED to.** Any time the question is *"is this credential
permitted to X?"*, the answer lives in the state of the object store, not in `$?`.

See [[feedback_your_own_verification_script_must_not_testify]],
[[feedback_shared_store_scope_the_destroyer_and_the_monitor]],
[[feedback_calibrate_the_mutation_before_counting_it]], [[reference_b2_preflight_discriminator]].
Org corpus: `feedback_silent_zero_is_not_a_measurement`. deploy#634/#638.
