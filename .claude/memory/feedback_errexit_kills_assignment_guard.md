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

Related: [[reference_b2_preflight_discriminator]].
