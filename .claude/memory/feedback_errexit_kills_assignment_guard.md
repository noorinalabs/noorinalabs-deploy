---
name: feedback_errexit_kills_assignment_guard
description: Under `set -e`, `OUT="$(fn)"` followed by `RC=$?` is DEAD CODE on every failing path — errexit fires at the assignment. Guards written this way never print their diagnosis. Use `RC=0; OUT="$(set +e; fn)" || RC=$?`. And a static grep for the line proves only that it was typed. CORRECTED 2026-07-11: the no-match rule holds for `grep` ONLY — `sed`/`find` exit 0 on no-match; look up the command's actual contract. Third mechanism: `pipefail` promotes an early-exit consumer's SIGPIPE (141) over a SUCCEEDING final stage.
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

> **Under `set -e` + `pipefail`, ANY `VAR="$(grep …)"` whose command substitution can
> legitimately produce NO OUTPUT is a crash, not an empty string.** A no-match is a *normal,
> expected* outcome and a *non-zero* exit. Such an assignment needs `|| true` (or a condition
> context). Ask of each one: **"what is the exit status when this legitimately finds nothing?"**

### ⛔ CORRECTION 2026-07-11 (Nino Kavtaradze, deploy#591) — the restatement above was ITSELF over-generalised

It originally read `$(grep/sed/find …)`. **That lumps three commands with three different
no-match contracts, and it is wrong for two of them.** Measured, not reasoned:

| command | exit on **no match** | under `set -e` |
|---|---|---|
| `grep` | **1** | **CRASHES** — the rule holds |
| `sed -n '/x/p'` | **0** | survives — **the rule is FALSE** |
| `find` | **0** | survives — **the rule is FALSE** |
| `find` over an **unreadable** dir | **1** | **CRASHES — but on an ERROR, never a no-match** |

**The over-generalised rule fails in BOTH directions.** It produces guards against crashes that
cannot happen (`sed`, `find`), *and* it points at the wrong trigger for `find` — so anyone who
removes a `find` guard on learning "find doesn't crash on empty" is then bitten by the
permission case. A rule wrong in both directions is worse than no rule.

> **The correct instruction is not a list of commands. It is: LOOK UP THE COMMAND'S ACTUAL
> NO-MATCH EXIT CODE.** Do not generalise across commands that merely *feel* similar. `grep`'s
> "no match is exit 1" is a deliberate and unusual contract, not a Unix convention — most
> filters exit 0 on empty output.

**And errexit does not reach into `$( )` at all by default.** `shopt inherit_errexit` is unset,
so a failing command *inside* a command substitution does not kill the subshell — regardless of
whether the call site is a condition context. It is a **global shell option**: setting it arms
every `$( )` in the file at once. What *does* fire is the **assignment's own rc** — a command
substitution's exit status is the exit status of the assignment reading it, so a *top-level*
`VAR="$(fn_returning_1)"` crashes under `set -e` no matter what is inside the function.

## PIPEFAIL PROMOTES SIGPIPE — an early-exit consumer poisons a SUCCEEDING pipeline (deploy#591)

Same family, third mechanism, found by Nurul Hakim in the deploy#591 review:

```bash
set -euo pipefail
grep '^BACKUP_MANIFEST ' | head -n1 | tr ' ' '\n' | grep -qx 'complete=true'
```

When a **consumer exits early** (`head -n1`, `grep -q`), its producer takes **SIGPIPE and dies
141** — and **`pipefail` promotes that 141 to the rc of the whole pipeline even though the final
stage SUCCEEDED.** A complete, attesting backup manifest read as *"does not attest"*.

There were **two** early-exit consumers, so the obvious one-token fix does **not** close it:

```
first line attests complete=true:
  grep | head -n1 | tr | grep -qx     100k lines -> 141    huge first line -> 141
  grep -m1 | tr | grep -qx            100k lines -> 141    huge first line -> 141
                                      ^ `grep -qx` exits on the match and SIGPIPEs `tr`.
```

> **Do not swap the early-exit consumer — REMOVE THE PIPELINE.** A herestring is a file
> descriptor, not a pipe: `grep -m1 pattern <<< "$text"` may stop reading early and nothing
> dies. Then do the token test in plain bash. The shape is deleted rather than guarded.

Latent (needs a manifest ~700x what the producer writes) and it fails closed — but **a guard
with a known path that silently disarms it is not a correct guard, it is an incomplete one.**

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
