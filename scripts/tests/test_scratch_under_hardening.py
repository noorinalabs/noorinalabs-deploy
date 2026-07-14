"""Scratch allocation must survive the backup unit's hardening — and never lie when it can't.

THE BLIND SPOT THIS MODULE EXISTS TO CLOSE
------------------------------------------
``systemd/isnad-backup.service`` runs ``ProtectSystem=strict`` with ``PrivateTmp``
deliberately unset (deploy#121 Bug A) and grants only ``BACKUP_DIR``,
``/opt/noorinalabs-deploy`` and ``/var/lib/node_exporter``. **``/tmp`` is READ-ONLY there.**

A bare ``mktemp`` defaults to ``/tmp``. So a script that captures stderr into a temp file
cannot run under the unit at all — and, worse, the *way* it fails is silent: the allocation
returns empty, the ``2>"$err"`` redirect dies, and the caller reports the resulting non-zero
as a fact about whatever it was checking.

This has now shipped **twice**:

* **deploy#613** — ``b2_preflight.sh`` reported ``verdict=KEY_INVALID``. The B2 key was
  perfectly good. Every backup this project ever attempted died there, blaming a credential.
* **deploy#623** — ``compose_project.sh`` reported ``Cannot reach Docker Compose — is the
  daemon running?``. The daemon was running. This shipped *in the fix for deploy#617*, past
  23 CI checks and four reviewers.

Both are the same bug: **a check that could not run, reporting its own breakage as a fact
about the system it was supposed to be checking**, and pointing the operator at the wrong
subsystem.

WHY THE REST OF THE SUITE CANNOT CATCH IT
-----------------------------------------
GitHub Actions runners have a writable ``/tmp``. So do the rehearsal containers. So does the
dev box. **The entire test surface — including the e2e tests that drive the real
``backup.sh`` — runs in an environment where this bug is unreachable.** Only the on-host
systemd path is affected, and nothing in CI executes that path. The tests were never weak;
they were *structurally incapable* of failing, which is why a green suite meant nothing here
twice over (cf. ``feedback_silent_zero_is_not_a_measurement``,
``feedback_fixture_makes_guard_assertion_inert``).

So this module does the one thing the others cannot: it makes the scratch parent
**unusable** and asserts on what the script then says.

The unusable parent is ``/proc/self`` — a real directory that exists and refuses ``creat()``
**for uid 0 as well**. ``chmod`` would be a no-op against root, so a root CI runner would
quietly turn this whole class green; procfs does not care who you are. Same reasoning as the
``ENOTDIR`` fixture in ``test_b2_preflight.py``.

WHAT A GREEN RUN MEANS — AND WHAT IT DOES NOT
---------------------------------------------
Say it in the narrow words, because the wide words are how deploy#613 shipped inside the fix
for deploy#617, past a green suite: **a green run of this module means the scripts behave
honestly when a scratch allocation fails, and no ``mktemp`` in them defaults to /tmp.** It
does **not** mean the backup path is safe under hardening.

The property that actually matters is *"writes nothing outside ``ReadWritePaths=``"*, and that
is a runtime property of a process under a namespace. It is not decidable by reading source
text, and ``mktemp`` is merely where it has bitten us twice. ``> /tmp/foo``, a ``cd /tmp``, a
config read under ``ProtectHome=yes``, a ``python -c`` calling ``tempfile.mkstemp()`` — each
fails identically under the unit and none of them is a ``mktemp`` line. (``rclone`` escapes
``ProtectHome=yes`` today only because ``backup.sh`` configures it entirely through
``RCLONE_CONFIG_ISNAD_*`` env vars rather than a config file. Nothing tests that, and it is one
refactor away from being the next defect. — Nino Kavtaradze, #624 review.)

The real gate is **deploy#626**: run the entry points under the hardening *read out of the unit
file itself*, so the test cannot disagree with production about what production permits. A
hand-written "read-only /tmp" job would just be a third place to encode the same assumption,
stale the moment someone edits ``ReadWritePaths=``. This module is the cheap, deterministic
stopgap that closes the known proxy and buys the time to build that. It is not the answer.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path, PurePosixPath

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
SYSTEMD_DIR = REPO_ROOT / "systemd"
LIB = SCRIPTS_DIR / "compose_project.sh"
SCRATCH_LIB = SCRIPTS_DIR / "scratch.sh"


# EVERY Exec directive, not just ExecStart=. (Nurul Hakim's finding on the #654 harness, and
# it was live in this file too — see test_the_pin_sees_every_exec_directive.)
#
# The first cut matched `line.startswith("ExecStart=")`. That silently excludes `ExecStartPre=`,
# `ExecStartPost=`, `ExecStop=`, `ExecStopPost=`, `ExecCondition=` and `ExecReload=` — and
# systemd runs EVERY ONE OF THEM INSIDE THE SAME SANDBOX. A bare `mktemp` in an `ExecStartPre=`
# helper is deploy#613, under `ProtectSystem=strict`, and the pin had nothing to say about it.
#
# What makes this worth a paragraph rather than a one-line fix: MY OWN CALIBRATION MISSED IT.
# The mutation I used to "prove" the unit rule worked injected an `ExecStartPost=` script that
# ALSO sourced scratch.sh — so the ADOPTER rule flagged it, the suite went red, and I read that
# red as evidence for the unit rule. A guard can pass for the wrong reason, and a calibration
# that does not isolate the rule it is calibrating cannot tell the difference
# (cf. `feedback_measurement_is_the_thing_that_breaks`).
_EXEC_DIRECTIVE = re.compile(r"^Exec[A-Za-z]*\s*=\s*(.*)$")


def unit_reachable_scripts(
    systemd_dir: Path = SYSTEMD_DIR, scripts_dir: Path = SCRIPTS_DIR
) -> set[Path]:
    """Every script a HARDENED systemd unit can execute, resolved from the units themselves.

    Every `Exec*=` directive gives the entry points — a unit may legally carry SEVERAL, and all
    of them run under the unit's sandbox — and the transitive `source`/`.` graph gives the rest.
    A hand-typed list would drift the moment someone adds a unit or sources a new helper, and
    drift in THIS list means a script runs under `ProtectSystem=strict` with nobody checking
    where its scratch lands, which is the whole of deploy#613/#623.

    Scoped to units that are actually hardened: an unhardened unit has a writable /tmp and a
    bare `mktemp` in it is not a bug.

    `systemd_dir`/`scripts_dir` default to the real repo layout. They are parameters ONLY so a
    test can point the resolver at a FIXTURE TREE under `tmp_path` — the pin must never mutate
    the deployable `systemd/` unit files to exercise itself (deploy#674).
    """
    entry: set[Path] = set()
    for unit in sorted(systemd_dir.glob("*.service")):
        if "ProtectSystem=strict" not in unit.read_text():
            continue
        entry |= _entry_points_of_unit(unit, scripts_dir)

    # Transitive closure over the `source` graph — but matched on BASENAME ANYWHERE in the
    # script, not on the `source` line itself.
    #
    # The obvious implementation (look only at lines starting with `source`/`.`) finds
    # NOTHING here, and I know because I wrote it and this module's own closure assertion
    # caught it. backup.sh sources indirectly:
    #
    #     COMPOSE_PROJECT_LIB="$(dirname "${BASH_SOURCE[0]}")/compose_project.sh"
    #     source "$COMPOSE_PROJECT_LIB"
    #
    # The filename is on the ASSIGNMENT line; the `source` line carries only a variable. A
    # resolver that evaluated the path expression would be guessing at runtime semantics.
    #
    # So this DELIBERATELY OVER-APPROXIMATES: any sibling *.sh whose basename is mentioned
    # anywhere in a unit-reachable script is treated as reachable. Over-inclusion only WIDENS
    # the set of files scanned, so THE RESOLVER can produce a false red (someone merely names
    # a script in a comment-free string) but not a false green. For a guard against a bug that
    # has shipped twice, that is the correct direction to be wrong in.
    #
    # Read that claim narrowly: it is a property of the RESOLVER, not of the module. Scanning
    # the right files says nothing about whether the pattern applied to them is sound, and it
    # says nothing about the writes that are not `mktemp` calls at all. See the module
    # docstring, "WHAT A GREEN RUN MEANS".
    return _close_over_source_graph(entry, scripts_dir)


def _entry_points_of_unit(unit: Path, scripts_dir: Path = SCRIPTS_DIR) -> set[Path]:
    """Every script THIS unit can execute, across ALL of its `Exec*=` directives.

    A `Type=oneshot` unit legally carries SEVERAL `ExecStart=` lines and runs them in sequence,
    so this collects every line of every Exec directive — not the first, and not the last.
    (Nurul Hakim's #654 finding: a resolver that took only the LAST `ExecStart=` silently never
    ran a FIRST one that carried deploy#613's own defect.)
    """
    entry: set[Path] = set()
    for line in unit.read_text().splitlines():
        m = _EXEC_DIRECTIVE.match(line.strip())
        if not m:
            continue
        # Every token, so a `/bin/sh -c /opt/…/helper.sh` form is resolved too, and the
        # `-@+!:` exec prefixes are stripped rather than hiding the path behind them.
        for token in m.group(1).split():
            name = Path(token.lstrip("-@+!:")).name
            candidate = scripts_dir / name
            if candidate.is_file() and candidate.suffix == ".sh":
                entry.add(candidate)
    return entry


def _closure_of_unit(unit: Path, scripts_dir: Path = SCRIPTS_DIR) -> set[Path]:
    """Everything THIS unit can execute, plus everything those scripts source."""
    return _close_over_source_graph(_entry_points_of_unit(unit, scripts_dir), scripts_dir)


def _close_over_source_graph(entry: set[Path], scripts_dir: Path = SCRIPTS_DIR) -> set[Path]:
    """Transitive closure of `entry` over the `source` graph, matched on BASENAME."""
    seen: set[Path] = set()
    pending = list(entry)
    siblings = sorted(scripts_dir.glob("*.sh"))
    while pending:
        script = pending.pop()
        if script in seen:
            continue
        seen.add(script)
        body = "\n".join(
            ln for ln in script.read_text().splitlines() if not ln.lstrip().startswith("#")
        )
        for sibling in siblings:
            if sibling != script and sibling.name in body and sibling not in seen:
                pending.append(sibling)
    return seen


def scratch_guarded_scripts() -> set[Path]:
    """Every script the no-bare-mktemp rule binds — and it is WIDER than the hardened units.

    THE UNIT-REACHABLE CLOSURE IS NECESSARY AND NOT SUFFICIENT. (deploy#625/#628)

    `restore.sh` and `verify_b2_backup_artifact.sh` are not `ExecStart=`ed by any unit, so the
    resolver above cannot see them — and both were allocating with a bare `mktemp`. They are
    safe today BY ACCIDENT, NOT BY CONSTRUCTION: restore is operator-run and the verify script
    runs on GitHub Actions, and both of those happen to hand it a writable /tmp. Nothing
    enforced it. deploy#640 is putting restore under a workflow right now, and the accident is
    one runner-image change or one `ProtectSystem=` away from ending.

    So the guarded set is:

        (a) anything a HARDENED unit can execute        — the sandbox is real TODAY
        (b) anything that SOURCES THE SCRATCH LIBRARY   — it has opted into the discipline

    Both halves are RESOLVED, never hand-typed. A hand-typed list is a third place to encode
    the same assumption, and it drifts the moment somebody adds a unit or a caller — which is
    exactly how a guard rots into a coincidence detector.

    (b) is the load-bearing half for the direction this repo is moving in: a bare `mktemp`
    sitting three lines from a `scratch_file` call is the regression this PR exists to make
    impossible, and it is the shape the next person will reach for.
    """
    guarded = unit_reachable_scripts()

    # Anyone who names the scratch library has adopted it — and is therefore bound by it.
    adopters: set[Path] = set()
    for script in sorted(SCRIPTS_DIR.glob("*.sh")):
        body = "\n".join(
            ln for ln in script.read_text().splitlines() if not ln.lstrip().startswith("#")
        )
        if SCRATCH_LIB.name in body or "scratch_file" in body or "scratch_dir" in body:
            adopters.add(script)

    return guarded | _close_over_source_graph(adopters)


# The ANCHOR IS THE INVOCATION, NOT THE LINE. (deploy#624 review — Lucas Ferreira)
#
# The first cut asked `"-p " not in ln`, i.e. it judged the whole LINE. Any unrelated `-p `
# elsewhere on the line — a `grep -p`, a comment fragment, a second command — vouched for a
# bare `mktemp` sitting next to it. Lucas mutated a bare `mktemp` back in on such a line and
# the pin stayed GREEN. A guard whose anchor is bigger than the thing it guards is not a
# guard; it is a coincidence detector (cf. feedback_lint_gate_cover_all_syntactic_forms).
#
# So: find each `mktemp` occurrence and read only ITS OWN arguments, up to the next shell
# separator.
_MKTEMP_CALL = re.compile(r"\bmktemp\b([^|;&)`\n]*)")

# ONE rule, no exemptions: in a hardened script, every `mktemp` NAMES ITS PARENT.
#
# The first cut accepted a template carrying a `$` or a `/` as proof the call had chosen its
# own directory. It is not proof, and Nino Kavtaradze broke it three ways (#624 review):
#
#   mktemp -t "${X}.XXXXXX"    `-t` puts it in $TMPDIR-or-/tmp. The `${` vouched for it.
#   mktemp --tmpdir            No `=DIR` means $TMPDIR-or-/tmp. The prefix-match vouched for it.
#   MUT="$(mktemp)"  # ${BACKUP_DIR}   ...and the bare call passes if a `$` shares the line.
#
# The third is the one that matters: this PR's own convention is to write `${BACKUP_DIR}`, so
# the codebase actively selects for the line that disarms the guard. An escape hatch inside a
# guard against a bug that has now shipped twice is not a convenience, it is the hole.
#
# So the hatch is REMOVED, not narrowed. A call is exempt only if it says where the file goes:
# `-p <val>` or `--tmpdir=<val>`. Bare `--tmpdir` and `-t` both resolve to $TMPDIR-or-/tmp, so
# neither is an exemption; `${` is never one. The two legitimate sites in the closure
# (backup.sh, emit-backup-failure-marker.sh) were converted to `-p` to meet it — same landing
# directory, now checkable. Cost: `mktemp "$dir/x.XXXXXX"` is a false RED. That is the correct
# direction to be wrong in, and it is one flag to fix.
#
# THE OPTION MUST BE AN OPTION, NOT A SUBSTRING. (#624 review 3 — Lucas Ferreira)
#
# The first cut of this regex was `-p\s*["$\w/]`, unanchored — so a `-p` ANYWHERE in the
# argument text exempted the call, INCLUDING inside a template NAME. The library's own scratch
# template is `compose-project-XXXXXX`. It contains `-p`. So:
#
#     err2="$(mktemp -t compose-project-XXXXXX)"     <-- allocates in the READ-ONLY /tmp
#
# passed GREEN, and that is deploy#613 verbatim, in the file the unit executes. `mktemp
# --tmpdir backup-pg.XXXXXX` evades identically (`-pg`). Worse, the calibration fixtures I
# wrote to PROVE this filter worked used template names — `probe.XXXXXX`, `${PREFIX}.XXXXXX` —
# that happen to contain no `-p`, so the very test meant to catch this could not see it. A
# guard and its calibration sharing the same blind spot is not a guard
# (cf. `feedback_fixture_makes_guard_assertion_inert`).
#
# So the option is anchored: it must START a token (preceded by whitespace or the string
# start). `-\w*p` admits clustered short flags (`-dp DIR`) while still requiring the `p` to
# live in a token that begins with `-`. The fixtures below now carry `-p` INSIDE the template
# name in both classes, so this cannot silently reopen.
_HAS_PARENT = re.compile(r'(?:\s|^)(?:-\w*p\s*["$\w/]|--tmpdir=)')


def _mktemp_invocations(line: str) -> list[str]:
    """The argument text of every `mktemp` call on this line, each judged on its own."""
    return [m.group(1) for m in _MKTEMP_CALL.finditer(line)]


def _names_its_parent(args: str) -> bool:
    """Did THIS mktemp call say where its file lands, rather than defaulting to /tmp?"""
    return _HAS_PARENT.search(args) is not None


# A directory that EXISTS and into which no file can be created — for root too.
UNWRITABLE_PARENT = "/proc/self"

# Words that constitute a claim about a subsystem the check never actually reached. If a
# scratch failure produces any of these, it is doing the deploy#613/#623 thing: converting
# "I could not run" into "the thing you asked me about is broken".
FALSE_CLAIMS = (
    "is the daemon running",
    "Cannot reach Docker Compose",
    "is not running:",
    "Could not START neo4j",
)

_DRIVER = """
set -euo pipefail
log() {{ printf '%s %s\\n' "$1" "$2"; }}
COMPOSE_FILE="compose/docker-compose.prod.yml"
source "{lib}"
{call}
"""


def _run(
    call: str, *, backup_dir: str, with_docker: bool, tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """Drive the library with BACKUP_DIR pointed at `backup_dir`."""
    env = dict(os.environ)
    env["BACKUP_DIR"] = backup_dir
    env["COMPOSE_PROJECT"] = "noorinalabs"
    # TMPDIR must not rescue the fallback chain: under the real unit /tmp is read-only and
    # TMPDIR is unset, so the ONLY writable path is BACKUP_DIR. A test that let TMPDIR stand
    # in would silently restore the very escape hatch production does not have.
    env["TMPDIR"] = UNWRITABLE_PARENT
    env.pop("COMPOSE_PROJECT_NAME", None)

    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    if with_docker:
        # A perfectly healthy docker. If the script still blames the daemon, that accusation
        # is coming from the scratch failure, not from docker — which is the entire point.
        (stub / "docker").write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$*\" == *'{{.Service}}:{{.State}}'* ]]; then\n"
            "  printf 'postgres:running\\nuser-postgres:running\\nneo4j:running\\n'\n"
            "fi\n"
            "exit 0\n"
        )
        (stub / "docker").chmod(0o755)
        env["PATH"] = f"{stub}:{env['PATH']}"

    script = _DRIVER.format(lib=LIB, call=call)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=SCRIPTS_DIR.parent,
        timeout=60,
    )


# --------------------------------------------------------------------------
# Calibration: the fixture must be able to express the defect
# --------------------------------------------------------------------------


def test_the_unwritable_parent_really_is_unwritable_even_for_root() -> None:
    """The instrument, before the reading.

    If `/proc/self` were writable in this environment, every assertion below would pass
    vacuously against ANY code, fixed or broken. A root runner defeats `chmod`; it does not
    defeat procfs. Prove that here, or the rest of this file is theatre.
    """
    probe = subprocess.run(
        ["bash", "-c", f'mktemp -p "{UNWRITABLE_PARENT}" probe-XXXXXX'],
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0, (
        f"{UNWRITABLE_PARENT} accepted a file — this fixture cannot express the deploy#623 "
        f"defect, so every test in this module is inert. Pick a parent that refuses creat() "
        f"for uid 0 as well (chmod does NOT, which is why it is not used here)."
    )


def test_the_library_works_when_the_scratch_parent_is_writable(tmp_path: Path) -> None:
    """Positive control: the guard is not simply 'the function that always fails'.

    Without this, a `scratch_file` that returned 1 unconditionally would satisfy every
    negative assertion below and look like a perfect fix.
    """
    good = tmp_path / "backups"
    good.mkdir()
    proc = _run(
        "running_services > /dev/null && echo REACHED_DOCKER",
        backup_dir=str(good),
        with_docker=True,
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0, (
        f"the library failed with a WRITABLE scratch parent:\n{proc.stderr}"
    )
    assert "REACHED_DOCKER" in proc.stdout


# --------------------------------------------------------------------------
# The defect: a check that could not run must not speak about the system
# --------------------------------------------------------------------------


def test_running_services_does_not_blame_docker_when_scratch_is_unwritable(tmp_path: Path) -> None:
    """deploy#623, exactly. Docker is HEALTHY; only the scratch write fails.

    Before the fix this printed "Cannot reach Docker Compose (rc=1) — is the daemon running?"
    and sent the operator to inspect a daemon that was fine, while the real cause — a
    read-only /tmp under ProtectSystem=strict — went unmentioned.
    """
    proc = _run(
        "running_services || true",
        backup_dir=UNWRITABLE_PARENT,
        with_docker=True,
        tmp_path=tmp_path,
    )
    combined = proc.stdout + proc.stderr

    for claim in FALSE_CLAIMS:
        assert claim not in combined, (
            f"a failed SCRATCH WRITE was reported as {claim!r} — docker is healthy in this "
            f"fixture and was never even reached. This is deploy#613/#623: a check that could "
            f"not run, testifying about the system it never checked.\n\n{combined}"
        )

    assert "could not RUN" in combined, (
        "the diagnostic must say the check could not run, in its own words"
    )
    assert "NO evidence" in combined, (
        "it must explicitly disown any conclusion about docker/the project/the stack — "
        "otherwise the next operator debugs the daemon, as they would have on stg"
    )
    assert "ProtectSystem=strict" in combined and "BACKUP_DIR" in combined, (
        "the diagnostic must name the actual cause and the actual remedy"
    )


def test_neo4j_start_does_not_claim_the_graph_failed_when_scratch_is_unwritable(
    tmp_path: Path,
) -> None:
    """The same trap, in the more dangerous place.

    `neo4j_start` runs in the window where backup.sh has ALREADY STOPPED the graph. A scratch
    failure rendered as "Could not START neo4j" would tell the operator the database is
    broken — when in truth it was never touched, and the only thing that failed was a temp
    file.
    """
    proc = _run(
        "neo4j_start || true",
        backup_dir=UNWRITABLE_PARENT,
        with_docker=True,
        tmp_path=tmp_path,
    )
    combined = proc.stdout + proc.stderr

    assert "Could not START neo4j" not in combined, (
        "a failed scratch write was reported as a failure to start Neo4j. The graph was never "
        "touched. Telling an operator their database would not start — in the window where "
        f"the backup has just stopped it — is the worst possible false claim here.\n\n{combined}"
    )
    assert "could not RUN" in combined and "NO evidence" in combined


def test_the_offender_filter_itself_separates_tmp_from_not_tmp() -> None:
    """Calibrate the guard before trusting the guard.

    `test_no_bare_mktemp_survives_in_the_library` reports "0 offenders" over the closure. That
    number is worth exactly what the filter behind it is worth — and the previous filter was
    worth nothing against three spellings that all land in /tmp (Nino Kavtaradze, #624 review).
    An oracle that only ever says CLEAN proves every line clean
    (cf. `feedback_calibrate_the_mutation_before_counting_it`).

    So: run the filter over both classes and require it to SEPARATE them. Every LANDS_IN_TMP
    line must be flagged, every NAMES_ITS_PARENT line must not. Anyone who loosens the pattern
    to wave a line through fails here first, with the reason spelled out.
    """
    # Each of these resolves to $TMPDIR-or-/tmp, which is READ-ONLY under the unit.
    lands_in_tmp = [
        'tmp="$(mktemp)"',
        'tmp="$(mktemp -d)"',
        'tmp="$(mktemp -t probe.XXXXXX)"',
        'tmp="$(mktemp --tmpdir probe.XXXXXX)"',
        # `-t` with an expansion in the template: the `${` used to vouch for it.
        'tmp="$(mktemp -t "${PREFIX}.XXXXXX")"',
        # A bare call with an unrelated expansion elsewhere on the line. This PR's own
        # convention is to write ${BACKUP_DIR}, so the codebase SELECTS for this line.
        'mkdir -p "${BACKUP_DIR}" && tmp="$(mktemp)"',
        # An unrelated `-p` belonging to some other command on the line.
        'grep -p foo bar || true; tmp="$(mktemp)"',
        # THE TEMPLATE NAME CONTAINS `-p`. (Lucas Ferreira, #624 review 3.) These are the two
        # that shipped past the unanchored regex — and note the first uses the library's OWN
        # scratch template, so the codebase supplies the evasion for free. Both allocate in the
        # read-only /tmp. If either of these ever goes green again, deploy#613 is back.
        'err2="$(mktemp -t compose-project-XXXXXX)"',
        'tmp="$(mktemp --tmpdir backup-pg.XXXXXX)"',
    ]
    # Each of these says where the file goes. These are the real forms in the closure.
    names_its_parent = [
        'tmp="$(mktemp -p "$parent" compose-project-XXXXXX)"',
        'tmp="$(mktemp -d -p "$scratch_parent" b2-preflight-XXXXXX)"',
        'TMP="$(mktemp -p "$TEXTFILE_DIR" isnad_backup_failure.prom.XXXXXX)"',
        'tmp="$(mktemp --tmpdir=/var/lib/x probe.XXXXXX)"',
        # The positive-control half of Lucas's finding: a REAL `-p` whose template ALSO carries
        # a `-p`. Anchoring the option must not cost us the legitimate call it looks like.
        'tmp="$(mktemp -p "$parent" compose-project-XXXXXX)"',
        # Clustered short flags — `-d` and `-p` in one token. Still names its parent.
        'tmp="$(mktemp -dp "$parent" compose-project-XXXXXX)"',
    ]

    for line in lands_in_tmp:
        calls = _mktemp_invocations(line)
        assert calls, f"the filter did not even SEE a mktemp call in: {line}"
        assert not all(_names_its_parent(a) for a in calls), (
            f"FALSE GREEN: this lands in /tmp and the filter waves it through:\n    {line}\n"
            "Under ProtectSystem=strict /tmp is read-only, so this is deploy#613 again. Only "
            "an explicit `-p <val>` / `--tmpdir=<val>` is an exemption — not `-t`, not a bare "
            "`--tmpdir`, and never a `${...}` sharing the line."
        )

    for line in names_its_parent:
        calls = _mktemp_invocations(line)
        assert calls, f"the filter did not even SEE a mktemp call in: {line}"
        assert all(_names_its_parent(a) for a in calls), (
            f"FALSE RED: this names its parent and the filter flags it anyway:\n    {line}"
        )


def test_no_bare_mktemp_survives_in_the_library() -> None:
    """Structural backstop over EVERY script the hardened units can execute.

    The first cut of this test read `compose_project.sh` and nothing else — and
    `isnad-backup.service` does not ExecStart `compose_project.sh`. It ExecStarts
    **`backup.sh`**, which *sources* `b2_preflight.sh` and `compose_project.sh`. So a bare
    `mktemp` added to `backup.sh` tomorrow would be deploy#613 for the THIRD time, under
    `ProtectSystem=strict`, and this suite would not have said a word. A guard written
    against "the next caller" that covers one of the three files in the closure is not a
    guard (deploy#624 review — Nino Kavtaradze).

    The reachable set is RESOLVED FROM THE UNITS — `ExecStart=` plus the transitive `source`
    graph — never a hand-typed list, so it cannot drift away from what systemd actually runs.
    Add a script to a unit, or source one from a script a unit runs, and it is scanned from
    that moment on without anyone remembering to add it here.

    WIDENED BEYOND THE UNITS (deploy#625/#628). The unit closure alone did not cover
    `restore.sh` or `verify_b2_backup_artifact.sh` — no unit ExecStarts either — and BOTH were
    allocating with a bare `mktemp`. See `scratch_guarded_scripts` for why adopting the scratch
    library is the second, resolved membership rule.
    """
    guarded = scratch_guarded_scripts()

    # The closure must be non-empty AND contain the known members, or the scan is inert and
    # every assertion below passes vacuously (cf. calibrate_the_mutation_before_counting_it).
    #
    # PIN THE DEFINITION, NOT ONLY THE USAGES (deploy#633). If someone deletes the `source
    # scratch.sh` line from restore.sh, the *offender scan* would go quietly green — the file
    # simply drops out of the set it is being scanned in. That is the emptied-string failure
    # verbatim: a scan cannot see that the thing it scans has stopped being in scope. So
    # membership is asserted SEPARATELY and by name, and losing a member is a RED.
    names = {p.name for p in guarded}
    assert "backup.sh" in names, (
        f"the unit-reachable closure does not contain backup.sh — the resolver is broken and "
        f"this guard is inert. Found: {sorted(names)}"
    )
    assert {"compose_project.sh", "b2_preflight.sh", "scratch.sh"} <= names, (
        f"the closure did not follow `source` — backup.sh sources these, and they allocate "
        f"scratch. Found: {sorted(names)}"
    )
    assert {"restore.sh", "verify_b2_backup_artifact.sh"} <= names, (
        "restore.sh and/or verify_b2_backup_artifact.sh dropped OUT of the guarded set — which "
        "means they stopped sourcing scratch.sh. That is not a passing state: it silently "
        "removes them from this scan (deploy#633, 'a scan cannot see an emptied string') and "
        "hands them back the bare `mktemp` that deploy#628 was filed for. Both are one "
        "workflow (deploy#640) away from running somewhere /tmp is not writable.\n"
        f"Found: {sorted(names)}"
    )

    offenders: list[str] = []
    for script in sorted(guarded):
        for ln in script.read_text().splitlines():
            stripped = ln.lstrip()
            if stripped.startswith("#") or "mktemp" not in ln:
                continue
            for args in _mktemp_invocations(ln):
                if not _names_its_parent(args):
                    offenders.append(f"{script.name}: {ln.strip()}")

    assert not offenders, (
        "bare mktemp in a script the hardened units EXECUTE — it defaults to /tmp, which is "
        "READ-ONLY under ProtectSystem=strict. This is deploy#613, which has now shipped "
        "TWICE (#613 in b2_preflight.sh, #623 in compose_project.sh). Allocate under a path "
        "the unit grants — scratch_file() in compose_project.sh does this.\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# deploy#629 — the debris, and the volume that must not fill
# --------------------------------------------------------------------------


def _scratch_debris(backup_dir: Path) -> list[Path]:
    """Scratch left lying at the BACKUP_DIR ROOT — the level retention does not reach."""
    return sorted(backup_dir.glob("compose-project-*")) + sorted(backup_dir.glob("b2-preflight-*"))


def _run_and_sigterm(script: str, backup_dir: Path, tmp_path: Path) -> None:
    """Start a bash script, wait until it says it is holding a scratch, then SIGTERM it."""
    ready = tmp_path / "ready"
    body = f"""
set -euo pipefail
log() {{ printf '%s %s\\n' "$1" "$2"; }}
source "{SCRATCH_LIB}"
{script}
"""
    proc = subprocess.Popen(
        ["bash", "-c", body],
        env={**os.environ, "BACKUP_DIR": str(backup_dir), "READY": str(ready)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while not ready.exists() and time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError("the fixture script exited before it held a scratch file")
        time.sleep(0.02)
    assert ready.exists(), "the fixture never reached the point of holding a scratch"
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=20)


# The two arms differ in ONE line: whether the EXIT trap drains the registry. Anything else
# held equal, so a green result is attributable to the fix and nothing else.
_HOLD_SCRATCH = """
f="$(scratch_file)"
touch "$READY"
sleep 30
"""
_ARM_UNPROTECTED = _HOLD_SCRATCH  # no EXIT trap at all — this is the pre-fix world
_ARM_FIXED = f"""
cleanup() {{ scratch_cleanup_all; }}
trap cleanup EXIT
{_HOLD_SCRATCH}
"""


def test_a_killed_run_without_the_drain_really_does_leak(tmp_path: Path) -> None:
    """CALIBRATION. The leak detector must be able to SEE a leak, or the next test is theatre.

    This is the positive control for `test_a_killed_run_leaves_no_debris_in_backup_dir`. If
    SIGTERM somehow cleaned up on its own in this environment, that test would pass against
    ANY code — fixed or broken — and report a guarantee nobody had built
    (cf. `feedback_calibrate_the_mutation_before_counting_it`).

    It also pins the FACT the fix is built on, which is not the one deploy#629 assumed: bash
    does NOT run a `trap … RETURN` on a signal. Measured here, in the harness, rather than
    asserted in a comment.
    """
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    _run_and_sigterm(_ARM_UNPROTECTED, backup_dir, tmp_path)

    assert _scratch_debris(backup_dir), (
        "a SIGTERMed run with NO scratch drain left NOTHING behind — so this fixture cannot "
        "express the deploy#629 leak, and the test that asserts the fix works is inert. Either "
        "the kill is not landing or the allocation is not happening."
    )


def test_a_killed_run_leaves_no_debris_in_backup_dir(tmp_path: Path) -> None:
    """deploy#629. SIGTERM mid-run must not leave scratch at the BACKUP_DIR root.

    Previously this debris landed in /tmp, which the system reaps. **BACKUP_DIR's root has no
    reaper**: retention purges the REMOTE, and local cleanup covers LOCAL_BACKUP_PATH — the
    per-run subdirectory, not the root above it.

    The files are a byte each, so the leak is slow. It is worth fixing anyway because of what a
    FULL BACKUP_DIR now does: an ENOSPC there makes `scratch_file` fail mid-run, and the
    `service_is_running neo4j` call site renders that as "Neo4j is NOT running" — a false claim
    about the graph whose implied remedy ("start it and re-run") is the one action that makes a
    full disk worse. The volume filling stopped being a storage problem and became a
    LYING-DIAGNOSTIC problem.

    Note what the fix is NOT: deploy#629 proposed a `trap … RETURN` in each caller, and the
    control above measures that a RETURN trap does not fire on a signal at all. The EXIT trap
    does, so the registry is drained from there.
    """
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    _run_and_sigterm(_ARM_FIXED, backup_dir, tmp_path)

    debris = _scratch_debris(backup_dir)
    assert not debris, (
        "a killed run left scratch at the BACKUP_DIR root, which has no reaper — it will sit "
        "there until the volume the dumps themselves fill runs out, at which point the backup "
        f"starts reporting 'Neo4j is NOT running' about a healthy graph.\nLeaked: {debris}"
    )


def _dead_pid() -> int:
    """A PID that is definitely not running: spawn a child, reap it, return its pid."""
    victim = subprocess.Popen(["true"])  # noqa: S607
    victim.wait()
    return victim.pid


def _reap(backup_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
            "bash",
            "-c",
            f'set -euo pipefail\nlog() {{ :; }}\nsource "{SCRATCH_LIB}"\nscratch_reap_stale\n',
        ],
        env={**os.environ, "SCRATCH_PARENT": str(backup_dir)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_reaper_uses_liveness_not_age(tmp_path: Path) -> None:
    """deploy#668. AGE IS NOT LIVENESS — and the fixture that was missing is the only one that
    mattered.

    SIGKILL is untrappable, so no trap of any kind closes the leak; only a reaper does. But the
    first reaper deleted anything older than `-mmin +60`, reasoning that a scratch lives for the
    duration of one `dc ps` — seconds. That is true of the sites it was written for. It is
    **false of `restore.sh`'s pg_restore capture**, which is held for as long as the restore
    runs — and a production dump can take well over an hour. The nightly backup's lazy reap
    would then match a **live** file by template and delete it out from under a running restore.
    **During a DR.**

    The old fixture could not have caught it: it tested a FRESH live file, which is the case
    where the age heuristic is RIGHT. There was no old-and-live fixture, and old-and-live is the
    ONLY case where it is wrong (cf. `feedback_fixture_makes_guard_assertion_inert`).

    So age is gone. The PID is already in the filename, so liveness is OBSERVABLE: ask
    `/proc/<pid>`. All four cases are enumerated below; the third is the one that did not exist.
    """
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    two_hours_ago = time.time() - 2 * 3600
    dead, alive = _dead_pid(), os.getpid()

    # 1. OLD + owner DEAD -> debris from a SIGKILLed run. Must go.
    old_dead = backup_dir / f"compose-project-{dead}-AAAAAA"
    old_dead.write_text("debris from a SIGKILLed run\n")
    os.utime(old_dead, (two_hours_ago, two_hours_ago))

    # 2. FRESH + owner ALIVE -> a concurrent run. Must stay. (The case the old fixture had.)
    fresh_live = backup_dir / f"compose-project-{alive}-BBBBBB"
    fresh_live.write_text("a concurrent run is using this\n")

    # 3. OLD + owner ALIVE -> **the >1h pg_restore, mid-DR.** Must stay.
    #    THIS IS THE FIXTURE THAT DID NOT EXIST, and the only one the age heuristic fails.
    old_live = backup_dir / f"compose-project-{alive}-CCCCCC"
    old_live.write_text("a pg_restore of a 40GB dump, still running\n")
    os.utime(old_live, (two_hours_ago, two_hours_ago))

    # 4. A real dump directory, old. Never scratch, never touched.
    dump_dir = backup_dir / "2026-07-14T030100Z"
    dump_dir.mkdir()
    (dump_dir / "isnad-pg.dump").write_text("precious\n")
    os.utime(dump_dir, (two_hours_ago, two_hours_ago))

    proc = _reap(backup_dir)
    assert proc.returncode == 0, f"the reaper failed: {proc.stderr}"

    assert not old_dead.exists(), (
        "the reaper left debris whose owning process is GONE — that is the SIGKILL leak it "
        "exists to sweep, and nothing else will ever remove it."
    )
    assert old_live.exists(), (
        "THE REAPER DELETED A LIVE SCRATCH BECAUSE IT WAS OLD. Its owning process is still "
        "running: this is restore.sh's pg_restore capture during a multi-hour recovery, and the "
        "nightly backup just pulled the file out from under it. Age is not liveness — the PID is "
        "in the filename, so ask /proc rather than guessing from mtime. (deploy#668)"
    )
    assert fresh_live.exists(), "the reaper deleted a fresh scratch held by a live process"
    assert dump_dir.exists() and (dump_dir / "isnad-pg.dump").exists(), (
        "THE REAPER ATE A DUMP DIRECTORY. It runs at the root of the volume the backups live "
        "on; it must match only this library's own PID-tagged template, and never descend."
    )


def test_the_reaper_refuses_names_it_cannot_identify(tmp_path: Path) -> None:
    """No PID in the name means no liveness signal — and no liveness signal means DO NOT DELETE.

    Refusing to remove what it cannot identify is the only safe default at the root of the volume
    the dumps live on. `b2-preflight-*` is the real instance: b2_preflight.sh allocates those,
    they carry no PID, and I will not guess with another script's artifacts. Their SIGKILL debris
    is pre-existing and belongs in its own issue, not in a heuristic here.
    """
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    two_hours_ago = time.time() - 2 * 3600

    unidentifiable = [
        backup_dir / "compose-project-NOTAPID-XXXX",  # our template, but no numeric pid
        backup_dir / "b2-preflight-abc123",  # another script's scratch, no pid at all
    ]
    for path in unidentifiable:
        path.write_text("not mine to judge\n")
        os.utime(path, (two_hours_ago, two_hours_ago))

    proc = _reap(backup_dir)
    assert proc.returncode == 0, f"the reaper failed: {proc.stderr}"
    for path in unidentifiable:
        assert path.exists(), (
            f"the reaper deleted {path.name}, whose owner it cannot determine. With no PID there "
            f"is no liveness signal, and deleting on a guess at the root of the backup volume is "
            f"exactly the class of move this PR exists to stop."
        )


# --------------------------------------------------------------------------
# THE PROPERTY, NOT THE PROXY (deploy#666 — Nino Kavtaradze, blocking)
#
# "no bare mktemp" is a PROXY. The property that actually matters is:
#
#   the scratch parent a script resolves is inside the writable set of EVERY hardened unit
#   that can reach it
#
# and that is decidable from the files already being parsed — `ReadWritePaths=` and
# `Environment=` sit in the unit, next to the `ExecStart=` the resolver already reads.
#
# The proxy passed `isnad-backup-failure-marker.service` while the property FAILED there:
# ReadWritePaths=/var/lib/node_exporter only, no BACKUP_DIR, no TMPDIR, ProtectSystem=strict —
# so scratch_parent()'s old chain resolved to the READ-ONLY /tmp that started this class. The
# closure had correctly identified the script as living under hardening, and then scanned it
# for the wrong invariant.
# --------------------------------------------------------------------------


def _units() -> list[Path]:
    return sorted(SYSTEMD_DIR.glob("*.service"))


def _hardened_units() -> list[Path]:
    return [u for u in _units() if "ProtectSystem=strict" in u.read_text()]


def _unit_env(text: str) -> dict[str, str]:
    """`Environment=KEY=VALUE` lines, as a dict."""
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("Environment="):
            continue
        assignment = line[len("Environment=") :].strip().strip('"')
        if "=" in assignment:
            key, _, value = assignment.partition("=")
            env[key.strip()] = value.strip().strip('"')
    return env


def _read_write_paths(text: str) -> list[str]:
    """Every path granted by `ReadWritePaths=`. The directive may repeat and may list several."""
    paths: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ReadWritePaths="):
            paths.extend(line[len("ReadWritePaths=") :].split())
    return [p.lstrip("-+") for p in paths]


def scratch_parent_chain() -> tuple[list[str], str]:
    """The fallback chain, PARSED OUT OF scratch.sh — never restated here.

    Restating `BACKUP_DIR -> TMPDIR -> /tmp` in this file would be a THIRD place to encode the
    same assumption, and it would drift the moment the library changed — which is the exact
    criticism that produced this test. So the chain is read from the shipped `scratch_parent()`:

        printf '%s\\n' "${SCRATCH_PARENT:-${BACKUP_DIR:-${TMPDIR:-/tmp}}}"

    -> (["SCRATCH_PARENT", "BACKUP_DIR", "TMPDIR"], "/tmp")
    """
    body = SCRATCH_LIB.read_text()
    m = re.search(r"scratch_parent\(\)\s*\{(.*?)\n\}", body, re.S)
    assert m, "scratch_parent() not found in scratch.sh — this test cannot resolve the chain"
    names = re.findall(r"\$\{(\w+):-", m.group(1))
    assert names, f"no `${{VAR:-...}}` fallback chain found in scratch_parent():\n{m.group(1)}"
    terminal = re.search(r":-([^}$\"\s]+)\}+\"?\s*$", m.group(1).strip().splitlines()[-1].strip())
    assert terminal, f"could not find the terminal default in scratch_parent():\n{m.group(1)}"
    return names, terminal.group(1)


def _resolved_parent(env: dict[str, str]) -> str:
    names, terminal = scratch_parent_chain()
    for name in names:
        if env.get(name):
            return env[name]
    return terminal


def _within(path: str, allowed: list[str]) -> bool:
    p = PurePosixPath(path)
    return any(p == PurePosixPath(a) or PurePosixPath(a) in p.parents for a in allowed)


def test_the_chain_parser_actually_reads_the_shipped_chain() -> None:
    """Calibrate the parser before trusting anything it says.

    If this returned an empty chain, `_resolved_parent` would fall straight to the terminal for
    every unit and the property test below would assert something nobody wrote.
    """
    names, terminal = scratch_parent_chain()
    assert names[0] == "SCRATCH_PARENT", (
        f"the chain no longer starts with the unit-declared SCRATCH_PARENT: {names}"
    )
    assert "BACKUP_DIR" in names, f"BACKUP_DIR fell out of the chain: {names}"
    assert terminal == "/tmp", (
        f"the terminal default is {terminal!r}. If the chain no longer ends at /tmp that may be "
        f"an improvement — but this test's whole premise is that the terminal is a GUESS, so "
        f"re-read it before editing this assertion."
    )


def test_every_hardened_unit_declares_a_writable_scratch_parent() -> None:
    """THE PROPERTY. For every hardened unit: the parent scratch.sh resolves is WRITABLE there.

    This is what the bare-`mktemp` scan was a proxy for, and the proxy was green on a unit where
    the property was false. Note what this test does NOT look at: **it never reads a script.** A
    unit can fail this with no `mktemp` anywhere in its closure — because the defect is in the
    relationship between the unit's `Environment=` and its `ReadWritePaths=`, not in any line of
    shell. That is the whole point of the correction (deploy#666).

    `isnad-backup-failure-marker.service` is the case in hand: `ReadWritePaths=/var/lib/
    node_exporter`, no `BACKUP_DIR`, no `TMPDIR` — so the old chain landed on the read-only
    `/tmp`. And it is the `OnFailure=` handler, so the failure would surface only on a night
    when something else had already broken.
    """
    hardened = _hardened_units()
    assert hardened, "no hardened units found — this test is inert"

    failures: list[str] = []
    for unit in hardened:
        text = unit.read_text()
        env = _unit_env(text)
        rwp = _read_write_paths(text)
        parent = _resolved_parent(env)
        if not _within(parent, rwp):
            failures.append(
                f"{unit.name}: scratch.sh resolves its parent to {parent!r}, which is NOT "
                f"inside ReadWritePaths={rwp}. Under ProtectSystem=strict that path is "
                f"READ-ONLY, so every scratch allocation in this unit fails — and the caller "
                f"reports that failure as a fact about whatever it was checking (deploy#613)."
            )

    assert not failures, (
        "a hardened unit resolves a scratch parent it cannot write to. Declare "
        "`Environment=SCRATCH_PARENT=<path>` in the unit, with a path inside its own "
        "`ReadWritePaths=` — do not rely on the library's fallback chain, whose terminal rung "
        "is the read-only /tmp that caused deploy#613 and deploy#623.\n  " + "\n  ".join(failures)
    )


def test_named_scratch_parents_in_unit_scripts_are_writable_under_every_reaching_unit() -> None:
    """The same property, for `mktemp -p "$DIR"` calls that name their parent LITERALLY.

    `emit-backup-failure-marker.sh` does not use the library — it allocates with
    `mktemp -p "$TEXTFILE_DIR"`, where `TEXTFILE_DIR` is assigned a literal absolute path. That
    satisfies the no-bare-mktemp proxy trivially. It satisfies the PROPERTY only because that
    literal happens to sit inside the unit's `ReadWritePaths=` — and nothing was checking.

    So: resolve literal assignments in each unit-reachable script, and require every named
    parent to be writable under EVERY hardened unit that can reach that script.

    LIMIT, STATED PLAINLY: this resolves literal assignments only. A parent computed at runtime
    is not decidable here, and this test will not see it. That is why the library exists and why
    `test_every_hardened_unit_declares_a_writable_scratch_parent` above is the load-bearing one
    — this is a second net under the calls that bypass the library, not a proof about all of them.
    """
    # Which hardened units can reach which scripts.
    reach: dict[Path, list[Path]] = {}
    for unit in _hardened_units():
        for script in _closure_of_unit(unit):
            reach.setdefault(script, []).append(unit)
    assert reach, "no hardened unit reaches any script — inert"

    failures: list[str] = []
    for script, units in sorted(reach.items()):
        text = script.read_text()
        literals = dict(re.findall(r'^\s*(\w+)="?(/[^"\s$]+)"?\s*$', text, re.M))
        for ln in text.splitlines():
            if ln.lstrip().startswith("#") or "mktemp" not in ln:
                continue
            for args in _mktemp_invocations(ln):
                m = re.search(
                    r'(?:\s|^)-\w*p\s*"?\$\{?(\w+)\}?"?|(?:\s|^)-\w*p\s*"?(/\S+?)"?[\s"]', args
                )
                if not m:
                    continue
                target = literals.get(m.group(1)) if m.group(1) else m.group(2)
                if not target:
                    continue  # not statically resolvable — see LIMIT above
                for unit in units:
                    rwp = _read_write_paths(unit.read_text())
                    if not _within(target, rwp):
                        failures.append(
                            f"{script.name}: allocates into {target!r}, which is NOT inside "
                            f"{unit.name}'s ReadWritePaths={rwp}. That path is READ-ONLY under "
                            f"ProtectSystem=strict."
                        )

    assert not failures, (
        "a unit-reachable script names a scratch parent that its own unit cannot write to:\n  "
        + "\n  ".join(failures)
    )


def test_the_pin_sees_every_exec_directive(tmp_path: Path) -> None:
    """A unit runs `ExecStartPre=` in the SAME sandbox as `ExecStart=`. The pin must see both.

    THE HOLE, AND WHY MY CALIBRATION DID NOT FIND IT.

    `unit_reachable_scripts` matched `line.startswith("ExecStart=")`, which excludes
    `ExecStartPre=`, `ExecStartPost=`, `ExecStop=`, `ExecStopPost=`, `ExecCondition=` and
    `ExecReload=`. systemd runs every one of them under `ProtectSystem=strict`, so a bare
    `mktemp` in an `ExecStartPre=` helper is deploy#613 verbatim — and the pin was silent.

    The mutation I originally used to "prove" the unit rule worked injected an `ExecStartPost=`
    script that ALSO sourced `scratch.sh`. The **adopter** rule flagged it, the suite went red,
    and I credited the unit rule. **A guard passed for the wrong reason, and my calibration
    could not tell.** So this test isolates the unit rule: the fixture script does NOT source
    the library and does NOT mention it, which makes the adopter rule structurally incapable of
    seeing it. If it is caught, it is caught by the unit graph or not at all.

    (Same species as Nurul's finding on the #654 harness, where `Unit.one()` took the LAST
    `ExecStart=` and a `Type=oneshot` unit's FIRST one — carrying this very defect — was never
    run. A resolver that reads a subset of the directives is a scan looking at the wrong thing.)

    THE FIXTURE TREE IS BUILT UNDER `tmp_path`, NEVER IN THE REAL REPO. (deploy#674, Bereket
    Tadesse.) The first version of this test rewrote the deployable `systemd/isnad-backup.service`
    on disk four times and dropped a bare-`mktemp` helper into the live `scripts/` directory,
    restoring in a `finally`. `finally` does not cover a SIGKILL, an OOM, or a CI timeout — so an
    interrupted run could leave a mutated PRODUCTION UNIT and a planted bad script behind. Worse,
    it planted that script in the exact directory `test_no_bare_mktemp_survives_in_the_library`
    scans, so under `pytest-xdist` the two tests could observe each other and false-RED on a
    `mktemp` the suite planted itself. A test that edits deployable files to exercise itself is
    the very "measurement mutating the thing it measures" this PR exists to stop. So the resolver
    now takes `systemd_dir`/`scripts_dir`, and everything below lives under `tmp_path`.
    """
    systemd_dir = tmp_path / "systemd"
    scripts_dir = tmp_path / "scripts"
    systemd_dir.mkdir()
    scripts_dir.mkdir()

    # A real entry point, so the closure is non-empty and the resolver has something to anchor on.
    (scripts_dir / "backup.sh").write_text("#!/usr/bin/env bash\n:\n")
    # The probe: no `source`, no mention of scratch.sh, no scratch_file/scratch_dir call — so the
    # ADOPTER rule is structurally incapable of seeing it. If it is caught, the UNIT graph caught
    # it. That isolation is the whole point (see the calibration-passed-for-the-wrong-reason note).
    (scripts_dir / "helper.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nstaging="$(mktemp -d)"\necho "$staging"\n'
    )

    base = "[Service]\nType=oneshot\nProtectSystem=strict\nReadWritePaths=/var/lib/node_exporter\n"
    for directive in ("ExecStartPre", "ExecStartPost", "ExecStop", "ExecCondition"):
        (systemd_dir / "probe.service").write_text(
            base
            + f"{directive}=/opt/noorinalabs-deploy/scripts/helper.sh\n"
            + "ExecStart=/opt/noorinalabs-deploy/scripts/backup.sh\n"
        )
        reachable = {p.name for p in unit_reachable_scripts(systemd_dir, scripts_dir)}
        assert "helper.sh" in reachable, (
            f"a script the unit runs via `{directive}=` is NOT in the unit-reachable closure. "
            f"systemd executes it under the SAME ProtectSystem=strict sandbox as ExecStart=, so "
            f"a bare mktemp in it is deploy#613 — and the pin cannot see it. Resolve EVERY Exec* "
            f"directive, not just ExecStart=.\nFound: {sorted(reachable)}"
        )

    # A Type=oneshot unit legally carries MULTIPLE ExecStart= lines, run in sequence. The defect
    # in the FIRST one must be caught even though a real entry point follows it (Nurul, #654).
    (systemd_dir / "probe.service").write_text(
        base
        + "ExecStart=/opt/noorinalabs-deploy/scripts/helper.sh\n"
        + "ExecStart=/opt/noorinalabs-deploy/scripts/backup.sh\n"
    )
    reachable = {p.name for p in unit_reachable_scripts(systemd_dir, scripts_dir)}
    assert "helper.sh" in reachable, (
        "a defect in the FIRST of several ExecStart= lines was missed — a resolver that takes "
        f"only one line is Nurul's #654 finding. Found: {sorted(reachable)}"
    )

    # Calibration: the resolver must not simply return every script in the directory. A script no
    # unit references and nothing sources must NOT appear, or the assertions above are inert.
    (scripts_dir / "never_referenced.sh").write_text("#!/usr/bin/env bash\n:\n")
    reachable = {p.name for p in unit_reachable_scripts(systemd_dir, scripts_dir)}
    assert "never_referenced.sh" not in reachable, (
        "the resolver returned a script that NO unit references and nothing sources — it is just "
        "globbing the directory, so every 'yes' above meant nothing."
    )

    # THE SOURCE-GRAPH CLOSURE MUST READ THE FIXTURE TREE, NOT THE REAL ONE. (Weronika Zielinska,
    # #678 review.) `unit_reachable_scripts` forwarded `scripts_dir` to the ENTRY-POINT step but
    # not to `_close_over_source_graph`, so the source-graph closure silently globbed the REAL
    # `scripts/`. A helper reachable ONLY through `source` — the exact form the closure exists to
    # follow — was therefore invisible in a fixture, resolving to `['backup.sh']` only. That is
    # the #674 fidelity trap (a test reading the real tree) reintroduced one function over, and it
    # made the globbing calibration above partially inert. This is the fixture that catches it.
    (scripts_dir / "backup.sh").write_text(
        '#!/usr/bin/env bash\nLIB="$(dirname "$0")/sourced_helper.sh"\nsource "$LIB"\n'
    )
    (scripts_dir / "sourced_helper.sh").write_text("#!/usr/bin/env bash\n:\n")
    (systemd_dir / "probe.service").write_text(
        base + "ExecStart=/opt/noorinalabs-deploy/scripts/backup.sh\n"
    )
    reachable = {p.name for p in unit_reachable_scripts(systemd_dir, scripts_dir)}
    assert "sourced_helper.sh" in reachable, (
        "a helper reachable ONLY through the `source` graph, under tmp_path, was NOT resolved — "
        "so the closure step scanned the REAL scripts/ dir, not the fixture. This is the #674 "
        f"fidelity trap one function over. Found: {sorted(reachable)}"
    )

    # And the consequence that matters: a bare `mktemp` in that source-reached helper must be a
    # findable offender. This is the offender scan of `test_no_bare_mktemp_survives_in_the_library`
    # run over the FIXTURE closure — it proves the closure not only lists the helper but that the
    # bare-mktemp rule then reaches it. Before the line-147 fix this found NOTHING, because the
    # helper was never in the closure to begin with.
    (scripts_dir / "sourced_helper.sh").write_text(
        '#!/usr/bin/env bash\nerr="$(mktemp)"\necho "$err"\n'
    )
    offenders = [
        f"{script.name}: {ln.strip()}"
        for script in unit_reachable_scripts(systemd_dir, scripts_dir)
        for ln in script.read_text().splitlines()
        if not ln.lstrip().startswith("#") and "mktemp" in ln
        for args in _mktemp_invocations(ln)
        if not _names_its_parent(args)
    ]
    assert any("sourced_helper.sh" in o for o in offenders), (
        "a bare `mktemp` in a helper reached THROUGH the source graph was not flagged. Either "
        "the closure did not follow `source` into the fixture, or it followed it into the real "
        f"tree where the bare mktemp does not exist. Offenders seen: {offenders}"
    )


def test_the_allocators_own_words_reach_the_caller(tmp_path: Path) -> None:
    """The REASON the scratch failed must survive the subshell — and it nearly did not.

    The point of removing the old `2>/dev/null` is that ENOSPC, a read-only mount and a missing
    directory are three different problems with three different remedies, and the allocator is
    the only thing that knows which one it hit. Discarding that distinction is exactly what let
    deploy#613 read as a bad credential.

    My first implementation stored it in a `SCRATCH_ERROR` global — and it came back EMPTY at
    every call site, because every caller allocates as `err="$(scratch_file)"`, a COMMAND
    SUBSTITUTION, and a variable set inside that subshell dies with it. The diagnostic printed a
    blank where the reason should have been. Nothing failed; the feature was simply INERT, and I
    would have shipped a PR claiming it worked (cf. `feedback_silent_zero_is_not_a_measurement`).

    STDERR is not captured by the substitution, so that is the channel. This test asserts the
    real thing an operator needs — the allocator's actual complaint, not a placeholder — and
    calibrates on BOTH sides so "always prints a reason" cannot pass for "prints the reason".
    """
    proc = _run(
        "running_services || true",
        backup_dir=UNWRITABLE_PARENT,
        with_docker=True,
        tmp_path=tmp_path,
    )
    combined = proc.stdout + proc.stderr

    assert "Scratch allocation FAILED" in combined, (
        "the scratch failure did not report WHY. A variable cannot carry it out of the "
        f'`err="$(scratch_file)"` subshell — stderr can.\n\n{combined}'
    )
    # The allocator's OWN text, not our paraphrase. On /proc/self the kernel refuses the create,
    # and the message names the reason. If this ever becomes a bare "allocation failed" with no
    # errno text, the operator is back to guessing between a full disk and a read-only mount.
    assert re.search(r"Scratch allocation FAILED:.*\S", combined), (
        f"the reason line is present but EMPTY — the classic subshell-eaten variable:\n{combined}"
    )
    assert re.search(
        r"(Permission denied|Read-only file system|No such file|Not a directory|cannot create)",
        combined,
    ), (
        "the reason line does not carry the ALLOCATOR's own errno text, so it cannot separate "
        f"ENOSPC from a read-only mount from a missing parent:\n\n{combined}"
    )

    # Calibration: a WORKING allocation must print no failure line at all. Without this, a
    # `_scratch_report` that fired unconditionally would satisfy every assertion above.
    good = tmp_path / "backups"
    good.mkdir()
    ok = _run(
        "running_services > /dev/null && echo FINE",
        backup_dir=str(good),
        with_docker=True,
        tmp_path=tmp_path,
    )
    ok_combined = ok.stdout + ok.stderr
    assert "FINE" in ok_combined and "Scratch allocation FAILED" not in ok_combined, (
        "a SUCCESSFUL allocation reported a scratch failure — the reason line fires "
        f"unconditionally and therefore means nothing.\n\n{ok_combined}"
    )


def test_no_lingering_return_trap_kills_the_next_function(tmp_path: Path) -> None:
    """The fix deploy#629 ASKED for is a bug, and this is the test that caught it.

    `trap 'rm -f -- "$err"' RETURN` inside `running_services` looks obviously right. It is not:

      * **A RETURN trap is GLOBAL and stays armed.** It is not scoped to the function that
        installed it. After `running_services` returns, the trap is still there, and it fires
        AGAIN on the next function's return — where `err` no longer exists. Under `set -u` that
        is `err: unbound variable`, errexit fires, and THE BACKUP DIES. If that later function
        happens to have an `err` of its own, it is worse than a crash: the stale trap deletes
        the WRONG FILE.
      * **It does not even cover the case it was raised for.** A RETURN trap does not fire on a
        signal, so it never cleans up the SIGTERM deploy#629 is actually about (see the kill
        test above).

    So the release is explicit and the EXIT drain is the backstop.

    THE FIXTURE MUST BE THE PRODUCTION SHAPE, AND MY FIRST ONE WAS NOT. Measured, the stale trap
    does NOT re-fire on a sibling function called at top level — it fires when the function that
    CALLED the allocating one returns. My first fixture called `running_services` and then an
    unrelated function from the top level, and it stayed GREEN against a reintroduced RETURN
    trap: a regression test for a bug it could not express
    (cf. `feedback_fixture_makes_guard_assertion_inert`).

    The real callers are nested, which is why this bites for real:

        restore.sh:406  list_backups()  ->  list_category()   [allocates]
        backup.sh:480   (a function)    ->  neo4j_start()     [allocates]

    So the fixture nests too. `outer` is what `list_backups` is.
    """
    good = tmp_path / "backups"
    good.mkdir()

    proc = _run(
        # The allocating function called FROM ANOTHER FUNCTION — the shape every real caller
        # has. A lingering RETURN trap detonates on `outer`'s return, not on the inner one's.
        "outer() { running_services > /dev/null; }\nouter\necho SURVIVED",
        backup_dir=str(good),
        with_docker=True,
        tmp_path=tmp_path,
    )
    combined = proc.stdout + proc.stderr

    assert "unbound variable" not in combined, (
        "a RETURN trap set inside a scratch-allocating function is still armed when the NEXT "
        "function returns, and `$err` is gone by then — `set -u` turns that into a fatal "
        "`unbound variable` in the middle of a backup. This is why the release is explicit and "
        f"the EXIT trap is the backstop.\n\n{combined}"
    )
    assert "SURVIVED" in combined and proc.returncode == 0, (
        f"the library did not survive a second function call after allocating scratch:\n{combined}"
    )


def _code(script: Path) -> str:
    """The script with comment lines stripped — so a mention in prose is never a match."""
    return "\n".join(
        ln for ln in script.read_text().splitlines() if not ln.lstrip().startswith("#")
    )


def test_every_scratch_allocating_entry_point_drains_on_exit() -> None:
    """The EXIT drain must be pinned in the REAL scripts, not only in the test's own fixtures.

    WHY THIS EXISTS: I calibrated the kill test by deleting `scratch_cleanup_all` from
    restore.sh's EXIT trap — and the whole module stayed GREEN. The leak test drives its own
    two-arm fixture, so it proves the LIBRARY MECHANISM works while saying nothing about whether
    the scripts that ship actually call it. A guard that passes when you remove the fix is not a
    guard (cf. `feedback_calibrate_the_mutation_before_counting_it`), and this is the third time
    in this file's history that the measurement, not the code, was the broken thing
    (cf. `feedback_measurement_is_the_thing_that_breaks`).

    The rule, RESOLVED rather than hand-typed:

        a script that ALLOCATES scratch (directly, or by sourcing compose_project.sh, whose
        running_services/neo4j_start allocate) and INSTALLS AN EXIT TRAP is an entry point,
        and it MUST drain the registry — because the EXIT trap is the only one that runs on a
        SIGTERM, which is how systemd stops a unit that is taking too long.

    Libraries (no EXIT trap) are exempt: they cannot install one without clobbering their
    caller's, which is exactly why draining is the CALLER's job and has to be asserted here.
    """
    allocators_with_exit_traps: list[Path] = []
    for script in sorted(scratch_guarded_scripts()):
        body = _code(script)
        allocates = "scratch_file" in body or "scratch_dir" in body or "compose_project.sh" in body
        installs_exit_trap = re.search(r"\btrap\b[^\n]*\bEXIT\b", body) is not None
        if allocates and installs_exit_trap:
            allocators_with_exit_traps.append(script)

    # The set must be non-empty and must contain the three real entry points, or the loop below
    # iterates nothing and reports success over an empty room.
    found = {p.name for p in allocators_with_exit_traps}
    assert {"backup.sh", "restore.sh", "verify_b2_backup_artifact.sh"} <= found, (
        f"the entry-point resolver did not find the scripts that allocate scratch and trap "
        f"EXIT — it is inert, and the drain assertion below proves nothing. Found: {sorted(found)}"
    )

    missing = [p.name for p in allocators_with_exit_traps if "scratch_cleanup_all" not in _code(p)]
    assert not missing, (
        "these scripts allocate scratch and install an EXIT trap, but never drain the registry "
        "from it. A `trap … RETURN` does NOT fire on a signal (measured, see the kill test), so "
        "the EXIT trap is the ONLY thing that cleans up when systemd stops or times out the "
        "unit mid-run. Without the drain the scratch lands at the BACKUP_DIR root, which has no "
        "reaper, on the volume the dumps themselves fill — and a full BACKUP_DIR makes the "
        "backup report 'Neo4j is NOT running' about a healthy graph (deploy#629).\n  "
        + "\n  ".join(missing)
    )


def test_scratch_dir_refuses_an_unwritable_parent_and_says_so(tmp_path: Path) -> None:
    """The directory variant carries the same contract as the file variant.

    `mktemp -d` in verify_b2_backup_artifact.sh was the fourth bare allocation (deploy#628), and
    a directory has an extra way to lie: it can EXIST and still refuse a file. A caller that
    redirects into `$dir/out` then gets an ambiguous-redirect rc it will report as a failure of
    whatever it was measuring — deploy#613's shape, one layer down.
    """
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail\nlog() {{ printf "%s %s\\n" "$1" "$2"; }}\n'
            f'source "{SCRATCH_LIB}"\n'
            f'if d="$(scratch_dir)"; then echo "ALLOCATED=$d"; else '
            f'scratch_failed "The dir probe" "the thing it was checking"; fi\n',
        ],
        env={**os.environ, "BACKUP_DIR": UNWRITABLE_PARENT, "TMPDIR": UNWRITABLE_PARENT},
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    assert "ALLOCATED=" not in combined, (
        f"scratch_dir handed back a directory under an unwritable parent:\n{combined}"
    )
    assert "could not RUN" in combined and "NO evidence" in combined, (
        f"scratch_dir failed without the honest diagnostic:\n{combined}"
    )

    # Positive control: it must still be able to SUCCEED, and the directory must be writable —
    # not merely exist. Without this, a scratch_dir that always failed would pass the above.
    good = tmp_path / "backups"
    good.mkdir()
    ok = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail\nlog() {{ :; }}\nsource "{SCRATCH_LIB}"\n'
            f'd="$(scratch_dir)"\ntouch "$d/proof"\necho "OK=$d"\n',
        ],
        env={**os.environ, "BACKUP_DIR": str(good)},
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0 and "OK=" in ok.stdout, (
        f"scratch_dir could not allocate under a WRITABLE parent:\n{ok.stderr}"
    )


def test_service_is_running_separates_not_running_from_could_not_find_out(tmp_path: Path) -> None:
    """THE THIRD CALL SITE — and deploy#624 is what made it reachable. (Lucas Ferreira)

    `service_is_running` used to collapse "the service is not running" and "I could not take
    the listing" into a single `return 1`. The comment justifying that said the two "collapse
    harmlessly", and while scratch lived on /tmp it was true — **because /tmp never fills.**

    deploy#623's fix moves scratch onto BACKUP_DIR: the volume the dumps themselves fill. And
    the ordering is against us — backup.sh gates on assert_stack_present BEFORE the Postgres
    dumps and asks `service_is_running neo4j` AFTER them, so the disk that was fine at the
    gate can be full by the time we ask.

    Collapsed, that produces a lie with a dangerous remedy attached: scratch fails, this
    returns 1, and backup.sh prints "Neo4j is NOT running — Start Neo4j and re-run." The graph
    is UP. The disk is FULL. Re-running is the one action that makes it worse.

    So: rc=1 is a fact about the STACK; rc=2 is a fact about US.
    """
    # Docker is healthy and reports neo4j:running. Only the scratch is broken.
    proc = _run(
        # `|| rc=$?` — the exact form backup.sh uses. A bare call under `set -e` would abort
        # the driver before the echo, and the test would be measuring errexit, not the code.
        "rc=0; service_is_running neo4j || rc=$?; echo RC=$rc",
        backup_dir=UNWRITABLE_PARENT,
        with_docker=True,
        tmp_path=tmp_path,
    )
    combined = proc.stdout + proc.stderr

    assert "RC=2" in combined, (
        "a scratch failure returned the NOT-RUNNING code. The graph is up and the docker stub "
        "says so; the only thing that failed is a temp file. Collapsing 'I could not find out' "
        f"into 'it is not running' is what tells an operator with a FULL DISK to re-run the "
        f"backup — the single action that makes it worse.\n\n{combined}"
    )
    assert "RC=1" not in combined

    # Positive control: with a writable scratch, the SAME function must still be able to say
    # "not running" — otherwise rc=2 is just the new constant and nothing is distinguished.
    good = tmp_path / "backups"
    good.mkdir()
    proc_ok = _run(
        "rc=0; service_is_running redis || rc=$?; echo RC=$rc",  # stub lists only pg/user-pg/neo4j
        backup_dir=str(good),
        with_docker=True,
        tmp_path=tmp_path,
    )
    assert "RC=1" in (proc_ok.stdout + proc_ok.stderr), (
        "with a WORKING scratch, a genuinely absent service must still return 1. If it does "
        "not, the fix has simply replaced one constant with another and distinguishes nothing."
    )
