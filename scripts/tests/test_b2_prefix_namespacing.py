"""stg and prod share one B2 bucket — the remote path MUST carry the environment (deploy#632).

`BACKUP_B2_BUCKET` is a single repo-level secret. Before this guard, `backup.sh` and
`restore.sh` built every remote path as `${B2_BUCKET}/<category>/<date>`, with no
environment anywhere in it. Nothing collided only because prod had never taken a backup.

The fatal one is retention. `prune_old_backups` listed `${B2_BUCKET}/${category}/` and
`rclone purge`d every date dir past the cutoff — unscoped. **Prod's nightly purge would
have deleted stg's backups, and stg's would have deleted prod's.** A backup system whose
retention job destroys the other environment's backups is worse than none, because it
reports success while doing it.

WHAT A GREEN RUN MEANS — AND WHAT IT DOES NOT
---------------------------------------------
Green here means EXACTLY this, and nothing more:

    *No rclone call in the backup-store scripts addresses the bucket outside a sanctioned
    prefixed root — in ANY of its arguments; every bucket root is DEFINED with the
    environment in it; every rclone token the scanner cannot parse into a checkable call is
    REFUSED rather than skipped; and every one of those scripts refuses to run without a
    prefix, hostile prefixes included.*

It does NOT mean "backups are isolated." Isolation is a RUNTIME CAPABILITY property — one
shared read-write key can still reach across the namespace, and only a prefix-scoped key
(deploy#634) changes that. Post-#633 the backups are **namespaced, not isolated**.

The gap between those two sentences is where deploy#613, #617, #623 and #632 all lived: a
source-text proxy was read as though it were the runtime property it stands in for. Say what
the guard checks; do not let it testify to more.

WHY THE POLARITY IS INVERTED (deploy#637)
-----------------------------------------
This guard used to hunt for the BAD spelling and was blind to variable indirection. It now
enumerates what is ALLOWED and fails CLOSED on anything it does not recognise — see the long
note above `_REMOTE`. Every assertion is calibrated against BOTH classes, including the
literal evasion from the issue body, because a filter nobody has watched go red is not
evidence (deploy#574, #624, #633).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKUP_SH = REPO_ROOT / "scripts" / "backup.sh"
RESTORE_SH = REPO_ROOT / "scripts" / "restore.sh"
B2_PREFLIGHT_SH = REPO_ROOT / "scripts" / "b2_preflight.sh"
VERIFY_ARTIFACT_SH = REPO_ROOT / "scripts" / "verify_b2_backup_artifact.sh"
DEPLOY_STG = REPO_ROOT / ".github" / "workflows" / "deploy-stg.yml"
DEPLOY_PROD = REPO_ROOT / ".github" / "workflows" / "deploy-prod.yml"
VERIFY_ARTIFACT = REPO_ROOT / ".github" / "workflows" / "verify-backup-artifact.yml"
DATA_LOAD = REPO_ROOT / ".github" / "workflows" / "deploy-data-load.yml"
GRAPH_PRUNE = REPO_ROOT / ".github" / "workflows" / "graph-prune-narrators.yml"

# The scripts that BUILD backup-bucket addresses from `${B2_BUCKET}`, and must therefore
# route every one of them through the environment prefix.
#
# `b2_preflight.sh` joins this list in deploy#638. Until now the guard scanned only
# backup.sh and restore.sh, so the preflight — which writes and deletes a canary object on
# EVERY backup — was entirely unguarded, and its canary sat at the bucket root.
#
# verify_b2_backup_artifact.sh is deliberately NOT here: it never constructs a bucket
# address, it receives an already-prefixed `B2_ROOT` from its caller. That is a different
# property, pinned by test_the_artifact_scanner_does_not_build_its_own_bucket_address.
BACKUP_STORE_SCRIPTS = [BACKUP_SH, RESTORE_SH, B2_PREFLIGHT_SH]

# ===========================================================================
# THE POLARITY IS INVERTED ON PURPOSE (deploy#637)
# ===========================================================================
# The first version of this guard HUNTED FOR THE BAD SPELLING: it searched for
# `${B2_BUCKET}/` not followed by a prefix. Two lines defeat that completely:
#
#     PURGE_ROOT="${RCLONE_REMOTE}:${B2_BUCKET}"        # no trailing slash -> never matches
#     rclone purge "${PURGE_ROOT}/${category}/${dir}/"  # contains no B2_BUCKET at all
#
# Invisible to a `${B2_BUCKET}/`-hunting scan, and deploy#632 restored in full. This is not
# hypothetical: `REMOTE_ROOT` in restore.sh is that exact shape, and `B2_ROOT` in
# verify-backup-artifact.yml is too. They are safe only because their DEFINITIONS happen to
# carry the prefix — a property the old scan never checked and could not see.
#
# So we stop enumerating what is FORBIDDEN and enumerate what is ALLOWED
# (`feedback_lint_gate_cover_all_syntactic_forms`). The rule now has two halves, and
# together they fail CLOSED on anything the guard does not recognise:
#
#   1. USAGE — EVERY remote-bearing argument of EVERY `rclone` call must be a SANCTIONED
#              bucket address, and at least one must be. Not "at least one is enough": a
#              call can have a fine SOURCE and a catastrophic DESTINATION —
#              `rclone moveto "${REMOTE_ROOT}/…" "<bucket-root>"` MOVES the environment's
#              own namespace into the shared root, and an at-least-one check passed it on
#              the strength of its source (deploy#637 review, Lucas Ferreira). The
#              at-least-one clause still has to stay, because an EMPTIED root variable makes
#              a call remote-bearing NOWHERE and would satisfy "every remote arg is
#              sanctioned" vacuously.
#   2. DEFINITION — every variable ever ASSIGNED a value containing a remote-spec is a root
#              variable, discovered from the source rather than from a hardcoded list, and
#              its definition must itself be a sanctioned address.
#
# Half 2 is what makes half 1 sound, and it is the `feedback_a_scan_cannot_see_an_emptied_
# string` lesson made mechanical: a bulk find/replace once ate `REMOTE_ROOT`'s own
# definition, every "remote" path silently addressed the LOCAL FILESYSTEM, and the old
# source-text guard went GREEN because the file then contained no bucket path AT ALL. It was
# correctly answering the wrong question. Pin the DEFINITION, not just the usages.
#
# We deliberately do NOT enumerate rclone's verbs. A verb list is a denylist wearing a
# different hat — the day someone reaches for `rcat` or `moveto`, an unlisted verb is a
# silent hole. Any rclone subcommand is scanned.
#
# And — the lesson that cost this guard a review round — that argument applies with EQUAL
# force to the COMMAND-PREFIX CONTEXT. The first cut recognised `rclone` only after a fixed
# set of shell operators and skipped it everywhere else, which is the same denylist one
# level up: `timeout 300 rclone purge …` walked straight through it. There is no anchor now.
# See the note above `_RCLONE_VERB`.

# The BACKUP store is the rclone remote `isnad` (backup.sh and restore.sh hardcode it;
# b2_preflight.sh defaults RCLONE_REMOTE to it).
#
# THE DISCRIMINATOR IS THE REMOTE NAME, NOT THE VARIABLE NAME. There is a second, unrelated
# B2 store in this repo — the read-only Parquet PIPELINE bucket, reached as
# `pipeline:noorinalabs-pipeline` by deploy-data-load.yml and graph-prune-narrators.yml.
# Those workflows locally assign `B2_BUCKET="noorinalabs-pipeline"`, so a guard keyed on the
# NAME `B2_BUCKET` would false-positive on a bucket that has no environment namespace and
# needs none. `test_the_pipeline_store_is_a_different_remote` below pins that separation, so
# "out of scope" is a verified fact rather than an assumption — and the day anything points
# those files at `isnad:`, that test goes red and they come into scope.
# The braced form must actually CLOSE (`${RCLONE_REMOTE}`) before the colon, and the colon
# must not be a bash parameter-expansion operator. Both matter, and both were found by
# running this detector against the real files rather than against fixtures:
#
#   RCLONE_REMOTE="${RCLONE_REMOTE:-isnad}"    <-- `:` here is the `:-` DEFAULT operator
#
# A sloppier `\$\{?RCLONE_REMOTE\}?:` matches that line and reports the remote's own name as
# an unprefixed bucket root. False alarms are not a harmless failure mode: a guard that cries
# wolf on correct code is a guard someone disables.
_REMOTE = r"(?:\$\{(?:RCLONE_REMOTE|remote)\}|\$(?:RCLONE_REMOTE|remote)\b|\bisnad\b)"
_REMOTE_SPEC = re.compile(_REMOTE + r":(?![-=?+])")

# A SANCTIONED address: the backup remote, the bucket, then the ENVIRONMENT NAMESPACE —
# either spelled literally (`${BACKUP_PREFIX}`) or carried by `REMOTE_SUBDIR`, whose own
# definition is pinned by test_remote_subdir_is_built_from_the_prefix.
#
# Both expansion spellings, braced and bare (`${B2_BUCKET}` and `$B2_BUCKET`) — the bare `$`
# form is one character shorter, semantically identical, idiomatic in these very files, and
# it is what slipped past the braced-only detector in the #633 review (Lucas Ferreira).
_SANCTIONED_INLINE = re.compile(
    _REMOTE + r":\$\{?B2_BUCKET\}?/\$\{(?:BACKUP_PREFIX|REMOTE_SUBDIR)\}"
)

# A bare remote with no bucket path at all — `"${remote}:"`. This addresses the ACCOUNT, not
# the bucket. Only the allowlisted capability probe may use it (see _SANCTIONED_EXCEPTIONS).
_BARE_REMOTE = re.compile(_REMOTE + r":/?$")

_LEADING_VAR = re.compile(r"^\$\{?(\w+)\}?")

# THE COMMAND-PREFIX ANCHOR WAS A DENYLIST ONE LEVEL UP (deploy#637 review, Lucas Ferreira)
#
# The first cut of this guard recognised `rclone` only after `^`, `$(`, `||`, `&&`, `;`,
# `if`, `then`, `elif`, `!` — and SKIPPED anything else instead of flagging it. Which means
# I argued, correctly, that a VERB list is a denylist wearing a different hat, and then wrote
# a CONTEXT list that is the identical mistake one layer up. Lucas put the #632 unscoped
# purge back into the real backup.sh with nothing in front of it but `timeout 300 ` — a
# nightly retention job deleting the other environment's backups — and the suite said
# `23 passed`. `sudo rclone`, `eval rclone`, `for … do rclone`, `| rclone rcat`,
# `xargs -I{} rclone` and `RC=rclone; $RC purge` all sailed through the same hole.
#
# So there is no anchor any more. ANY bare `rclone` token outside a DOUBLE-quoted string, on
# a non-comment logical line, is a call — and if the scanner cannot parse it into a shape it
# recognises, that is an OFFENDER ("unrecognised call shape"), never a skip. Fail closed on
# what you cannot read; do not wave it through.
#
# DOUBLE-quoted, and only double-quoted. A SINGLE-quoted region is not prose — it is CODE
# that `eval`, `trap` and `bash -c` execute, and `b2_preflight.sh` already uses that idiom.
# Skipping single quotes made `eval 'rclone purge …'`, `trap 'rclone purge …' EXIT` and
# `RC='rclone'` invisible. See the long note on `_rclone_token_positions`.
#
# The DOUBLE-quote exclusion is what keeps the scanner off the prose, and it does that job
# without an anchor: b2_preflight.sh's own diagnostics say things like
# `echo "...retention prune uses 'rclone purge' and will fail silently-late"`, and that
# `rclone` sits inside a double-quoted string — the apostrophes around it are irrelevant.
#
# Command substitution re-enters UNQUOTED context, which matters enormously: every real call
# in restore.sh is `out="$(rclone lsf …)"` — the token is inside double quotes but inside a
# `$( )` within them. A naive quote-tracker would skip all four of restore.sh's rclone calls
# and the file would go green while saying nothing at all.
_RCLONE_VERB = re.compile(r"rclone\s+(\w[\w-]*)\s*(.*)$", re.DOTALL)

# Shell-ish argument tokens: a double-quoted string, or a bare word.
_ARG = re.compile(r'"([^"]*)"|(\S+)')

# The two places these scripts NAME rclone without CALLING it. Explicit, reasoned, and
# checked against the token's own position — not a blanket line pass. Anything else that
# fails to parse is an offender.
_NON_CALL_CONTEXTS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"command\s+-v\s+rclone\b"),
        "PRESENCE CHECK. `command -v rclone` asks whether the binary exists on PATH; it "
        "invokes nothing and addresses no bucket. b2_preflight.sh uses it so that a missing "
        "rclone is reported as PROBE_FAILED rather than as a bad credential (deploy#613).",
    ),
    (
        re.compile(r"\bfor\s+cmd\s+in\b[^;]*\brclone\b"),
        "DEPENDENCY LOOP. backup.sh's `for cmd in docker rclone zstd sha256sum` names rclone "
        "among the binaries it requires and checks each with `command -v`. It invokes "
        "nothing.",
    ),
    (
        re.compile(r"\bREQUIRED_CMDS\+?=\([^)]*\brclone\b"),
        "DEPENDENCY ARRAY. restore.sh appends rclone to REQUIRED_CMDS (only on the B2 path — "
        "a local-dir restore must not demand it) and then checks each entry with "
        "`command -v`. It invokes nothing. Found by the fail-closed scanner itself: the "
        "shape was unparseable, so it was refused rather than skipped, which is the whole "
        "point of the rewrite.",
    ),
]


def _rclone_token_positions(line: str) -> list[int]:
    """Offsets of every bare `rclone` token that is CODE rather than prose.

    A SINGLE-QUOTED REGION IS NOT PROSE — IT IS CODE SOMEONE ELSE EXECUTES (deploy#637
    review round 3, Lucas Ferreira). This scanner used to skip single-quoted regions the way
    it skips double-quoted ones, and that made all of these invisible in the real backup.sh
    while the suite stayed at 49 passed:

        eval    'rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/"'
        trap    'rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/"' EXIT
        bash -c 'rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/"'
        RC='rclone'                       # <-- ONE PAIR OF QUOTES walks straight through
        $RC purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/"

    The fourth is the one that indicts the previous round: I closed `RC=rclone` by making an
    unparseable shape an offender, and a single pair of quotes reopened it.

    And this is NOT contrived, because `b2_preflight.sh` — the file THIS PR brings into the
    guard's scope — already uses the idiom, with a comment correctly explaining why:

        # Single-quoted so $tmp expands when the trap FIRES, not when it is set.
        trap 'rm -rf -- "$tmp"' RETURN

    Twenty lines below that sits the canary `copyto`/`deletefile`. The obvious next change to
    the file — "an interrupted preflight orphans its canary, so fold the delete into the
    existing RETURN trap" — is written by extending that single-quoted trap, and a guard that
    cannot see inside single quotes says NOTHING. That is deploy#638 reintroduced invisibly by
    a maintainer following the file's own documented idiom. Not an attack: the path of least
    resistance.

    So there is no single-quote toggle. Only DOUBLE-quoted regions are excluded — that is what
    holds the operator prose (`echo "...uses 'rclone purge' and will fail..."`), and it still
    does, because that `rclone` is inside double quotes regardless of the apostrophes around
    it.

    `$( … )` re-enters unquoted context: every real call in restore.sh is
    `out="$(rclone lsf …)"`, and without the re-entry all four are invisible and the file goes
    green by saying nothing at all.
    """
    positions: list[int] = []
    in_double = False
    stack: list[bool] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == "\\":
            i += 2
            continue
        if line.startswith("$(", i):
            stack.append(in_double)
            in_double = False
            i += 2
            continue
        if c == ")" and stack and not in_double:
            in_double = stack.pop()
            i += 1
            continue
        if c == '"':
            in_double = not in_double
            i += 1
            continue
        if not in_double and line.startswith("rclone", i):
            before = line[i - 1] if i else " "
            after = line[i + 6] if i + 6 < n else " "
            # A whole token: not `/usr/bin/rclone`, not `rclone_helper`. A leading `'` is NOT
            # excluded — `RC='rclone'` must be seen.
            if not (before.isalnum() or before in "_-./") and not (
                after.isalnum() or after in "_-"
            ):
                positions.append(i)
            i += 6
            continue
        i += 1
    return positions


# Assignment, including the `local`/`export` forms.
_ASSIGN = re.compile(r"^\s*(?:local\s+|export\s+|declare\s+)?(\w+)=(.*)$")

# ---------------------------------------------------------------------------
# Sanctioned exceptions. Each needs a WRITTEN REASON — an allowlist entry with a reason is
# fine; a blanket pass is not. Keyed by (filename, rclone verb).
#
# An exempted call is still not a free pass: _offending_rclone_calls verifies that every
# remote-bearing argument of an exempted call is a BARE remote (no bucket path). So the day
# `lsd "${remote}:"` becomes `lsd "${remote}:${B2_BUCKET}/"`, the exemption stops applying
# and the call is an offender again.
# ---------------------------------------------------------------------------
_SANCTIONED_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("b2_preflight.sh", "lsd"): (
        'BUCKET-ROOT CAPABILITY PROBE. `rclone lsd "${remote}:"` enumerates BUCKETS at the '
        "account level — it addresses no path inside the bucket at all, and it must address "
        "the account to answer the one question it exists to answer: can this key see this "
        "bucket? A bucket-scoped key lists only its own bucket, so visibility here is what "
        "distinguishes a wrongly-scoped key from a missing bucket — the two cases rclone's "
        "error text renders IDENTICALLY (deploy#559). Scoping this probe to a prefix would "
        "destroy the signal it was built for. It reads only: it writes nothing and deletes "
        "nothing, so it cannot touch the other environment's objects."
    ),
}


def _root_variables(text: str) -> dict[str, str]:
    """Every variable ASSIGNED a value containing a remote-spec, discovered from the source.

    This is the half of the guard that cannot be evaded by inventing a new name. The old
    scan knew only the names its author had thought of; this one finds `PURGE_ROOT` the
    moment it is defined, because it is defined in terms of the remote.
    """
    roots: dict[str, str] = {}
    for _lineno, line in _logical_lines(text):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        m = _ASSIGN.match(line)
        if not m:
            continue
        name, rhs = m.group(1), m.group(2).strip()
        rhs = rhs.split("#", 1)[0].strip().strip('"').strip("'")

        # A COMMAND SUBSTITUTION IS NOT A ROOT DEFINITION. `dirs=$(rclone lsf "<remote>:...")`
        # captures rclone's OUTPUT; `dirs` holds a directory LISTING, not a remote path, and
        # treating it as a bucket root reports the retention listing — correctly prefixed —
        # as an unprefixed root. The rclone call inside the substitution is still scanned as
        # a call, which is the half that actually matters here.
        if rhs.startswith(("$(", "`")):
            continue

        if _REMOTE_SPEC.search(rhs):
            roots[name] = rhs
    return roots


def _unsanctioned_root_definitions(text: str) -> list[str]:
    """Root variables whose DEFINITION does not name the environment."""
    return [
        f"{name}={rhs!r}"
        for name, rhs in _root_variables(text).items()
        if not _SANCTIONED_INLINE.match(rhs) and not _BARE_REMOTE.match(rhs)
    ]


def _sanctioned_roots(text: str) -> set[str]:
    """Root variables whose definition IS sanctioned — safe to root an rclone call in."""
    return {name for name, rhs in _root_variables(text).items() if _SANCTIONED_INLINE.match(rhs)}


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join shell line-continuations, keeping the line number of the line that STARTED it.

    `rclone` calls in these scripts wrap: backup.sh's upload puts its two path arguments on
    the first physical line and its flags on the next. But nothing stops the remote argument
    itself from landing on a continuation line —

        rclone purge \\
            "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/"

    — and a strictly line-based scanner would see `rclone purge \\` with no arguments at all.
    That direction happens to fail CLOSED here (no sanctioned root => offender), which is the
    right way round to be wrong, but it would also fire on a perfectly correct wrapped call.
    Join the continuations and the question does not arise.
    """
    out: list[tuple[int, str]] = []
    buf = ""
    start = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not buf:
            start = lineno
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        out.append((start, buf + line))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def _rclone_args(rest: str) -> list[str]:
    """The arguments of an rclone call, minus flags and shell noise."""
    args = []
    for quoted, bare in _ARG.findall(rest):
        if quoted:
            args.append(quoted)
            continue
        if bare.startswith("-") or bare in {"\\", "|", "||", "&&", ";"}:
            continue
        if bare.startswith((">", "<", "2>", "1>")) or bare.endswith(("2>&1",)):
            continue
        args.append(bare)
    return args


