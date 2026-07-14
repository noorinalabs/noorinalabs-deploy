---
name: feedback_measurement_is_the_thing_that_breaks
description: Across 3 review rounds on deploy#624 the broken thing was ALWAYS the measurement, never the code — and an author cannot calibrate their own guard, because the fixtures inherit the guard's blind spot.
metadata:
  type: feedback
---

Three rounds of review on deploy PR#624 (the guard against deploy#613/#623). **Every single
round, the product code was correct and the *measurement* was broken.** Each defect was found by
a reviewer, never by the author, and each one had passed a green suite:

| Round | The guard | How it was blind |
|-------|-----------|------------------|
| 1 | structural pin over `compose_project.sh` | Read **one file**. The unit `ExecStart`s `backup.sh`, which *sources* the other two. Two of three files in the closure were unguarded. |
| 2 | `"-p " not in line` | Anchored on the **LINE, not the invocation**. Any unrelated `-p` elsewhere on the line vouched for a bare `mktemp` beside it. |
| 3 | `re.compile(r'(-p\s*["$\w/]\|--tmpdir=)')` | Matched `-p` as a **SUBSTRING, not an option** — including inside a *template name*. The library's own template is `compose-project-XXXXXX`. It contains `-p`. So `mktemp -t compose-project-XXXXXX` — which allocates in the read-only `/tmp`, i.e. **deploy#613 verbatim** — passed GREEN. |

**Why:** the deepest one is round 3, and it is the reusable lesson.

**An author cannot calibrate their own guard.** The fixtures written *in the same sitting* to
prove the round-3 filter worked used template names `probe.XXXXXX` and `${PREFIX}.XXXXXX` —
names that happen to contain **no `-p`**. The guard and the test that calibrated the guard
**shared the same blind spot**, because they were written by the same person under the same
assumption. The test could not see the hole *by construction*. A green calibration proved
nothing.

Note also that **the codebase supplies the evasion for free**: the convention this very PR
introduced was to write `${BACKUP_DIR}`, and the round-2 filter exempted any line containing
`$`. The house style selected precisely for the line that disarmed the guard.

**How to apply:**

1. **The guard's calibration must be written adversarially, ideally by someone else.** If you
   must self-calibrate, derive the fixtures from the **real artifacts** (the actual templates,
   the actual call sites in the repo), never from names you invent — invented names inherit your
   assumptions. Lucas found round 3 by mutating **the real library**; he'd have missed it
   reading the fixtures.
2. **Put the evasion on BOTH sides of the table.** The must-flag fixtures *and* the must-not-flag
   fixtures should each contain the tricky token (`-p` inside a template name, in both classes).
   Then anchoring can't silently cost you the legitimate call it resembles, and a future
   loosening has to go red on a real spelling rather than a straw one.
3. **Ask what the codebase's own conventions hand an attacker.** A guard is not evaluated in a
   vacuum: grep the repo for the idiom your exemption keys on. If house style produces it, the
   exemption is a hole, not a convenience.
4. **Verify the instrument before reading it — both directions.** A one-way oracle proves
   nothing. (Mid-review Lucas's own probe grepped for `"9 passed"` against a 7-test module and
   reported RED on *everything*, baseline included; he rebuilt it on the pytest exit code before
   reading a single result. He also *withdrew* one of his own findings — `mktemp -up X` is
   exempted **correctly**, since `-p` takes an argument, so `-up X` is `-u -p X`.) That is the
   standard.
5. **Remove an escape hatch; do not narrow it.** A guard against a bug that has already shipped
   twice does not get a convenience exemption. One rule a human can hold — *in a hardened script,
   every `mktemp` names its parent* — beats a clever pattern with a carve-out.

See [[feedback_prod_hardening_unreachable_in_ci]] (the environment that made all of this
unreachable in CI), [[feedback_calibrate_the_mutation_before_counting_it]]. Org corpus:
`feedback_fixture_makes_guard_assertion_inert`, `feedback_silent_zero_is_not_a_measurement`.
