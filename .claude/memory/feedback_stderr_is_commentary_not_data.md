---
name: feedback_stderr_is_commentary_not_data
description: The two streams exist because one is DATA and one is COMMENTARY. `2>/dev/null`/`|| true` DISCARDS the diagnostic; `2>&1` into a variable that is then PARSED promotes it to data — and fires on the SUCCESS path, because tools write to stderr when nothing is wrong. Capture with `2>"$err"` and read it only on failure.
metadata:
  type: feedback
---

**deploy#584, 2026-07-11. Both reviewers blocked on it independently — and it was introduced
BY the fix for the previous bug.**

## The three ways to treat a diagnostic, and only one is right

| | |
|---|---|
| `2>/dev/null`, `\|\| true` | **DISCARDS** the diagnostic — the original bug |
| `2>&1` into a variable that is then **PARSED** | **PROMOTES it to DATA** — the over-correction |
| `2>"$err"`, read on failure | **CAPTURES** it ✅ |

## Why the over-correction is worse than it looks

`restore.sh` configures rclone via `RCLONE_CONFIG_ISNAD_*` and ships no `rclone.conf`, so
rclone writes this to stderr **on every SUCCESSFUL call**:

```
NOTICE: Config file ".../rclone.conf" not found - using defaults
```

`2>&1` folded that line into `$dirs`, and the loop then read it **as a backup directory
name**. Against a **healthy** bucket holding one good complete backup:

```
[WARNING] Skipped 2 INCOMPLETE backup(s) when resolving 'latest':
[WARNING]     incomplete: daily/2026/07/11 05:13:53 NOTICE: Config file … not found
[WARNING]     incomplete: weekly/2026/07/11 05:13:53 NOTICE: Config file … not found
LATEST=daily/2026-07-11
```

It selected the right backup **and invented two incomplete backups that do not exist**. And
`restore.sh --list` printed **a log line to the operator as a backup name**.

> **Note the direction of the regression. The original bug fired only on FAILURE. This one
> fires on SUCCESS** — because a tool writing to stderr when nothing is wrong is completely
> ordinary (warnings, deprecations, progress, config notices). **`2>&1` corrupts the NORMAL
> path, which is the path nobody tests as hard.**

And it corrupted the warning block added for the right reason — *"an operator told nothing
about why the newest backup was passed over will assume the tool is broken"* — so it now told
them **a lie instead of nothing**.

## The rule is about the CONSUMER, not the redirect — corrected twice

**First cut (mine, too narrow):** *"the bug is `2>&1` into a VARIABLE that is subsequently
parsed."* `backup.sh`'s `PREFLIGHT_OUT="$(preflight_b2 2>&1)"` is correct, because that
variable is only ever `printf`'d — commentary in, commentary out.

**Correction (Nino Kavtaradze), and it matters.** `restore.sh::restore_postgres` does
`pg_restore … > "$out" 2>&1` and then **greps `$out`** for `errors ignored on restore: [0-9]+`.
That **is** a parse of merged stderr — **and it is not a bug, it is REQUIRED**, because
`pg_restore` writes that count to stderr. My sweep saw this line, called it "a redirect into a
log file, not a variable parsed as data," and moved on. **Both halves were wrong** — it *is*
parsed, and the destination being a file rather than a variable is irrelevant. **I reached the
right verdict by the wrong reasoning**, which is the same defect this PR kept punishing: a
conclusion that happens to be correct is not evidence the method was.

**The invariant, stated over the CONSUMER:**

> **Merging commentary into data is fatal exactly when the consumer treats EVERY LINE AS A
> RECORD. It is harmless — and sometimes required — when the consumer SEARCHES FOR A SPECIFIC
> KNOWN TOKEN.**

`resolve_latest` iterates `$dirs` line by line and treats each as a backup directory: a
**record** consumer, so one NOTICE line becomes one phantom backup. `restore_postgres` greps
for a fixed pattern: a **search** consumer, so extra lines are inert.

**How to apply:** ask of every capture — **"does my reader ITERATE this, or SEARCH it?"**
Iterate ⇒ stderr must not be in it. Search for a known token ⇒ merging is fine, and may be the
only way to get the token at all. The destination is a red herring: **variable or file, the
question is the same.**

An over-broad rule ("never `2>&1`") is its own harm — it fires on correct code and teaches
people to ignore it.

**How to apply:**

- Capturing a command's output as **data**: `out="$(cmd 2>"$err")" || rc=$?`, then read `$err`
  only on the failing path. Never `2>&1`, never `2>/dev/null`.
- Capturing a command's output as **a diagnostic to print**: `2>&1` is fine.
- Writing a test for it: **the fixture must exercise the SUCCESS path with a tool that writes
  to stderr anyway.** Force the condition deterministically (here: `RCLONE_CONFIG=/nonexistent`
  makes rclone emit its NOTICE on every call) rather than depending on whether the dev box
  happens to have a config file — otherwise the test passes locally and the bug ships.

Same family as [[feedback_errexit_kills_assignment_guard]] (the rc, discarded) and
[[feedback_silent_zero_is_not_a_measurement]] (an empty result that is not a measurement).
The trio is one idea: **an instrument's failure, its commentary, and its data are three
different things, and collapsing any two of them produces a confident lie.**