def _is_sanctioned_arg(arg: str, sanctioned: set[str]) -> bool:
    """Does this argument address the bucket through a sanctioned prefixed root?"""
    if _SANCTIONED_INLINE.match(arg):
        return True
    var = _LEADING_VAR.match(arg)
    return bool(var and var.group(1) in sanctioned)


def _is_remote_bearing(arg: str, roots: dict[str, str]) -> bool:
    """Does this argument address the BUCKET at all — inline, or through a known root var?"""
    if _REMOTE_SPEC.search(arg):
        return True
    var = _LEADING_VAR.match(arg)
    return bool(var and var.group(1) in roots)


def _offending_rclone_calls(text: str, filename: str) -> list[str]:
    """rclone calls that do not address the bucket through a SANCTIONED prefixed root.

    FAILS CLOSED, in three directions:

      * an rclone token the scanner cannot parse into a call is an OFFENDER, not a skip
        (there is no command-prefix anchor — `timeout 300 rclone purge …` is a call);
      * EVERY remote-bearing argument must be sanctioned, not merely one of them
        (`rclone moveto "${REMOTE_ROOT}/…" "<bucket-root>"` has a fine source and a
        catastrophic destination, and "at least one" passed it);
      * a call rooted in a variable the guard has never heard of is an OFFENDER.
    """
    roots = _root_variables(text)
    sanctioned = _sanctioned_roots(text)
    offenders: list[str] = []

    for lineno, line in _logical_lines(text):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue

        # EVERY rclone token on the line, not just the first. `_RCLONE_CALL.search()` returned
        # only the first match, so a second `rclone purge` on the same logical line inherited
        # the first call's sanctioned argument and went green (deploy#637 review).
        for pos in _rclone_token_positions(line):
            non_call = next(
                (
                    reason
                    for pat, reason in _NON_CALL_CONTEXTS
                    if any(m.start() <= pos < m.end() for m in pat.finditer(line))
                ),
                None,
            )
            if non_call:
                continue

            m = _RCLONE_VERB.match(line, pos)
            if not m:
                # UNRECOGNISED CALL SHAPE. `RC=rclone; $RC purge …`, a bare `rclone` with no
                # verb, anything the parser cannot read. Refuse it — do not wave it through
                # because it did not fit the shape we happened to anticipate.
                offenders.append(
                    f"{lineno}: UNRECOGNISED rclone CALL SHAPE — the guard cannot parse this "
                    f"into a call it can check, so it refuses to call it safe. If this is not "
                    f"a call, add it to _NON_CALL_CONTEXTS with a reason — {stripped}"
                )
                continue

            verb, rest = m.group(1), m.group(2)
            args = _rclone_args(rest)
            exception = _SANCTIONED_EXCEPTIONS.get((filename, verb))

            if exception:
                # Exempt — but ONLY for the bare-remote shape the reason describes. An
                # exempted verb that grows a bucket path is an offender again.
                bad = [a for a in args if _REMOTE_SPEC.search(a) and not _BARE_REMOTE.match(a)]
                if bad:
                    offenders.append(
                        f"{lineno}: rclone {verb} is allowlisted ONLY as a bare-remote probe, "
                        f"but it now addresses a bucket path: {bad!r} — {stripped}"
                    )
                continue

            # (1) EVERY remote-bearing argument must be sanctioned — source AND destination.
            unsanctioned = [
                a
                for a in args
                if _is_remote_bearing(a, roots) and not _is_sanctioned_arg(a, sanctioned)
            ]
            if unsanctioned:
                for a in unsanctioned:
                    var = _LEADING_VAR.match(a)
                    if _REMOTE_SPEC.search(a):
                        why = "addresses the bucket but does not name the environment"
                    else:
                        assert var is not None
                        why = (
                            f"is rooted in {var.group(1)}={roots[var.group(1)]!r}, whose "
                            f"DEFINITION does not name the environment"
                        )
                    offenders.append(f"{lineno}: rclone {verb}: argument {a!r} {why} — {stripped}")
                continue

            # (2) …and at least one argument must actually BE a sanctioned root. Without this,
            # an emptied root variable (deploy#633) makes the call remote-bearing NOWHERE and
            # rule (1) passes it vacuously — the file contains no bucket path at all, which is
            # exactly the emptied-string failure this guard exists to survive.
            if not any(_is_sanctioned_arg(a, sanctioned) for a in args):
                offenders.append(
                    f"{lineno}: rclone {verb}: no argument names a sanctioned prefixed root — "
                    f"the guard does not recognise where this call points, so it refuses to "
                    f"call it safe — {stripped}"
                )

    return offenders


