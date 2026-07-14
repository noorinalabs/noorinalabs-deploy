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
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
SYSTEMD_DIR = REPO_ROOT / "systemd"
LIB = SCRIPTS_DIR / "compose_project.sh"


def unit_reachable_scripts() -> set[Path]:
    """Every script a HARDENED systemd unit can execute, resolved from the units themselves.

    `ExecStart=` gives the entry points; the transitive `source`/`.` graph gives the rest. A
    hand-typed list would drift the moment someone adds a unit or sources a new helper — and
    drift in THIS list means a script runs under `ProtectSystem=strict` with nobody checking
    where its scratch lands, which is the whole of deploy#613/#623.

    Scoped to units that are actually hardened: an unhardened unit has a writable /tmp and a
    bare `mktemp` in it is not a bug.
    """
    entry: set[Path] = set()
    for unit in sorted(SYSTEMD_DIR.glob("*.service")):
        text = unit.read_text()
        if "ProtectSystem=strict" not in text:
            continue
        for line in text.splitlines():
            if not line.startswith("ExecStart="):
                continue
            for token in line[len("ExecStart=") :].split():
                name = Path(token.lstrip("-@+!")).name
                candidate = SCRIPTS_DIR / name
                if candidate.is_file() and candidate.suffix == ".sh":
                    entry.add(candidate)

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
    seen: set[Path] = set()
    pending = list(entry)
    siblings = sorted(SCRIPTS_DIR.glob("*.sh"))
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
    """
    reachable = unit_reachable_scripts()

    # The closure must be non-empty AND contain the entry points, or the scan is inert and
    # every assertion below passes vacuously (cf. calibrate_the_mutation_before_counting_it).
    names = {p.name for p in reachable}
    assert "backup.sh" in names, (
        f"the unit-reachable closure does not contain backup.sh — the resolver is broken and "
        f"this guard is inert. Found: {sorted(names)}"
    )
    assert {"compose_project.sh", "b2_preflight.sh"} <= names, (
        f"the closure did not follow `source` — backup.sh sources both of these, and both "
        f"allocate scratch. Found: {sorted(names)}"
    )

    offenders: list[str] = []
    for script in sorted(reachable):
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
