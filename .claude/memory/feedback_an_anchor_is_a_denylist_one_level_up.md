---
name: feedback_an_anchor_is_a_denylist_one_level_up
description: You can invert a guard from denylist to allowlist on the axis you're thinking about and rebuild the denylist one level up — in the CONTEXTS you agree to look at. The allowlist lesson generalises past syntactic forms to the POSITIONS the scanner will even parse.
metadata:
  type: feedback
---

deploy#637 asked for the prefix guard's polarity to be **inverted**: stop hunting for the bad
spelling (`${B2_BUCKET}/` not followed by a prefix), and instead **enumerate what is ALLOWED** —
every `rclone` call's remote argument must name a sanctioned prefixed root — so the guard **fails
closed** on anything it does not recognise.

Nino Kavtaradze did exactly that, and argued the point himself, correctly:

> *Verbs are deliberately NOT enumerated — **a verb list is a denylist wearing a different hat.***

He was right. And then he wrote `_RCLONE_CALL`, which only recognised `rclone` when preceded by
`^`, `$(`, `||`, `&&`, `;`, `if`, `then`, `elif`, or `!`. **Anything else was not flagged — it was
SKIPPED.**

Lucas Ferreira put the #632 unscoped purge back into the **real** `backup.sh` with nothing in front
of it but `timeout 300 `, and the suite reported **23 passed**. Also invisible: `sudo rclone`,
`eval rclone`, `for … do rclone`, `printf x | rclone rcat`, `xargs -I{} rclone`,
`RC=rclone; $RC purge`.

> **He was right about verbs and then did the same thing one level up.**
>
> The polarity was inverted for *roots*. The **context list** — the set of shell positions the
> scanner agrees to parse at all — was still a **denylist of the constructs he happened to think
> of.** `timeout` in front of a purge that can hang is not an exotic evasion; **it is the next SRE
> reflex.**

## The rule

**Enumerate what is allowed — on EVERY axis, including the axis of "where will I even look?"**

A guard has more surfaces than the one you are consciously inverting:

| axis | denylist form (wrong) | allowlist form (right) |
|---|---|---|
| the **value** | hunt for the bad spelling | require a sanctioned root |
| the **verb** | enumerate `purge\|copy\|sync\|…` | scan any subcommand in command position |
| the **context** | enumerate `^`, `$(`, `\|\|`, `;`, `if` … | **any bare token outside a quoted string** |
| the **file set** | a hardcoded three-element list | derive from the source / the unit |
| the **argument** | check the *first* sanctioned arg | check **every** remote-bearing arg |

Inverting one and leaving the others is not "mostly fail-closed". A guard is only as closed as its
**most permissive** axis, and the attacker (or the well-meaning SRE) walks in through that one.

The fixed form: **anything the scanner cannot parse into a checkable shape is an OFFENDER
("UNRECOGNISED CALL SHAPE"), never a skip.** On its first contact with the real files that rewrite
immediately refused a shape nobody had noticed (`restore.sh`'s `REQUIRED_CMDS+=(rclone)`) — which is
the guard working, not the guard misfiring. Enumerate the genuine non-call contexts explicitly, each
**with a written reason**.

## Two traps in the fix itself

1. **Command substitution must RE-ENTER unquoted context.** Every real call in `restore.sh` is
   `out="$(rclone lsf …)"`. A naive quote-tracker treats that as inside-a-string, **skips all four**,
   and the file goes green **by saying nothing at all** — a fresh instance of
   `feedback_a_scan_cannot_see_an_emptied_string`. A must-NOT-flag fixture **cannot** prove the
   tracker sees those tokens (if it skipped them, the snippet has no calls and passes anyway). The
   calibration must be an **unscoped call nested inside `"$( )"`**, which can only go red if the
   tracker genuinely looks.
2. **Keep "at least one sanctioned arg" ALONGSIDE "every remote-bearing arg is sanctioned."** They
   catch different things. An **emptied root variable** (#633) makes a call remote-bearing *nowhere*,
   so the "every" clause passes it **vacuously**. Dropping either clause reopens a hole.

## The coda, and the reason this file exists

Nino's **first run of the proof was itself broken**. He hand-built a minimal env for the mutation
harness, which killed the interpreter's module path, so **pytest never ran** — and every mutation
dutifully reported **"RED"**. It printed `14/16 PASS` from an instrument that was measuring nothing.

**The BASELINE control is the only thing that caught it:** a baseline that goes RED indicts the
*instrument*, not the code. In his words:

> *Had I omitted the baseline — or read only the reds I was hoping for — I would have sent a
> fabricated proof table with a straight face, one round after being told my guard was inert.*

**A mutation table where everything is red is not a strong result. It is a broken harness.** Always
run the baseline and the negative control, and make the harness **raise** if the test runner did not
actually execute.

See [[feedback_your_own_verification_script_must_not_testify]],
[[feedback_calibrate_the_mutation_before_counting_it]],
[[feedback_a_scan_cannot_see_an_emptied_string]],
[[feedback_measurement_is_the_thing_that_breaks]].
Org corpus: `feedback_lint_gate_cover_all_syntactic_forms` (this is its sibling — that one covers
syntactic *forms*, this one covers the *positions* you agree to look at). deploy#637/#643.