# ---------------------------------------------------------------------------
# Calibrate the detector BEFORE reading it (deploy#574, #624).
# ---------------------------------------------------------------------------
# The must-flag fixtures are the REAL pre-fix lines, copied verbatim out of git history —
# not names invented here. Invented fixtures inherit the author's assumptions and share the
# guard's blind spot by construction; that is precisely how the #624 filter passed its own
# calibration while letting deploy#613 through.

# `noqa: E501` throughout: these fixtures are production lines copied VERBATIM. Reflowing
# them to satisfy a width limit would make them no longer the thing they are testing —
# which is the entire point of deriving fixtures from real artifacts rather than inventing
# them (deploy#624).
#
# The fixtures are SNIPPETS, not single lines, because the evasion this guard exists to
# catch IS a two-line construct: the root is emptied of the bucket in one line and used in
# another. A single-line fixture cannot express it — which is precisely why the old
# single-line detector could not see it.
PRE_FIX_SNIPPETS_MUST_FLAG: list[tuple[str, str]] = [
    (
        "deploy#637 — THE INDIRECTION EVASION, verbatim from the issue body. Defeats a "
        "`${B2_BUCKET}/`-hunting scan completely: the definition has no trailing slash so it "
        "never matches, and the usage contains no B2_BUCKET at all. This is deploy#632 — the "
        "unscoped purge that deletes the other environment's backups — fully invisible.",
        """
PURGE_ROOT="${RCLONE_REMOTE}:${B2_BUCKET}"
rclone purge "${PURGE_ROOT}/${category}/${dir}/" 2>>"$LOG_FILE" || true
""",
    ),
    (
        "deploy#638 — b2_preflight.sh's OWN canary, at the BUCKET ROOT, outside every "
        "prefix. The one remaining unprefixed write+delete, running on every backup.",
        """
canary=".backup-preflight/canary-$(date -u +%Y%m%d-%H%M%S)-$$"
rclone copyto "${tmp}/canary" "${remote}:${B2_BUCKET}/${canary}" > "${tmp}/write.out" 2>&1
""",
    ),
    (
        "deploy#633 — THE EMPTIED STRING. A bulk find/replace ate REMOTE_ROOT's own "
        "definition, so every 'remote' path addressed the LOCAL FILESYSTEM. The old "
        "source-text guard went GREEN because the file then contained no bucket path AT ALL "
        "— it was correctly answering the wrong question. Only shellcheck's SC2034 noticed.",
        """
REMOTE_ROOT="${REMOTE_ROOT}/${BACKUP_PREFIX}"
rclone copy "${REMOTE_ROOT}/${BACKUP_PATH}/" "$RESTORE_DIR/" --log-level INFO
""",
    ),
    (
        "A BRAND-NEW root variable the guard has never heard of, carrying no prefix. The "
        "fail-closed property: the guard must refuse what it does not recognise, rather "
        "than pass it.",
        """
ARCHIVE_ROOT="isnad:${B2_BUCKET}"
rclone sync "$LOCAL_BACKUP_PATH" "${ARCHIVE_ROOT}/${category}/"
""",
    ),
    (
        "An rclone verb NOT in any verb list anyone would think to write (`rcat`), rooted "
        "nowhere the guard recognises. Enumerating verbs would have missed this.",
        """
rclone rcat "${SOME_OTHER_ROOT}/${category}/note.txt" < /dev/null
""",
    ),
    # backup.sh — the destructive one. This is the purge that deletes the other env.
    (
        "deploy#632 pre-fix backup.sh — the unscoped retention listing.",
        '    dirs=$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/" --dirs-only 2>/dev/null || true)',  # noqa: E501
    ),
    (
        "deploy#632 pre-fix backup.sh — the unscoped purge itself.",
        '                    rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/" 2>>"$LOG_FILE" || \\',  # noqa: E501
    ),
    (
        "deploy#632 pre-fix backup.sh — the upload.",
        '    if rclone copy "$LOCAL_BACKUP_PATH" "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_SUBDIR}/" \\',  # noqa: E501
    ),
    (
        "deploy#632 pre-fix restore.sh — resolve_latest listing.",
        '        dirs="$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/" --dirs-only 2>"$lerr")" || lrc=$?',  # noqa: E501
    ),
    (
        "deploy#632 pre-fix restore.sh — the download.",
        '    if ! rclone copy "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PATH}/" "$RESTORE_DIR/" --log-level INFO; then',  # noqa: E501
    ),
    (
        "THE BARE-`$` SPELLING. Not a hypothetical: the braced-only detector called this "
        "SAFE, and it is deploy#632 restored verbatim (#633 review, Lucas Ferreira).",
        '                    rclone purge "${RCLONE_REMOTE}:$B2_BUCKET/${category}/${dir}/" 2>>"$LOG_FILE" || \\',  # noqa: E501
    ),
    (
        "The unscoped purge HIDDEN ON A CONTINUATION LINE. Joining continuations must not "
        "become a way to smuggle one past the scanner — the evasion has to stay RED in the "
        "wrapped form too, or _logical_lines is an escape hatch rather than a fix.",
        """
rclone purge \\
    "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/" 2>>"$LOG_FILE"
""",
    ),
    # -----------------------------------------------------------------------
    # deploy#637 REVIEW (Lucas Ferreira) — the COMMAND-PREFIX ANCHOR was a denylist one
    # level up. Every shape below defeated the anchored scanner completely: it recognised
    # `rclone` only after ^, $(, ||, &&, ;, if, then, elif, ! and SKIPPED everything else.
    # Lucas put the first one into the real backup.sh and the suite reported `23 passed`.
    # -----------------------------------------------------------------------
    (
        "ANCHOR EVASION — `timeout 300 rclone purge`. Lucas's exhibit: a nightly retention "
        "job deleting the OTHER environment's backups, and the suite said 23 passed.",
        '    timeout 300 rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/"',
    ),
    (
        "ANCHOR EVASION — `sudo rclone`.",
        '    sudo rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/"',
    ),
    (
        "ANCHOR EVASION — `eval rclone`.",
        '    eval rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/"',
    ),
    (
        "ANCHOR EVASION — inside a `for` body, no separator before the call.",
        '    for dir in $dirs; do rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/"; done',  # noqa: E501
    ),
    (
        "ANCHOR EVASION — the call is a PIPELINE consumer.",
        '    printf x | rclone rcat "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/note.txt"',
    ),
    (
        "ANCHOR EVASION — `xargs -I{} rclone`.",
        '    echo "$dirs" | xargs -I{} rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/{}/"',  # noqa: E501
    ),
    (
        "ANCHOR EVASION — the binary is smuggled through a variable: `RC=rclone; $RC purge`. "
        "There is no parseable call here at all, which is exactly why it must be an "
        "UNRECOGNISED CALL SHAPE and not a skip.",
        '    RC=rclone; $RC purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/"',
    ),
    # -----------------------------------------------------------------------
    # deploy#637 REVIEW — "at least one sanctioned argument" is NOT "every remote argument
    # is sanctioned". The rooted check broke on the first sanctioned arg and continued.
    # -----------------------------------------------------------------------
    (
        "TWO-ARG, BAD DESTINATION — a fine SOURCE and a catastrophic DESTINATION. `moveto` "
        "MOVES: this empties the environment's own namespace into the shared bucket root. "
        "The old check passed it on the strength of its source argument alone.",
        """
REMOTE_ROOT="${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}"
rclone moveto "${REMOTE_ROOT}/${category}/" "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/"
""",
    ),
    (
        "TWO CALLS ON ONE LOGICAL LINE, the second unscoped. `_RCLONE_CALL.search()` returned "
        "only the FIRST match, so the second purge INHERITED the first's sanctioned argument "
        "and went green. Lucas injected exactly this into the real backup.sh: 23 passed.",
        '    rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}/${category}/" && rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/"',  # noqa: E501
    ),
    (
        'AN UNSCOPED CALL NESTED IN `"$( )"` — the NON-VACUOUS control for the quote '
        "tracker. The matching must-NOT-flag fixture cannot prove the tracker SEES these: if "
        "it wrongly skipped `$( )` tokens the snippet would contain no calls, produce no "
        "offenders, and pass GREEN anyway. Only an EVASION nested this way can separate 'the "
        "scanner looked and approved' from 'the scanner never looked' — and every real call "
        "in restore.sh has this shape.",
        '    out="$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/" --dirs-only 2>"$err")" || rc=$?',  # noqa: E501
    ),
    # -----------------------------------------------------------------------
    # deploy#637 REVIEW ROUND 3 (Lucas Ferreira) — A SINGLE-QUOTED REGION IS NOT PROSE.
    # It is CODE that eval / trap / bash -c EXECUTE. The scanner skipped single-quoted
    # regions exactly as it skipped double-quoted ones, so every one of these was invisible
    # in the real backup.sh while the suite reported `49 passed`.
    # -----------------------------------------------------------------------
    (
        "SINGLE-QUOTED — `eval '…'`. The string is not text; eval executes it.",
        """    eval 'rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/"'""",
    ),
    (
        "SINGLE-QUOTED — `trap '…' EXIT`. Deferred, and it still deletes.",
        """    trap 'rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/"' EXIT""",
    ),
    (
        "SINGLE-QUOTED — `bash -c '…'`.",
        """    bash -c 'rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/"'""",
    ),
    (
        "THE ONE-QUOTE-PAIR BYPASS OF MY OWN FIX. Round 2 closed `RC=rclone` by making an "
        "unparseable shape an OFFENDER. `RC='rclone'` walks straight back through it: the "
        "quotes hid the token from the scanner entirely, so there was no shape left to refuse.",
        "    RC='rclone'\n    $RC purge \"${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/\"",
    ),
    (
        "WHY THIS IS NOT CONTRIVED — it is the change b2_preflight.sh is ASKING someone to "
        "make. That file already has `trap 'rm -rf -- \"$tmp\"' RETURN`, single-quoted, with "
        "a comment explaining that single-quoting is the correct idiom for deferred "
        "expansion. Twenty lines below it sits the canary. The obvious next edit — 'an "
        "interrupted preflight orphans its canary, so fold the delete into the existing "
        "RETURN trap' — reintroduces deploy#638 INVISIBLY, written by a maintainer following "
        "the file's own documented idiom. The path of least resistance, not an attack.",
        '    trap \'rm -rf -- "$tmp"; rclone deletefile "${remote}:${B2_BUCKET}/${canary}"\' RETURN',  # noqa: E501
    ),
]

