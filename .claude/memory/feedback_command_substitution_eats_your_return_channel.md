---
name: feedback_command_substitution_eats_your_return_channel
description: Callers invoke the allocator as err="$(scratch_file)" — a SUBSHELL. Anything the library writes to a variable (a registry array, an error global) dies at that boundary. It ate the cleanup registry (so the EXIT drain drained nothing) and the error reason (so the diagnostic printed blank). Also: the fix prescribed in the issue would have killed the backup.
metadata:
  type: feedback
---

Two lessons from deploy#625/#628/#629 (PR #661), both found by tests rather than by reading.

## 1. A library whose callers use `$( )` cannot return anything in a VARIABLE

Every call site allocates like this:

```bash
err="$(scratch_file)"
```

That is a **command substitution** — a **subshell**. The library's stdout crosses the boundary.
**Everything else does not.** Any state the library sets in a *variable* is written in a child
process and evaporates on return.

Lucas Ferreira fell into this **twice in one PR**, and the tests caught both:

* **It ate the cleanup registry.** The allocator appended each scratch path to a global array so the
  `EXIT` trap could drain it. The array was populated **in the subshell**. The parent's array stayed
  empty, **the EXIT drain drained nothing, and every allocation leaked** — the precise bug the
  registry was added to fix.
* **It ate the error reason.** A `SCRATCH_ERROR` global carried *why* allocation failed. Also set in
  the subshell. The diagnostic printed **a blank where the reason should be** — **inert**, and an
  earlier draft of the PR body claimed it worked.

**The fixes, and the general shapes:**

* **Registry → encode it in the artifact itself.** A PID tag in the *filename* survives, because the
  filename comes back on stdout. Note **`$$` is stable across command substitution; `$BASHPID` is
  not** — `$$` keeps the parent's PID inside a subshell, which is exactly what you want here and the
  opposite of what you usually want.
* **Error reason → stderr.** It is the one channel that crosses the boundary without colliding with
  the return value on stdout. (Consistent with `feedback_stderr_is_commentary_not_data`: stderr is
  DATA on the failure path.)

**Ask, of any shell library:** *how do my callers invoke this?* If the answer is `$( )`, then
**stdout is your only return channel, stderr is your only side channel, and variables are a lie.**

## 2. The fix the ISSUE prescribed would have KILLED THE BACKUP

deploy#629 said, in as many words: *"`trap 'rm -f -- "$err"' RETURN` in each caller."* It was
written by a careful reviewer and it was repeated in the implementation brief. **It is wrong twice
over**, and Lucas found out by implementing it faithfully and watching `test_restore_failure_modes.py`
go red:

* **`RETURN` does not fire on a signal.** Measured: SIGTERM mid-`dc ps` and the scratch **still
  leaks**. So it never closes the kill it was raised for — the entire reason #629 exists.
* **`RETURN` is GLOBAL and stays armed.** It re-fires on the *caller's* return, with `$err` now out
  of scope — which under `set -u` is `unbound variable`, and **kills `backup.sh` mid-run.** It
  detonates only when the allocating function is called **from another function**, which is what
  every real caller does (`list_backups → list_category`; `backup.sh → neo4j_start`). A naive unit
  test calling the function at top level would never see it.

**The correct shape:** an **`EXIT`-trap drain** (EXIT *does* run on SIGTERM), plus a **stale-reaper**
for SIGKILL, which is untrappable by construction and therefore cannot be handled — only cleaned up
afterwards.

**Why:** an issue's *diagnosis* and an issue's *prescription* are separately fallible. The diagnosis
here was excellent (the leak is real, `BACKUP_DIR`'s root has no reaper, and a full `BACKUP_DIR`
turns `scratch_file` failure into a **false claim that Neo4j is down**). The prescription was a
plausible one-liner that had never been executed. **Implement the brief, then test the brief.** When
a prescribed fix fails, that is a finding to report — not a spec to satisfy.

## 3. Coda — `pytest … | tail` returns TAIL's exit code

Lucas's first "green" full-suite run was piped to `tail`. **20 tests were failing behind a `0`.**
Redirecting instead of piping surfaced them. Already in the corpus as
`feedback_push_pipe_masks_rejection` — *ANY* `cmd | tail` returns tail's status — and it still cost
a round. **Redirect, don't pipe.**

See [[feedback_stderr_is_commentary_not_data]], [[feedback_errexit_kills_assignment_guard]],
[[feedback_prod_hardening_unreachable_in_ci]], [[feedback_calibrate_the_mutation_before_counting_it]].
Org corpus: `feedback_push_pipe_masks_rejection`, `feedback_investigate_before_implement`.
deploy#625/#628/#629, PR #661.
