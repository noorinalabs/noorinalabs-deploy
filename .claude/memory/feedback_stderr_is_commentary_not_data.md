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

## The rule is about the DESTINATION, not the redirect

`2>&1` is not banned. `backup.sh` does `PREFLIGHT_OUT="$(set +e; preflight_b2 2>&1)"` and that
is **correct**, because `PREFLIGHT_OUT` is only ever `printf`'d to the log — commentary in,
commentary out. **The bug is `2>&1` into a variable that is subsequently PARSED.** Ask of every
capture: *is this string going to be read as data?* If yes, stderr must not be in it.

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