POST_FIX_SNIPPETS_MUST_NOT_FLAG: list[tuple[str, str]] = [
    (
        "backup.sh retention listing, prefixed.",
        '    dirs=$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}/${category}/" --dirs-only 2>/dev/null || true)',  # noqa: E501
    ),
    (
        "backup.sh purge, prefixed.",
        '                    rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}/${category}/${dir}/" 2>>"$LOG_FILE" || \\',  # noqa: E501
    ),
    (
        "backup.sh upload via REMOTE_SUBDIR, whose definition carries the prefix.",
        '    if rclone copy "$LOCAL_BACKUP_PATH" "${RCLONE_REMOTE}:${B2_BUCKET}/${REMOTE_SUBDIR}/" \\',  # noqa: E501
    ),
    (
        "restore.sh routes everything through REMOTE_ROOT, defined WITH the prefix, so the "
        "bucket name does not appear in the path at all. The definition is what makes the "
        "usage safe — and the guard now checks it.",
        """
REMOTE_ROOT="${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}"
rclone copy "${REMOTE_ROOT}/${BACKUP_PATH}/" "$RESTORE_DIR/" --log-level INFO
""",
    ),
    (
        "deploy#638 — the FIXED canary, inside the environment's own namespace. Its remote "
        "argument sits on a shell LINE CONTINUATION, which is also the regression test for "
        "_logical_lines: a strictly line-based scanner sees `rclone copyto \\` with no "
        "remote argument at all and cries wolf on correct code.",
        """
canary=".backup-preflight/canary-$(date -u +%Y%m%d-%H%M%S)-$$"
rclone copyto "${tmp}/canary" \\
    "${remote}:${B2_BUCKET}/${BACKUP_PREFIX}/${canary}" > "${tmp}/write.out" 2>&1
""",
    ),
    # -----------------------------------------------------------------------
    # THE FALSE-ALARM SIDE of dropping the command-prefix anchor. Without the anchor the
    # scanner sees every `rclone` token in the file — including the ones that are PROSE and
    # the ones that NAME the binary without calling it. A guard that cries wolf on correct
    # code is a guard someone disables, so these are calibrated as carefully as the reds.
    # -----------------------------------------------------------------------
    (
        "PROSE. b2_preflight.sh's own operator diagnostics talk ABOUT rclone. The token is "
        "inside a double-quoted string, which is what keeps the scanner off it now that "
        "there is no command-prefix anchor.",
        """
echo "backup.sh's retention prune uses 'rclone purge' and will fail silently-late."
echo "rclone reports a read-only key's 401 as 'failed to create bucket'. Ignore it."
""",
    ),
    (
        "PRESENCE CHECK. `command -v rclone` invokes nothing.",
        "    if ! command -v rclone > /dev/null 2>&1; then",
    ),
    (
        "DEPENDENCY LOOP. backup.sh names rclone among its required binaries.",
        "for cmd in docker rclone zstd sha256sum; do",
    ),
    (
        "DEPENDENCY ARRAY. restore.sh appends rclone to REQUIRED_CMDS on the B2 path.",
        "    REQUIRED_CMDS+=(rclone)",
    ),
    (
        "THE REAL SINGLE-QUOTED TRAP in b2_preflight.sh. Removing the single-quote toggle "
        "must not make this fire: it contains no rclone token at all. This is the file's own "
        "documented idiom and it is CORRECT — the hazard is what someone might ADD to it, "
        "not the line itself.",
        "    trap 'rm -rf -- \"$tmp\"' RETURN",
    ),
    (
        "COMMAND SUBSTITUTION INSIDE DOUBLE QUOTES. Every real call in restore.sh is this "
        'shape — the token sits inside `".."` but inside a `$( )` within them. A naive '
        "quote-tracker would skip all four of restore.sh's calls and the file would go GREEN "
        "by saying nothing at all, which is the emptied-string failure wearing a new hat.",
        """
REMOTE_ROOT="${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}"
out="$(rclone lsf "${REMOTE_ROOT}/${category}/" --dirs-only 2>"$err")" || rc=$?
""",
    ),
]


