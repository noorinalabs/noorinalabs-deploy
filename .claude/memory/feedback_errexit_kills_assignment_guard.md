---
name: feedback_errexit_kills_assignment_guard
description: Under `set -e`, `OUT="$(fn)"` followed by `RC=$?` is DEAD CODE on every failing path — errexit fires at the assignment. Guards written this way never print their diagnosis. Use `RC=0; OUT="$(set +e; fn)" || RC=$?`. And a static grep for the line proves only that it was typed.
metadata:
  type: feedback
---

Found in review of deploy#563 (`backup.sh` → `b2_preflight.sh`), 2026-07-09.

## The bug

```bash
set -euo pipefail
PREFLIGHT_OUT="$(preflight_b2 2>&1)"   # <-- errexit fires HERE on failure
PREFLIGHT_RC=$?                        # never executes
printf '%s\n' "$PREFLIGHT_OUT"         # never executes
if [[ "$PREFLIGHT_RC" -ne 0 ]]; then
    log "ERROR" "B2 preflight failed — refusing to dump…"   # never executes
fi
```

**An assignment whose command substitution exits non-zero is itself a failing simple
command.** `errexit` fires *at the assignment*. Everything below is unreachable on exactly
the paths the guard exists to serve. The operator saw one line, from the `EXIT` trap:
`Backup script exited with code 1`. The verdict and its remediation were computed and then
thrown away.

## The fix

```bash
PREFLIGHT_RC=0
PREFLIGHT_OUT="$(set +e; preflight_b2 2>&1)" || PREFLIGHT_RC=$?
```

`|| RC=$?` puts the assignment in a **condition context**, which suspends errexit for it.
The inner `set +e` additionally protects the callee's probes (see below).

## Two bash subtleties, measured on 5.2.21

1. **Command substitutions do NOT inherit `errexit` by default.** `$-` inside `$( )` shows
   no `e`, and `OUT="$(false; echo HI)"` yields `HI`. So the callee runs to completion and
   returns its real rc — the rc is just discarded when the assignment dies. Inheritance
   only happens under `shopt -s inherit_errexit`, and then the callee **dies at its first
   failing probe** before `rc=$?`, never computing a verdict at all.
   Defend inside the function with an explicit `set +e` rather than trusting the caller.
2. **`||`/`&&`/`if` suspend errexit for the *entire* command**, including inside the
   command substitution and the function it calls. This is why a repro written as
   `OUT="$(fn)" || true` passes while production, which has no `||`, dies.

## The meta-lesson: the guard's test was the same bug one level up

The test asserted the literal string `PREFLIGHT_RC=$?` appeared in the source. **It did
appear. It was also dead.** A static grep over a line that never executes proves only that
the line was typed.

Three harnesses all passed while production was broken, none of them production's:

| harness | why it passed |
|---|---|
| `./scripts/b2_preflight.sh` standalone | its own `set -uo pipefail` has **no `-e`** |
| `bash -c 'source …; classify_b2_state …'` | likewise no `-e` |
| `grep` over the source | executes nothing |

Production **sources** the script under `set -euo pipefail`. See
[[feedback_passing_repro_masks_bug]] — a green repro proves nothing if it used a different
invocation form than production.

**Test a guard by running the real entry point with the failure injected, and assert the
remediation text reaches stdout.** Prove it red against the unfixed tree first. Same family
as [[feedback_silent_zero_is_not_a_measurement]]: a check that cannot fail is a decoration.

## RECURRED, 2026-07-11 (deploy#584) — and this memory did not stop it

Same bug, same repo, two days later, in `restore.sh`:

```bash
set -euo pipefail
RESTORE_RUN_TS="$(… | grep '^BACKUP_MANIFEST ' | tr … | sed …)"   # <-- dies HERE
```

On an artifact with **no manifest**, `grep` matches nothing → under `pipefail` the whole
pipeline fails → the bare assignment is a failing simple command → **errexit kills
`restore.sh`**. The recovery path died on exactly the artifacts its own fallback branch
existed to serve. Fix: end the pipeline in `|| true`.

**The author had read this file and quoted it in the sibling script's comments an hour
earlier.** Knowing the rule did not help, because the *shape* looked different: #563 was
`OUT="$(fn)"; RC=$?` (a guard reading an rc); #584 was a plain `VAR="$(pipeline)"` (just
parsing a string). **There is no rc in sight, so the rule doesn't pattern-match.** The
generalisation to hold instead:

> **Under `set -e` + `pipefail`, ANY `VAR="$(…)"` whose command substitution can legitimately
> produce NO OUTPUT is a crash, not an empty string.** `grep`, `sed -n`, `find … | head`,
> `jq -e` — a no-match is a *normal, expected* outcome and a *non-zero* exit. Every such
> assignment needs `|| true` (or a condition context). Ask of each one: **"what is the exit
> status when this legitimately finds nothing?"**

### The harness table, again — and it is the same three rows

Every unit test passed. `_select()` ran the shipped block under `set -uo pipefail` —
**errexit omitted**, exactly the row already in the table above. The harness ran production
code under a *weaker shell mode than production*, so the one option that breaks the code was
the one option missing.

> **A harness that runs production code under a weaker shell mode is not running production
> code.** Copy the script's own `set -…` line into the harness verbatim; never retype a
> subset.

It was caught by the **restore rehearsal** — the end-to-end job that runs the real script
against a real stack, which went red on all four cases *including the intact-artifact
positive control*. That is the argument for keeping an expensive end-to-end rehearsal even
when the unit suite is green: it is the only harness that is not a paraphrase.

Related: [[reference_b2_preflight_discriminator]], [[feedback_passing_repro_masks_bug]],
[[feedback_calibrate_the_mutation_before_counting_it]].
