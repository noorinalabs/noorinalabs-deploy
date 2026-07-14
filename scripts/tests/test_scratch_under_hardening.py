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
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
LIB = SCRIPTS_DIR / "compose_project.sh"

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


def test_no_bare_mktemp_survives_in_the_library() -> None:
    """Structural backstop: the next caller must not have to remember any of this.

    A bare `mktemp` (no `-p`, no explicit parent) resolves to /tmp and is unusable under the
    unit. Every capture file in this library goes through `scratch_file`; this pins that, so
    a third instance of deploy#613 cannot be added by hand.
    """
    code = [
        ln
        for ln in LIB.read_text().splitlines()
        if not ln.lstrip().startswith("#") and "mktemp" in ln
    ]
    offenders = [ln.strip() for ln in code if "-p " not in ln]
    assert not offenders, (
        "bare mktemp in compose_project.sh — it defaults to /tmp, which is READ-ONLY under "
        "isnad-backup.service (ProtectSystem=strict). This is deploy#613, which has now "
        "shipped twice. Use scratch_file().\n  " + "\n  ".join(offenders)
    )