def test_the_detector_separates_prefixed_from_unprefixed() -> None:
    """The instrument must go RED on every evasion and GREEN on the fixed forms.

    Run it over BOTH classes and require SEPARATION. A detector that has only ever been
    watched agreeing with the code it was written beside has demonstrated nothing — and this
    guard has now shipped INERT twice (deploy#574, #624, #633). A guard you did not watch
    fail is not a guard.
    """
    missed = [
        f"{why}\n      {snippet.strip()}"
        for why, snippet in PRE_FIX_SNIPPETS_MUST_FLAG
        if not _offending_rclone_calls(snippet, "fixture.sh")
    ]
    assert not missed, (
        "The detector FAILED TO FLAG a known evasion — it is inert, and every assertion "
        "below it is worthless:\n  " + "\n  ".join(missed)
    )

    false_alarms = [
        f"{why}\n      {snippet.strip()}"
        for why, snippet in POST_FIX_SNIPPETS_MUST_NOT_FLAG
        if _offending_rclone_calls(snippet, "fixture.sh")
    ]
    assert not false_alarms, (
        "The detector flagged a CORRECT prefixed form — it would block the fix:\n  "
        + "\n  ".join(false_alarms)
    )


def test_the_detector_catches_the_root_definition_itself() -> None:
    """Half 2 of the guard, calibrated separately: PIN THE DEFINITION.

    `_unsanctioned_root_definitions` is what makes an unrecognised root impossible to
    introduce quietly. Watch it fail before trusting it.
    """
    assert _unsanctioned_root_definitions('PURGE_ROOT="${RCLONE_REMOTE}:${B2_BUCKET}"'), (
        "The root-definition check did not flag a bucket root defined WITHOUT the "
        "environment prefix — deploy#637's evasion is wide open."
    )
    assert _unsanctioned_root_definitions('ARCHIVE_ROOT="isnad:${B2_BUCKET}/${category}"'), (
        "The root-definition check did not flag a literal-remote root without the prefix."
    )
    assert not _unsanctioned_root_definitions(
        'REMOTE_ROOT="${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}"'
    ), "The root-definition check flagged the CORRECT definition — it would block the fix."
    assert not _unsanctioned_root_definitions('remote="${RCLONE_REMOTE}"'), (
        "The root-definition check flagged a bare remote name carrying no bucket path."
    )

    # THE TWO FALSE-POSITIVE CLASSES, both found by running this detector against the REAL
    # files rather than against fixtures I invented. A guard that cries wolf on correct code
    # is a guard someone eventually disables, so its false alarms are calibrated as carefully
    # as its true ones.
    assert not _unsanctioned_root_definitions(
        '    dirs=$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}/${category}/" --dirs-only 2>/dev/null || true)'  # noqa: E501
    ), (
        "A COMMAND SUBSTITUTION was read as a root definition. `dirs` holds rclone's OUTPUT "
        "— a directory listing — not a remote path. This is backup.sh's real, correctly "
        "prefixed retention listing being reported as an unprefixed bucket root."
    )
    assert not _unsanctioned_root_definitions('RCLONE_REMOTE="${RCLONE_REMOTE:-isnad}"'), (
        "The `:-` DEFAULT operator was read as a remote-spec `:`, so the remote's own NAME "
        "was reported as an unprefixed bucket root. b2_preflight.sh:63 is exactly this line."
    )


