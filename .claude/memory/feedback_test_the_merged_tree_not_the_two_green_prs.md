---
name: feedback_test_the_merged_tree_not_the_two_green_prs
description: Two PRs, each green alone, that merge textually clean — and break when combined. #661 made backup.sh source a new scripts/scratch.sh; #644's test fixture staged backup.sh but not scratch.sh, so on the merged tree backup.sh refused to run and 2 of #644's tests failed. GitHub said MERGEABLE/CLEAN. Only a merged-tree test run found it.
metadata:
  type: feedback
---

Two PRs in the same wave, both green, both approved, both touching `backup.sh`:

* **#661** introduced `scripts/scratch.sh` and made `backup.sh` **source it** at startup —
  `backup.sh` now hard-refuses to run if the allocator library is absent (correct: fail closed).
* **#644** added retention tests whose fixture **copies `backup.sh` + `compose_project.sh`** into a
  temp `scripts/` dir and executes it.

#644's fixture file-list was **complete when it was written**. After #661 merged, `backup.sh`
depends on a file the fixture does not stage, so the sourced script aborts —
`Missing scripts/scratch.sh — no scratch allocator, cannot run safely` — before it reaches the
code the test is about. **Two of #644's tests go red on the combined tree.**

GitHub reported **`MERGEABLE` / `CLEAN`**. There is no textual conflict — the two `backup.sh` hunks
are in different regions. Each PR's own CI was **green**. The break exists **only in the
combination**, and nothing either PR could show in isolation reveals it.

## The rule

**Before merging PR B while PR A (that shares a file, or a runtime dependency, with B) has already
landed, test the ACTUAL merged tree — not the two green PRs, not GitHub's `MERGEABLE`.**

`MERGEABLE` means *"git can produce a merge commit without a textual conflict."* It says **nothing**
about whether the merged code, tests, or runtime still work. A clean merge of two individually-green
branches can be red. Those are three different questions and only the third one matters:

1. Does it merge without conflict?  → `MERGEABLE` (necessary, not sufficient)
2. Was each side green alone?        → each PR's CI (necessary, not sufficient)
3. **Is the MERGED tree green?**      → the only one that gates a safe merge

The cheap, decisive check (what caught this):

```bash
git worktree add --detach /tmp/m <current-main>
cd /tmp/m && git merge --no-edit <pr-branch>       # clean? good — now the real question:
python3 -m pytest <the tests that touch the shared file>   # green on the COMBINED tree?
```

If red, that is a real finding for the second author — here, a one-line fixture fix (stage
`scratch.sh` too) — surfaced **before** it lands on `main`, not after.

**And READ the result correctly — do not pipe the run.** The check caught #642's break too (18
failed, on the same `scratch.sh` gap) — but I ran `pytest … 2>&1 | tail -8` in a background command,
so the harness reported the **exit code of `tail` (0)**, not pytest's (1). I nearly read "exit code
0" as a pass. The `N failed` **summary line** in tail's output is the only thing that stopped me. This
is `feedback_push_pipe_masks_rejection` — *ANY* `cmd | tail`/`| head` returns the pipe's status, not
`cmd`'s — and the orchestrator running the verification is not exempt from it. **Redirect pytest to a
file and read the summary line; never pipe the command whose exit code is the whole point.** Two green
PRs, a clean merge, two approvals, and a masked "exit 0" all lined up to wave a 18-failure tree onto
`main`'s restore path; only reading the actual `18 failed` line held.

**The same "verify the artifact, never the exit code" rule governs the PUSH, not just the test.** The
deploy pre-push suite (~6 min) outlives the default SSH idle timeout, so `git push` returns
**`rc=141` (SIGPIPE, "connection closed by remote host") on a push whose hooks all PASSED** — the ref
may or may not have advanced. Three agents hit it this wave and all did the right thing: **check
`git ls-remote` to see whether the ref actually moved**, trusting neither `rc=0` nor `rc=141`. Fix is
a transport keepalive (`GIT_SSH_COMMAND='ssh -o ServerAliveInterval=20 -o ServerAliveCountMax=40'`) or
a tiered pre-push — **never `--no-verify`** (blocked, full stop). deploy#682. The through-line: an exit
code — of a test, a pipe, or a push — is a proxy; the artifact (the summary line, the remote ref, the
object store) is the thing.

## Why it matters more than usual here

The shared file is `backup.sh`, which runs unattended at 03:00 against the only backups production
has. **Merging a break onto `main`'s backup path, even briefly, is not acceptable** — "merge then
revert" is a gamble the backup path does not get to take. The whole point of testing the merged tree
first is that the second PR's break is caught in a scratch worktree, costing one author a rebase,
instead of on `main`, costing everyone a red backup gate.

## The deeper cause, and the durable fix

#644's fixture **hand-listed** the files to stage. A hand-maintained file-list is correct exactly
until the shared script grows a new dependency — which is the same disease as every other "derive
the set, don't hand-type it" finding this wave
([[feedback_a_mutation_must_isolate_the_rule_under_test]], the `Exec*` closure, the scanned-file
set). **Stage whatever the script actually sources**, resolved from the script, not a list frozen at
authoring time. Then the next new dependency does not silently break the harness.

Org corpus: `feedback_consumer_wave_merge_ordering` (the CI-ordering sibling — a consumer wave
merged before its producer false-reds; this is its within-wave, shared-file twin),
`feedback_deployable_merge_verification` (verify post-merge-only workflows after landing).
deploy#644 × #661.
