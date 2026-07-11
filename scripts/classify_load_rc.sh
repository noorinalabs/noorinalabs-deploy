#!/bin/sh
# classify_load_rc.sh — map a graph-loader process exit code to a load-outcome
# decision for the deploy-data-load.yml batch load step (deploy#569 + #570).
#
# THE BUG THIS FIXES
# ------------------
# deploy-data-load.yml used to treat EVERY non-zero loader rc as "batch load
# failed", print an error, and `exit "${RC}"` — which ALSO skipped the post-load
# read-back. That is wrong for one specific, common, load-critical case:
#
#   rc=5  VALIDATION_FINDINGS  — the load SUCCEEDED and is COMMITTED to the
#                               graph; post-load validation merely surfaced
#                               data-quality findings (da#354, routed from
#                               _cmd_load by da#372).
#
# So a fully-committed load was reported as "batch load failed (rc=4)"/(rc=5)
# and the operator never saw the read-back that proves what actually landed.
# For the #928 re-run — whose whole point is a TRUSTWORTHY load outcome — the
# workflow must stop lying: a committed-with-findings load is a WARNING with a
# read-back, not a failure.
#
# THE CONTRACT THIS MAPS TO  (authoritative: da repo src/exit_codes.py, da#384)
# ---------------------------------------------------------------------------
# The loader has a rich exit-code space. The ONLY distinction this classifier
# needs is "did the load COMMIT a graph I should read back, and is the outcome
# acceptable?":
#
#   rc   name (da src/exit_codes.py)   committed?         -> decision
#   ---  --------------------------    ----------------   ----------
#   0    (success)                     graph fully written  SUCCESS
#   5    VALIDATION_FINDINGS           graph committed,     FINDINGS
#                                      findings present
#   1    LOAD_FAILED                   nothing written      FAILURE
#   6    REFUSED_ROWS                  graph MINUS refused   FAILURE  (see note)
#                                      rows (malformed-id
#                                      data loss)
#   8    DB_UNREACHABLE                nothing written      FAILURE
#   3/4/7/9/10/11 (other stages)       n/a for `load`       FAILURE  (fail-closed)
#   any other non-zero                 unknown              FAILURE  (fail-closed)
#
# Note on rc=6 REFUSED_ROWS: the graph IS partially committed, but refused rows
# are silent data loss — an incomplete graph is NOT an acceptable load for a
# re-run that must be trustworthy, so we deliberately classify it FAILURE (fail
# the job, do not bless the partial load). This matches the deploy#569/#570
# direction: the malformed-id class is a true failure, not a findings warning.
#
# Only rc=5 is the "committed but non-fatal" code. Every OTHER non-zero rc maps
# to FAILURE — fail-closed by construction. If da ever adds another
# committed-but-acceptable code, add it here EXPLICITLY (an unlisted new code
# stays a FAILURE, which is the safe direction). da's exit_codes.py commits to
# NOT renumbering existing codes ("The fix is not to renumber"), so hardcoding
# the single value 5 here is stable, not a stale-table hazard.
#
# WHY A STANDALONE SCRIPT (not inline in the YAML): the workflow runs this over
# SSH on the VPS from /opt/noorinalabs-deploy (a git checkout of this repo), so
# the file is present at run time. Extracting it makes the rc->decision mapping
# a single source of truth that is unit-tested directly
# (scripts/tests/test_classify_load_rc.py) rather than re-encoded inside a YAML
# heredoc no test can reach — same pattern as scripts/kafka_logdir_preflight.sh.
#
# POSIX sh on purpose (no bashisms): it is sourced/invoked from the same
# `set -euo pipefail` remote shell as the rest of the load step.
#
# Usage:  classify_load_rc.sh <loader-rc>
# Output: exactly one decision token on stdout — SUCCESS | FINDINGS | FAILURE
# Exit:   0 on a successful classification (the DECISION is the stdout token,
#         never this script's own status); 2 only on misuse (missing or
#         non-integer argument — a caller bug, surfaced fail-closed under set -e).
set -eu

rc="${1:-}"
case "$rc" in
	'')
		echo "usage: classify_load_rc.sh <loader-rc>" >&2
		exit 2
		;;
	*[!0-9]*)
		echo "classify_load_rc.sh: <loader-rc> must be a non-negative integer, got '$rc'" >&2
		exit 2
		;;
esac

case "$rc" in
0) echo "SUCCESS" ;;                      # everything the load meant to write
5) echo "FINDINGS" ;;                     # VALIDATION_FINDINGS — committed, findings present
*) echo "FAILURE" ;;                      # LOAD_FAILED / DB_UNREACHABLE / REFUSED_ROWS / any other non-zero
esac
exit 0