def test_the_allowlisted_probe_cannot_grow_a_bucket_path() -> None:
    """An allowlist entry with a reason is fine; a blanket pass is not.

    `rclone lsd "${remote}:"` is exempt because it enumerates BUCKETS at the account level.
    The exemption is for that SHAPE, not for the verb: the moment the call addresses a path
    inside the bucket, it is an offender again.
    """
    assert not _offending_rclone_calls(
        'rclone lsd "${remote}:" > "$lsd_out" 2>&1', "b2_preflight.sh"
    ), (
        "The bucket-root capability probe must stay exempt — it is how a wrongly-scoped "
        "key is detected at all."
    )

    assert _offending_rclone_calls(
        'rclone lsd "${remote}:${B2_BUCKET}/" > "$lsd_out" 2>&1', "b2_preflight.sh"
    ), (
        "The allowlisted verb grew a BUCKET PATH and the guard still passed it. The "
        "exemption is for the bare-remote probe shape, not a blanket pass for `lsd`."
    )

    assert _offending_rclone_calls(
        'rclone purge "${remote}:${B2_BUCKET}/" 2>&1', "b2_preflight.sh"
    ), "The exemption leaked to a verb it was never granted to."

    assert _offending_rclone_calls('rclone lsd "${remote}:" > "$lsd_out" 2>&1', "backup.sh"), (
        "The exemption leaked to a FILE it was never granted to."
    )


def test_every_sanctioned_exception_carries_a_written_reason() -> None:
    """A bare entry in an allowlist is an unexplained hole. Each must say why."""
    for key, reason in _SANCTIONED_EXCEPTIONS.items():
        assert reason and len(reason) > 80, (
            f"Sanctioned exception {key} has no substantive written reason. An allowlist "
            f"entry without a reason is a blanket pass with extra steps."
        )


