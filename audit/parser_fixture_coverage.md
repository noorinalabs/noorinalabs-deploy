# Parser-Fixture Coverage Audit — noorinalabs-deploy

**Audit date:** 2026-05-07
**Auditor:** Bereket Tadesse (P3W7 T1)
**Wave:** P3W7 (parent#300)
**Hook count:** 7 files in `.claude/hooks/` (6 PreToolUse + 1 utility)

---

## Hook Inventory

| # | File | Type | Parser class | Fixture coverage | Notes |
|---|------|------|-------------|-----------------|-------|
| 1 | `validate_commit_identity.py` | PreToolUse (Bash) | Parser | None | Stale local copy; parent version uses shlex tokenizer; local uses regex-only |
| 2 | `validate_pr_ci_status.py` | PreToolUse (Bash) | Parser | None | Stale local copy; parent version has NEUTRAL allowlist (#219); local lacks it |
| 3 | `validate_labels.py` | PreToolUse (Bash) | Parser | None | Stale local copy; parent uses shlex tokenizer with `--repo` pass-through; local uses regex-only |
| 4 | `block_git_config.py` | PreToolUse (Bash) | Parser | None | Stale local copy; parent uses shlex tokenizer; local uses regex-only |
| 5 | `block_no_verify.py` | PreToolUse (Bash) | Parser (light) | None | Stale local copy; parent uses shlex tokenizer; local uses regex-only |
| 6 | `auto_set_env_test.py` | PreToolUse (Bash) | Parser (light) | None | Stale local copy; parent has `gh`/`--body` short-circuit fixes (#114); local lacks them |
| 7 | `annunaki_log.py` | Utility (no PreToolUse) | Non-parser | N/A | Logging utility only; no input parsing |

**Parser-class hooks:** 6 (hooks 1–6)
**Non-parser hooks:** 1 (annunaki_log.py utility)

---

## Critical Finding: Stale Local Copies vs. Authoritative Parent Hooks

The deploy repo's `.claude/settings.json` correctly points ALL hook registrations to the parent repo's hooks at `noorinalabs-main/.claude/hooks/`. However, the local `.claude/hooks/` directory contains **stale copies** of all 6 PreToolUse hooks that diverge from the parent versions. These local copies are:

1. **Not registered** in `settings.json` (unused/orphaned)
2. **Missing parser improvements** shipped to the parent in P3W4–W6:
   - shlex tokenization via `_shell_parse.py` (replaces regex-only parsing)
   - NEUTRAL allowlist logic in `validate_pr_ci_status.py` (#219)
   - `--repo` pass-through in `validate_labels.py`
   - `gh`/`--body` short-circuit in `auto_set_env_test.py` (#114)
   - Push `--no-verify` detection in `block_no_verify.py`

---

## Coverage Table — Parser-Class Hooks

| Hook | Inputs parsed | Known shape gaps | Parent test file exists? | Deploy local test file? |
|------|--------------|-----------------|-------------------------|------------------------|
| `validate_commit_identity.py` | `git -c user.name=X -c user.email=Y commit` command strings; heredoc bodies; cross-repo `cd <path>` | regex misses backslash-continuation (#287 shape); shlex fix shipped to parent only | Yes (`test_validate_commit_identity.py`, 374 lines) | No |
| `validate_pr_ci_status.py` | `gh pr merge` command + `statusCheckRollup` JSON from `gh pr view` | NEUTRAL=pending Chromatic shape (#219) handled in parent only; local copy treats all NEUTRAL as pass | Yes (`test_validate_pr_ci_status.py`, 207 lines) | No |
| `validate_labels.py` | `gh issue create --label` command; `--repo` flag; comma-separated labels | `--body` content leaking into label extraction (#Bug 2 in parent); `--repo` unforwarded to `gh label list` (#Bug 1) — both fixed in parent via shlex tokenizer; local copy vulnerable | Yes (`test_validate_labels.py`, 272 lines) | No |
| `block_git_config.py` | `git config` command + read-only flag detection | `--body`/prose false-positives (#216 shape) fixed in parent via shlex; local copy uses raw-string regex | Yes (`test_block_git_config.py`, 100 lines) | No |
| `block_no_verify.py` | `git commit --no-verify` / `git push --no-verify` | `git push --no-verify` not detected by local copy (only checks `commit`); parent version covers push too; `--body`/prose false-positives fixed in parent | Yes (`test_block_no_verify.py`, 93 lines) | No |
| `auto_set_env_test.py` | pytest / make test command detection | `gh issue comment --body "run pytest"` false-positive (#114); local copy has no short-circuit; parent version short-circuits on `gh` argv[0] and `--body` flag | Yes (`test_auto_set_env_test.py`, 176 lines) | No |

---

## Gap Classification

### Gap 1 — LOCAL COPIES UNUSED BUT DIVERGENT (all 6 parser hooks)

All 6 parser hooks have stale local copies that are neither registered nor tested. The local copies represent a maintenance hazard: they diverge from the parent versions (which have all parser fixes), and their presence creates confusion about which version runs. The parent hooks are authoritative.

**Risk:** If `settings.json` is ever reconfigured to use local hooks (e.g., by a new team member referencing CLAUDE.md), the stale versions would reintroduce known parser bugs.

**Action:** Delete local stale copies OR replace with symlinks to parent hooks.

### Gap 2 — NO FIXTURE TESTS IN DEPLOY REPO

The deploy repo has zero parser fixture tests. All 6 parser hooks are exercised only via parent-repo CI (which has full test suites). The deploy repo has no `hooks-lint` or `hooks-test` CI workflow.

**Risk:** Deploy-repo-specific parser regressions (e.g., a deploy-specific hook added in future) would ship without any test gate.

**Action:** Add `hooks-lint` workflow to deploy repo. Either adopt parent test suite via `git show`/path references or add deploy-local tests.

### Gap 3 — validate_pr_ci_status.py: NEUTRAL allowlist missing in local copy

The local copy treats `NEUTRAL` conclusions as pass for all CI checks. The parent version added the `_NEUTRAL_PENDING_CHECK_NAMES` allowlist (#219) to handle Chromatic visual-regression CI correctly. If the deploy repo ever routes Chromatic checks, the stale local copy would silently pass a pending visual-regression review.

**(Moot while local copy is unused — becomes active risk if settings.json is changed.)**

---

## Pattern G Observations

No in-wave Pattern G fixes were possible: the local copies are unused orphans, so deleting them is the correct action. The gap is structural (stale-copies-no-tests), not a one-liner fix. Tracked via backport issues below.

---

## Backport Issues Filed

- deploy#**TBD** — `bug: stale local hook copies diverge from parent canonical versions — delete orphans` (bug, p3-wave-7)
- deploy#**TBD** — `enhancement: add hooks-lint CI workflow to deploy repo` (enhancement, ci-cd, p3-wave-7)

---

## Summary

- **7 hooks total** in `.claude/hooks/` (6 PreToolUse + 1 utility)
- **6 parser-class hooks** — all with coverage gap: zero local fixture tests; local copies are stale orphans not registered in `settings.json`
- **1 non-parser hook** — `annunaki_log.py` (utility; no input parsing; N/A for fixture coverage)
- **Parent test suites exist** for all 6 parser hooks (374 + 207 + 272 + 100 + 93 + 176 = 1,222 lines total)
- **Local CI gap** — no `hooks-lint`/`hooks-test` workflow in deploy repo
- **Primary recommendation** — delete stale local copies; add CI workflow referencing parent test suite
