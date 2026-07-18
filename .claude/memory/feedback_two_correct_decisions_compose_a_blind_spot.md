---
name: feedback_two_correct_decisions_compose_a_blind_spot
description: The hardening gate — built specifically to catch a scratch failure under ProtectSystem=strict — CANNOT SEE one on the retention path. Not because either component is wrong. Because two individually-correct safety decisions composed into a hole, and the gate's own success criteria still held.
metadata:
  type: feedback
---

Two decisions, each correct, each made by a different person for a good reason:

* **deploy#635** (retention): a `scratch_file()` / listing failure inside `prune_old_backups` sets
  `RETENTION_OK=false`, emits gauge `0`, logs `"no writable scratch file"` — **and by design does NOT
  fail the backup run.** Correct: failing the run would convert a storage-cost problem into a **total
  backup outage**, which is strictly worse.
* **deploy#654** (the hardening gate): the harness executes the real `backup.sh` under
  `ProtectSystem=strict` and asserts the run **`rc == 0`** and the **success gauge is present** —
  exactly to catch the deploy#613/#623 class where scratch lands in read-only `/tmp`.

Now compose them. A `scratch_file()` regression **on the retention path**:

```
scratch_file() fails under ProtectSystem=strict
  -> "no writable scratch file", RETENTION_OK=false, gauge 0
  -> run does NOT fail            (deploy#635, deliberate)

harness asserts:  rc == 0  AND  success gauge present
  -> BOTH STILL HOLD
  -> *** THE HARDENING GATE PASSES ***
```

And `FALSE_CLAIMS` — the harness's list of lying diagnostics — did not include
`"no writable scratch file"`. So **the one gate built specifically to catch a scratch failure under
hardening cannot see one on the retention path — precisely because retention is well-behaved enough
not to crash.** (deploy#675, found by Aisha Idrissi driving the merged gate against her own code.)

## Why this is a class above "the test was weak"

Every other instrument failure in this corpus is a *mistake*: a substring oracle, a silent zero, an
`rc=0` misread, a mutation that could not fire. **This one contains no mistake.** Both components are
correct in isolation and would pass any review of themselves. The defect lives **only in the seam**,
and it is invisible from inside either component:

* From #635's side: "I correctly decline to fail the run." True.
* From #654's side: "I correctly assert the run succeeded and emitted its gauge." True.
* The gap — *"a scratch failure that neither fails the run nor trips a FALSE_CLAIM is a defect the
  gate was built to catch and cannot see"* — is **nobody's line of code.**

**The safety decision (#635) created the blind spot.** The very property that makes retention safe —
it does not take the backup down — is what makes its failure invisible to a gate that watches the
backup's exit code. Robustness in one layer erased observability in another.

## How to apply

1. **Review the SEAMS, not just the components.** When two subsystems each make a local safety
   decision, ask explicitly: *does either decision hide a failure the other is responsible for
   catching?* A failure that is deliberately non-fatal in layer A is a failure layer B must detect
   some **other** way than "did the run fail?" — because A has guaranteed it will not.
2. **A gate's success criteria must cover every failure mode of every path it executes — including
   the paths that fail SILENTLY BY DESIGN.** `rc == 0` + a success gauge is not sufficient coverage
   for a script that contains a deliberately-non-fatal failure path. The gate must ALSO scan for the
   diagnostic string of that path. (The fix: add `"no writable scratch file"` to `FALSE_CLAIMS`, so
   the scratch failure goes RED **without** coupling retention's failure back to the run's exit code
   — you fix the observability gap, you do NOT undo the safety decision.)
3. **Do not "fix" a composition blind spot by removing one of the correct decisions.** Coupling
   retention's failure to the run's exit code would make the gate see it — and reintroduce the total-
   outage bug #635 closed. The fix is a THIRD observation (the string scan), not the deletion of the
   second.
4. **A gate whose green baseline is full of ERROR lines is already broken.** The same review found the
   harness stub's `rclone` claimed an upload succeeded and then returned an empty `lsf` — an internal
   contradiction that the real code's positive control correctly flagged, filling the gate's *passing*
   output with retention ERRORs. **Noise in the green state trains people to ignore exactly the lines
   that matter** — deploy#559/#565 (alert fatigue) reincarnated inside a test. A gate's happy path
   must be clean, or its reds are invisible.
5. **The only way to find this was to EXECUTE the gate against real code and read the output.** No
   static review of either #635 or #654 surfaces it, because each is correct on its own. Aisha found
   it by driving the merged harness and grepping the run — the same "execute it, don't reason about
   it" that has caught every real defect this wave.

See [[feedback_guard_the_actor_not_the_stage]], [[feedback_measurement_is_the_thing_that_breaks]],
[[feedback_prod_hardening_unreachable_in_ci]],
[[feedback_a_mutation_must_isolate_the_rule_under_test]].
deploy#635 + #654 → #675.