# ---------------------------------------------------------------------------
# The property.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", BACKUP_STORE_SCRIPTS, ids=lambda p: p.name)
def test_every_rclone_call_is_rooted_in_its_environment(script: Path) -> None:
    """USAGE — half 1.

    `b2_preflight.sh` is in this list for the first time (deploy#638). It was entirely
    unguarded: the guard scanned only backup.sh and restore.sh, and the preflight's canary
    write+delete sat at the BUCKET ROOT, outside every prefix, on every backup.
    """
    offenders = _offending_rclone_calls(script.read_text(), script.name)
    assert not offenders, (
        f"{script.name} makes an rclone call that does not address the bucket through a "
        f"sanctioned prefixed root. stg and prod share one bucket, so an unprefixed path "
        f"reaches into the OTHER environment's backups — and in prune_old_backups it "
        f"DELETES them (deploy#632):\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("script", BACKUP_STORE_SCRIPTS, ids=lambda p: p.name)
def test_every_bucket_root_definition_names_its_environment(script: Path) -> None:
    """DEFINITION — half 2. The half that makes half 1 sound.

    A root variable is discovered from its DEFINITION (any assignment containing a
    remote-spec), not from a list of names this guard happens to know. So `PURGE_ROOT`, or
    any other new root someone invents, is caught the moment it is defined — which is the
    whole of deploy#637.
    """
    offenders = _unsanctioned_root_definitions(script.read_text())
    assert not offenders, (
        f"{script.name} defines a bucket root that does not name the environment. Every "
        f"rclone call rooted in it then reaches the shared bucket root, invisibly to any "
        f"scan that hunts for `${{B2_BUCKET}}/` in the CALL (deploy#637):\n  "
        + "\n  ".join(offenders)
    )


def test_remote_subdir_is_built_from_the_prefix() -> None:
    """`${B2_BUCKET}/${REMOTE_SUBDIR}` is accepted as sanctioned — so pin what REMOTE_SUBDIR IS.

    Without this, `REMOTE_SUBDIR` is a name the guard trusts on faith, and redefining it
    without the prefix would silently unscope the upload while the guard stayed green. That
    is the `feedback_a_scan_cannot_see_an_emptied_string` failure in a different costume:
    the sanctioned vocabulary is only as sound as the definitions behind it.
    """
    body = BACKUP_SH.read_text()
    defs = re.findall(r'^\s*REMOTE_SUBDIR="([^"]*)"', body, re.MULTILINE)
    assert defs, "backup.sh never defines REMOTE_SUBDIR, but the guard sanctions it as a root."
    assert len(defs) == 1, f"REMOTE_SUBDIR is assigned more than once: {defs}"
    assert defs[0].startswith("${BACKUP_PREFIX}/"), (
        f'REMOTE_SUBDIR="{defs[0]}" does not START with ${{BACKUP_PREFIX}}/. The upload path '
        f"is built from it, so it would land outside the environment namespace — in the "
        f"shared bucket root that prod's purge walks."
    )


def test_the_pipeline_store_is_a_different_remote() -> None:
    """The scan's SCOPE is a claim, so verify it rather than assuming it.

    deploy-data-load.yml and graph-prune-narrators.yml call rclone and even assign a local
    `B2_BUCKET` — but they address the read-only Parquet PIPELINE bucket on the `pipeline:`
    remote, which has no environment namespace and needs none. They are out of scope for the
    backup-store guard.

    That exemption is only sound while they really are a different store. If either is ever
    pointed at `isnad:` — the backup remote — this test goes RED and they must come into
    scope. Scope-by-assumption is how a guard goes quietly blind.
    """
    for wf in (DATA_LOAD, GRAPH_PRUNE):
        body = wf.read_text()
        assert 'B2_BASE="pipeline:${B2_BUCKET}/${PARQUET_REF}"' in body, (
            f"{wf.name} no longer addresses the pipeline bucket the way this guard's scope "
            f"assumes. Re-derive the scope before trusting it."
        )
        assert not re.search(r"(?<!_)isnad:", body), (
            f"{wf.name} now addresses the `isnad:` BACKUP remote. It is no longer a "
            f"pipeline-only file, so it must be added to BACKUP_STORE_SCRIPTS and obey the "
            f"environment-prefix rule."
        )


def test_the_artifact_scanner_does_not_build_its_own_bucket_address() -> None:
    """verify_b2_backup_artifact.sh takes its root as INPUT — it must never mint one.

    Every rclone call in that script is rooted in `B2_ROOT`, which the workflow supplies as
    `isnad:${B2_BUCKET}/${{ matrix.env }}` (pinned by the watchdog test below). That
    indirection is safe ONLY because the script never constructs a bucket path itself. The
    day it does, its root stops being the caller's problem and the workflow's prefix pin
    stops protecting it.
    """
    body = VERIFY_ARTIFACT_SH.read_text()
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "B2_BUCKET" not in code, (
        "verify_b2_backup_artifact.sh now names B2_BUCKET in code. It is supposed to receive "
        "an already-prefixed B2_ROOT from its caller, not build a bucket address itself."
    )
    assert not re.search(r"(?<!_)isnad:", code), (
        "verify_b2_backup_artifact.sh now hardcodes the `isnad:` remote. Same problem: its "
        "root must come from B2_ROOT, whose only definition site carries the environment."
    )
    assert re.search(r':\s*"\$\{B2_ROOT:\?', body), (
        "verify_b2_backup_artifact.sh does not hard-require B2_ROOT. An unset root would "
        "make every rclone call address a bare relative path."
    )


@pytest.mark.parametrize("script", BACKUP_STORE_SCRIPTS, ids=lambda p: p.name)
def test_script_refuses_to_run_without_a_prefix(script: Path) -> None:
    """Fail CLOSED. No default is tolerable: `""` silently restores the shared prefix and
    `prod` would be catastrophic on stg. An un-deployed host must fail loudly."""
    body = script.read_text()
    assert re.search(r':\s*"\$\{BACKUP_PREFIX:\?', body), (
        f"{script.name} does not hard-require BACKUP_PREFIX. A default reintroduces the "
        f"collision silently — which is the whole failure mode."
    )
    assert not re.search(r"BACKUP_PREFIX:-", body), (
        f"{script.name} supplies a DEFAULT for BACKUP_PREFIX. There is no safe default."
    )


_REFUSAL = "not a bare name"


def _run_with_prefix(
    script: Path, prefix: str, backup_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the real script with a given BACKUP_PREFIX and nothing else it can succeed at.

    BACKUP_DIR is pointed at a writable tmp dir so that the un-writable
    `/var/lib/noorinalabs-backups` mkdir is not a CONFOUND — it is the ambient failure that
    made the original assertion a tautology, and a test whose signal has to be untangled
    from an unrelated crash is one nobody will trust at 3am (#633 review, Nino Kavtaradze).
    PATH stays minimal so no real rclone/docker call can leave the sandbox.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "B2_KEY_ID": "x",
        "B2_APP_KEY": "x",
        "B2_BUCKET": "x",
        "BACKUP_PREFIX": prefix,
    }
    if backup_dir is not None:
        env["BACKUP_DIR"] = str(backup_dir)
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("script", BACKUP_STORE_SCRIPTS, ids=lambda p: p.name)
def test_a_prefix_that_escapes_its_namespace_is_refused_AS_SUCH(
    script: Path, tmp_path: Path
) -> None:
    """A prefix carrying `/` or `..` climbs out of its own namespace into the other's.

    ATTRIBUTE THE REFUSAL. The first cut asserted only `returncode != 0` and claimed that
    "asserts the refusal, not the wording". It asserted neither — it was a TAUTOLOGY. These
    scripts exit non-zero in this sandbox no matter what, for reasons having nothing to do
    with the prefix, and there is no environment where they would not: `backup.sh` exits 0
    only on a working VPS with docker, rclone and live B2, which is never where a test runs.

    Lucas Ferreira proved it by DELETING the entire hostile-prefix guard from a scratch copy
    so that `BACKUP_PREFIX=../prod` was ACCEPTED — and the suite still reported `9 passed`.
    Side by side, same hostile prefix:

        guard present:  FATAL: BACKUP_PREFIX='../prod' is not a bare name ...   rc=1
        guard DELETED:  mkdir: cannot create '/var/lib/noorinalabs-backups' ... rc=1

    `returncode != 0` cannot separate those. So bind to the message the script itself owns,
    and pair it with a positive control — without the control, "the refusal message is
    absent" would be satisfied by any ambient failure and would prove nothing either.

    This is `restore_rehearsal.sh`'s own standard (it asserts non-zero AND that the failure
    was for the expected reason), and `restore.sh` is parametrized in because its identical
    guard had NO test executing it at all — on the restore side, an escaping prefix is how
    you load the stg graph into prod.
    """
    for hostile in (
        "../prod",
        "stg/../prod",
        "/prod",
        "PROD",
        "-prod",  # leading `-`: option injection into rclone, were it ever argv[0]
        "--config=/tmp/evil",
        "",
        "stg\n../prod",  # newline smuggling — bash =~ has no REG_NEWLINE, so `$` is end-of-STRING
        "stg; rm -rf /",
        "$(touch /tmp/pwned)",
        "рrod",  # Cyrillic homoglyph
    ):
        proc = _run_with_prefix(script, hostile, backup_dir=tmp_path)
        assert proc.returncode != 0, (
            f"{script.name} ACCEPTED BACKUP_PREFIX={hostile!r} — that prefix escapes its "
            f"namespace and can reach the other environment's backups."
        )
        # An empty prefix dies at the `:?` guard, which has its own wording; every other
        # hostile value must be refused BY THE CHARSET CHECK, by name.
        expected = "must be set" if hostile == "" else _REFUSAL
        combined = proc.stderr + proc.stdout
        assert expected in combined, (
            f"{script.name} exited {proc.returncode} on BACKUP_PREFIX={hostile!r}, but NOT "
            f"because it refused the prefix — so this assertion is being satisfied by an "
            f"unrelated ambient failure and proves nothing. Output: {combined[-300:]!r}"
        )


@pytest.mark.parametrize("script", BACKUP_STORE_SCRIPTS, ids=lambda p: p.name)
def test_a_legal_prefix_is_not_refused(script: Path, tmp_path: Path) -> None:
    """The positive control for the test above.

    Without it, "the refusal message appeared" could be true of EVERY input — including the
    legal ones — and the guard would be rejecting valid deploys while the suite stayed green.
    A one-way oracle proves nothing (deploy#574).

    The script will still fail here (no docker, no real B2) — that is fine and expected. What
    must NOT appear is the charset refusal.
    """
    proc = _run_with_prefix(script, "stg", backup_dir=tmp_path)
    combined = proc.stderr + proc.stdout
    assert _REFUSAL not in combined, (
        f"{script.name} REFUSED the legal prefix 'stg' as malformed. The charset guard is "
        f"rejecting valid input, so the refusal assertions above are vacuous. "
        f"Output: {combined[-300:]!r}"
    )


def test_remote_root_is_actually_built_from_remote_bucket_and_prefix() -> None:
    """REMOTE_ROOT must resolve to `<remote>:<bucket>/<prefix>` — and nothing else.

    This test exists because the guard above went GREEN on a genuinely broken restore.sh.
    Writing this change, a bulk rewrite of `"${RCLONE_REMOTE}:${B2_BUCKET}/` -> `"${REMOTE_ROOT}/`
    also rewrote REMOTE_ROOT'S OWN DEFINITION, leaving:

        REMOTE_ROOT="${REMOTE_ROOT}/${BACKUP_PREFIX}"     # -> "/stg"

    Every "remote" path then pointed at the LOCAL FILESYSTEM. `test_every_remote_path_names_
    its_environment` still passed — the file contained no unprefixed `${B2_BUCKET}/` path,
    because it contained no bucket path at all. A source-text scan cannot see that the string
    it is scanning has been emptied of meaning; only shellcheck noticed, via SC2034
    ("RCLONE_REMOTE appears unused"), which is a different instrument asking a different
    question.

    The proxy is not the property. Pin the property.
    """
    body = RESTORE_SH.read_text()
    m = re.search(r'^\s*REMOTE_ROOT="([^"]*)"\s*$', body, re.MULTILINE | re.DOTALL)
    defs = re.findall(r'^\s*REMOTE_ROOT="([^"]*)"', body, re.MULTILINE)
    real = [d for d in defs if d != ""]  # the "" initialiser is fine; the real one is not
    assert real, "restore.sh never assigns REMOTE_ROOT a value — every remote path is empty"
    assert len(real) == 1, f"REMOTE_ROOT is assigned more than once: {real}"
    definition = real[0]

    assert "REMOTE_ROOT" not in definition, (
        f'REMOTE_ROOT is defined in terms of ITSELF: REMOTE_ROOT="{definition}". It resolves '
        f"to a local path, so every rclone call silently addresses the local filesystem "
        f"instead of B2."
    )
    for required in ("${RCLONE_REMOTE}", "${B2_BUCKET}", "${BACKUP_PREFIX}"):
        assert required in definition, (
            f'REMOTE_ROOT="{definition}" does not contain {required}. It must resolve to '
            f"<remote>:<bucket>/<prefix> — drop any one and it addresses the wrong place."
        )
    assert m is not None


def test_the_two_environments_get_different_prefixes() -> None:
    """The copy-paste guard.

    Both workflows injecting `BACKUP_PREFIX=stg` would pass every other test in this file
    and restore the collision exactly.
    """
    stg = re.search(r"^\s*BACKUP_PREFIX=(\S+)", DEPLOY_STG.read_text(), re.MULTILINE)
    prod = re.search(r"^\s*BACKUP_PREFIX=(\S+)", DEPLOY_PROD.read_text(), re.MULTILINE)
    assert stg, "deploy-stg.yml does not inject BACKUP_PREFIX — stg backups will fail closed"
    assert prod, "deploy-prod.yml does not inject BACKUP_PREFIX — prod backups will fail closed"
    assert stg.group(1) != prod.group(1), (
        f"stg and prod both deploy BACKUP_PREFIX={stg.group(1)!r} — they would share a "
        f"prefix and purge each other, which is the entire bug."
    )


def test_the_b2_artifact_watchdog_scans_only_its_own_environment() -> None:
    """The false-green that was about to arm itself.

    verify-backup-artifact.yml runs a `[stg, prod]` matrix but scanned the bucket ROOT, so
    both legs read the SAME objects. With stg's first-ever backup now in the bucket, the
    PROD leg would report `fresh` on the strength of STAGING'S artifact — on the one check
    that exists because every other backup signal can lie.
    """
    body = VERIFY_ARTIFACT.read_text()
    assert 'B2_ROOT="isnad:${B2_BUCKET}"' not in body, (
        "verify-backup-artifact.yml scans the bucket ROOT. Both matrix legs then read the "
        "same objects and the prod leg reports on staging's backup (deploy#632)."
    )
    assert re.search(r'B2_ROOT="isnad:\$\{B2_BUCKET\}/\$\{\{ matrix\.env \}\}"', body), (
        "verify-backup-artifact.yml must scope its scan to ${{ matrix.env }} — the same "
        "name backup.sh writes its objects under."
    )
