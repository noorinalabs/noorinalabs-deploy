---
name: feedback_calibrate_the_mutation_before_counting_it
description: A mutation you have not shown to be EFFECTIVE is not evidence. Mutation testing has the same failure mode as the guards it audits — an oracle that can only say BLOCKED/PASS proves every guard holds. Calibrate the harness against the real bug before reading any result from it.
metadata:
  type: feedback
---

**Mutation testing is an instrument, and it obeys the same rule as every other instrument: verify it can separate the classes before you read it.** This is [[feedback_silent_zero_is_not_a_measurement]] applied to the thing you were using to check for silent zeros. It has now bitten three people on one PR (deploy#574, 2026-07-11) — including two who were *explicitly warning others about it at the time*.

## The two ways a mutation battery lies

### 1. The INERT mutation — reported as a kill

You mutate the code, the suite stays green, and you write "not caught — test gap". Or you mutate, it goes red, and you write "caught". **Both conclusions are void if the mutation did not actually change behaviour.**

Real cases, same PR:

* A "caller forges the provenance artifacts" mutation **passed green** — and it was not a test gap. `verify_present` refuses in B2 *before* the pull, so the forge line never executed. The mutation was a no-op. The effective version (drop the objects from `verify_present` **and** synthesize them locally) *was* caught.
* An `ALLOW_NO_MANIFEST` bypass mutation "passed" — because `A && B || die` with `A` false still reaches `die`. It never unlocked anything.
* A reviewer's own `SKIP_PROVENANCE` mutation declared the env var but never wired it to a bypass. Its "caught" status said something about the test and **nothing about the guard**.

**Four of one reviewer's seven mutations were inert.** He marked them inert rather than counting them as kills. That is the standard.

### 2. The oracle that can only return one answer

A reviewer built an unlock oracle to run attacks against the workflow. It reported **every attack BLOCKED** — including the **honest artifact**, which should have succeeded. The script was dying on line 1 (`sh` is dash; no `pipefail`), so nothing was ever exercised.

**An oracle that can only say BLOCKED proves every guard holds.** Uncalibrated, he would have written "all attacks held" and been completely wrong. The tell was the *positive* case failing — which is why the positive case must be in the battery.

## How to apply

1. **Baseline first.** Run the suite unmutated and require green. A battery on a red baseline is void.
2. **Include a positive control** — the honest input that must PASS. If your harness cannot produce a pass, it cannot produce information. This is the single highest-value line in any battery.
3. **Prove each mutation is effective** before recording its verdict. Ask: *did this actually change what the code does on the fixture?* If an earlier guard short-circuits it, the mutation is inert — rebuild it until it genuinely unlocks the dangerous path, then read the result.
4. **Calibrate the harness against the real bug.** Re-introduce the defect the harness exists to catch and require it to go RED. A harness that has never failed has never been tested.
5. **Report inert mutations as inert.** Never as kills, never as gaps.

## The sentence that goes with it

> **CI is 16/16, shellcheck clean, 21 behavioural tests green — and the workflow cannot prune. Green is not verified.**

The live bug it names: a `sed` extractor written single-quoted with doubled backslashes, so it matched **nothing, ever**. The gate emitted `canonical_ids=129234`; the caller extracted empty; the workflow would have aborted on the first honest artifact, and the completeness binding — the one gate standing between the graph and an 83.9% deletion — was **unreachable dead code**. It was authored by the same person who, one commit earlier, had fixed *the identical defect class* one layer down and added a positive control for it. **The control guarded the gate's parser. Nothing guarded the caller's.**

**Corollary — hardening a guard does not harden its caller.** Forging provenance *inside* the gate was caught. The identical three lines *in the caller*, one file over, were invisible and issued a real prune. Whenever you extract a guard into a testable unit, ask immediately: *what now tests the seam?* See [[feedback_security_guard_inline_not_followup]].
