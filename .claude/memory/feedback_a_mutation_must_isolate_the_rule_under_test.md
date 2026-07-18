---
name: feedback_a_mutation_must_isolate_the_rule_under_test
description: A mutation that TWO rules can both catch tells you nothing about EITHER. Lucas's calibration "proved" his unit-graph rule worked — but the mutation also tripped his adopter rule, which is what actually went red. The unit rule was blind, and the green table said otherwise.
metadata:
  type: feedback
---

The whole corpus already says *calibrate the mutation before counting it* — inject the defect, watch
it go RED, and only then believe the guard. This is the failure mode **one level past** that, and it
produces a mutation table that is **entirely correct and entirely uninformative.**

## What happened

Lucas Ferreira's structural pin (deploy#628, PR #661) enforced two independent rules:

1. **the unit-graph rule** — a script reachable from `systemd/*.service` must not allocate scratch
   outside the writable set;
2. **the adopter rule** — any script that *sources* `scratch.sh` must go through the allocator.

To prove rule 1, he injected a mutant script wired into a unit, and watched the suite go **RED**.
Table green. Rule proven. Ship it.

**Except the mutant script also sourced `scratch.sh`.** So **rule 2** flagged it. Rule 1 never fired.
And rule 1 was, in fact, **blind**: `unit_reachable_scripts()` matched only
`line.startswith("ExecStart=")` — which **excludes `ExecStartPre=`, `ExecStartPost=`, `ExecStop=`,
`ExecStopPost=`, `ExecCondition=`, `ExecReload=`**, every one of which **systemd runs in the same
sandbox**. A bare `mktemp -d` in an `ExecStartPre=` helper is deploy#613 verbatim under
`ProtectSystem=strict`, and the pin was **silent** on it.

In his own words:

> **Two rules, one mutation, wrong one credited. A calibration that doesn't *isolate the rule under
> test* cannot tell the difference.**

He had a **green mutation table, a real RED, and a broken rule**, simultaneously. Nothing in the
output was false. It just was not evidence for the thing he thought it was evidence for.

## The rule

**A mutation must be able to be caught by EXACTLY ONE rule — the one you are testing.** If any other
guard in the system can also catch it, the RED is *unattributable*, and an unattributable RED is not
a proof, it is a coincidence you have chosen to trust.

Concretely, when calibrating rule *R*:

* **Construct the fixture so every OTHER rule is structurally incapable of seeing it.** His fix: the
  new `test_the_pin_sees_every_exec_directive` wires a helper via `ExecStartPre`/`Post`/`Stop`/
  `Condition` using a fixture that **never mentions the library** — so the adopter rule *cannot*
  fire, and the RED can only have come from the unit graph.
* **Calibrate the other side too**, or your rule is just a glob: a script that **nothing references**
  must **NOT** appear in the closure. A resolver that returns everything is not a resolver.
* **Ask, of every RED: which rule fired?** Read it off the guard's own output (the offending-list, the
  specific failing assertion) — **not** off the FAILED test name, which is exactly what masked this.
  (The same trap bit the #637 review: Nino's calibration test fired on *any* clause deletion, so
  Lucas had to read `_offending_rclone_calls` directly to learn which clause caught what — and it
  turned out **each clause caught precisely what the other missed**, which the FAILED list could
  never have shown.)

## Why this is the hardest one to see

Every other instrument failure in this corpus announces itself: a silent zero, an all-red table, an
`rc=0` that should have been a refusal. **This one produces a perfectly healthy-looking result.** The
test ran. It went red. It went red for a real defect. The only thing wrong is the **inference** —
and there is nothing in the output to contradict it.

The defence is not vigilance. It is **construction**: build the fixture so that only one rule *can*
fire, and the inference becomes forced rather than assumed.

See [[feedback_calibrate_the_mutation_before_counting_it]],
[[feedback_measurement_is_the_thing_that_breaks]],
[[feedback_an_anchor_is_a_denylist_one_level_up]],
[[feedback_your_own_verification_script_must_not_testify]].
deploy#628/#661, and the sibling on deploy#637/#643.
