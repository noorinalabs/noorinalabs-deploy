"""Every compose call in the backup/restore path must name its PROJECT (deploy#617).

``docker compose -f compose/docker-compose.prod.yml <cmd>`` does not address the running
stack. ``-f`` names the FILE; with no ``-p`` and no ``COMPOSE_PROJECT_NAME``, Compose
derives the project name from the compose file's DIRECTORY — ``compose`` — while every
deploy path in this repo brings the stack up as ``-p noorinalabs``. So every call in
``backup.sh`` and ``restore.sh`` resolved a project that existed only as a name and held
nothing. Measured on stg 2026-07-13, on the first run ever to get past the B2 preflight:
both pg_dumps failed, ``stop neo4j`` stopped nothing, and ``up -d neo4j`` **created a new,
empty Neo4j with fresh volumes.**

WHY A COMMAND-STRING TEST WOULD PROVE NOTHING HERE
--------------------------------------------------
Stub ``docker``, assert the script issues ``pg_dump -U isnad -d isnad_graph --format=custom``,
go green. That test passes on the *broken* script: the command string is identical either
way. The bug is not in the command — it is in **which project the command is addressed
to**, and that is invisible to any assertion about the command's text.

So this module tests the two things that actually distinguish the trees:

1. a **static scan** — every ``docker compose`` invocation in the backup/restore path
   carries a project flag, and an unflagged one added later fails the suite;
2. a **behavioural** check against a fake ``docker`` that models the one fact that matters:
   a compose project you never deployed into is EMPTY, and ``ps`` against it EXITS 0.
   That vacuous zero is what the replaced guard read as "the stack is fine", and it is why
   ``test_the_replaced_ps_guard_cannot_tell_an_empty_project_from_a_healthy_one`` exists —
   the fixture must be shown capable of expressing the defect before its silence about the
   fixed code means anything (cf. ``feedback_calibrate_the_mutation_before_counting_it``).

Both classes are exercised in BOTH directions: the guard must fail on the phantom project
AND pass on the real one, and the stub must be shown able to record a container creation
before an empty creation-log is read as "nothing was created".
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
LIB = SCRIPTS_DIR / "compose_project.sh"
BACKUP = SCRIPTS_DIR / "backup.sh"
RESTORE = SCRIPTS_DIR / "restore.sh"
REHEARSAL = SCRIPTS_DIR / "restore_rehearsal.sh"

# The scripts that talk to the LIVE stack. restore_rehearsal.sh is in scope because it
# drives restore.sh: it is the one caller that must NOT resolve to `noorinalabs`.
SCANNED = (LIB, BACKUP, RESTORE, REHEARSAL)

PROJECT_FLAGS = {"-p", "--project-name"}
# Compose top-level flags that consume the following token as their value.
VALUE_FLAGS = {
    "-p",
    "--project-name",
    "-f",
    "--file",
    "--env-file",
    "--profile",
    "--project-directory",
    "--progress",
    "--ansi",
    "--parallel",
}

# The project the stack is actually deployed into, by every workflow and action in the repo.
CANONICAL_PROJECT = "noorinalabs"

# Matches:  COMPOSE_PROJECT="${COMPOSE_PROJECT:-${COMPOSE_PROJECT_NAME:-noorinalabs}}"
_PROJECT_DEFAULT_RE = re.compile(
    r'^COMPOSE_PROJECT="\$\{COMPOSE_PROJECT:-(?:\$\{COMPOSE_PROJECT_NAME:-)?(?P<default>[A-Za-z0-9_.-]+)',
    re.MULTILINE,
)


def _code_lines(script: Path) -> list[str]:
    """Logical shell lines with comments dropped and backslash-continuations joined.

    The fixes in these files quote the old, buggy commands verbatim so the next reader
    knows what was wrong. A raw substring scan would match the explanation instead of the
    executable line and fire on its own documentation — the same trap
    ``test_restore_failure_modes.py`` documents. Comment lines go first, then continuations
    are joined so a call split across lines is scanned as one invocation.
    """
    joined = re.sub(r"\\\n\s*", " ", script.read_text())
    return [ln for ln in joined.splitlines() if not ln.lstrip().startswith("#")]


def _compose_invocations(script: Path) -> list[tuple[str, list[str], str | None]]:
    """Every literal ``docker compose`` call: (line, top-level flags, subcommand).

    A script that is not there contributes nothing rather than raising. Its ABSENCE is a
    separate assertion (``test_the_project_library_exists``) — folded in here, a missing
    ``compose_project.sh`` would make the scan below die on a FileNotFoundError, i.e. go red
    for a reason unrelated to what it tests, and its red would stop meaning "an unflagged
    compose call exists".
    """
    if not script.is_file():
        return []
    found = []
    for line in _code_lines(script):
        idx = line.find("docker compose")
        if idx == -1:
            continue
        rest = line[idx + len("docker compose") :]
        try:
            tokens = shlex.split(rest)
        except ValueError:  # unbalanced quotes across a redirect; fall back to whitespace
            tokens = rest.split()

        flags: list[str] = []
        subcommand: str | None = None
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if not token.startswith("-"):
                subcommand = token
                break
            flags.append(token)
            i += 2 if token in VALUE_FLAGS else 1
        found.append((line.strip(), flags, subcommand))
    return found


# --------------------------------------------------------------------------
# Static scan: the project flag is not optional
# --------------------------------------------------------------------------


def test_the_project_library_exists() -> None:
    """backup.sh and restore.sh both source it and refuse to run without it."""
    assert LIB.is_file(), f"scripts/compose_project.sh is missing (expected at {LIB})"


def test_every_docker_compose_invocation_names_a_project() -> None:
    """The regression guard. FAILS on origin/main: 8 unflagged calls in backup.sh, 11 in
    restore.sh. An unflagged call added tomorrow fails here rather than on staging."""
    unflagged = []
    for script in SCANNED:
        for line, flags, subcommand in _compose_invocations(script):
            if not PROJECT_FLAGS & set(flags):
                unflagged.append(f"{script.name}: {line}  (subcommand={subcommand})")

    assert not unflagged, (
        "docker compose invocation(s) with no project flag — each one addresses the project "
        "Compose infers from the compose file's DIRECTORY (`compose`), not the project the "
        "stack runs in (`noorinalabs`), and therefore a project with nothing in it "
        "(deploy#617):\n  " + "\n  ".join(unflagged)
    )


def test_backup_and_restore_route_every_compose_call_through_the_library() -> None:
    """Stronger than the scan above, and the reason it can stay simple: the ONLY literal
    ``docker compose`` in the backup/restore path lives in ``compose_project.sh``'s dc()."""
    for script in (BACKUP, RESTORE):
        literals = [line for line, _, _ in _compose_invocations(script)]
        assert not literals, (
            f"{script.name} calls `docker compose` directly instead of going through dc() "
            f"in scripts/compose_project.sh — the project flag is then one edit away from "
            f"being forgotten again (deploy#617):\n  " + "\n  ".join(literals)
        )


def test_the_library_defaults_to_the_project_the_stack_is_deployed_into() -> None:
    match = _PROJECT_DEFAULT_RE.search(LIB.read_text())
    assert match is not None, "no COMPOSE_PROJECT default assignment found in compose_project.sh"
    assert match.group("default") == CANONICAL_PROJECT, (
        f"COMPOSE_PROJECT defaults to {match.group('default')!r}, but every deploy path in "
        f"this repo brings the stack up as `-p {CANONICAL_PROJECT}` — a backup or restore "
        f"would address a project with no containers in it"
    )


def test_the_canonical_project_matches_what_the_deploy_path_actually_uses() -> None:
    """Anchor the default in the deploy path itself, so a rename of the project on the
    deploy side cannot leave backup/restore quietly pointing at the old one."""
    action = REPO_ROOT / ".github" / "actions" / "write-deploy-env" / "action.yml"
    assert f"docker compose -p {CANONICAL_PROJECT}" in action.read_text(), (
        f"the deploy action no longer brings the stack up as `-p {CANONICAL_PROJECT}` — "
        f"scripts/compose_project.sh's default is now aimed at a project nothing deploys into"
    )


def test_the_backup_and_restore_path_never_uses_compose_up() -> None:
    """`up` is a CONVERGE verb: asked for a service the project does not have, it does not
    fail — it CREATES one, with fresh, empty volumes. That is how the stg run left a stray
    neo4j and a stray, empty `compose_neo4j_data`. In a RESTORE path the invented container
    is the one whose volume the next command loads a dump into. The capability is removed
    from these scripts, not merely aimed correctly."""
    offenders = []
    for script in (LIB, BACKUP, RESTORE):
        for line, _, subcommand in _compose_invocations(script):
            if subcommand == "up":
                offenders.append(f"{script.name}: {line}")
        # dc()-routed calls are invisible to the scan above; catch those too.
        for line in _code_lines(script):
            if re.search(r"(?<![\w-])dc\s+up(\s|$)", line):
                offenders.append(f"{script.name}: {line.strip()}")

    assert not offenders, (
        "compose `up` in the backup/restore path — it can CREATE a database that was not "
        "there (deploy#617). Use `start`, which can only start an existing container and "
        "fails loudly when there is none:\n  " + "\n  ".join(offenders)
    )


def test_the_rehearsal_names_its_own_project_when_driving_restore() -> None:
    """restore.sh now passes an explicit `-p`, and a FLAG BEATS COMPOSE_PROJECT_NAME. If the
    rehearsal does not set COMPOSE_PROJECT, every restore it runs — including the
    `--overwrite-destination` Neo4j load — lands on `noorinalabs`, THE REAL STACK, while
    assert_volume_isolation goes on inspecting the rehearsal project and reports all-clear."""
    body = re.search(
        r"^run_restore\(\) \{\n(.*?)^\}", REHEARSAL.read_text(), re.MULTILINE | re.DOTALL
    )
    assert body is not None, "run_restore() not found in restore_rehearsal.sh"
    code = "\n".join(ln for ln in body.group(1).splitlines() if not ln.lstrip().startswith("#"))
    assert 'COMPOSE_PROJECT="$PROJECT"' in code, (
        "restore_rehearsal.sh does not pass COMPOSE_PROJECT to restore.sh — the rehearsal "
        "would drive the REAL `noorinalabs` stack (deploy#617)"
    )


# --------------------------------------------------------------------------
# Behavioural: a fake docker that models compose's project scoping
# --------------------------------------------------------------------------

# The ONE fact this stub exists to model: a compose project you never deployed into is
# EMPTY, and asking about it is not an error. Everything the bug did follows from that.
FAKE_DOCKER = r"""#!/usr/bin/env bash
set -u
REAL_PROJECT="noorinalabs"
REAL_SERVICES="postgres user-postgres neo4j"

printf '%s\n' "$*" >> "$DOCKER_ARGV_LOG"

[[ "${1:-}" == "compose" ]] || exit 0
shift

project=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--project-name) project="$2"; shift 2 ;;
        -f|--file|--env-file|--profile) shift 2 ;;
        -*) shift ;;
        *) break ;;
    esac
done

# No -p: Compose derives the project from the compose file's DIRECTORY. Ours lives in
# compose/, so an unflagged call lands on the project `compose`.
[[ -n "$project" ]] || project="compose"

sub="${1:-}"
shift || true

case "$sub" in
    ps)
        # THE VACUOUS ZERO. An empty or entirely nonexistent project lists nothing and
        # EXITS 0. `ps` answers "did ps run", not "is the stack there".
        [[ "$project" == "$REAL_PROJECT" ]] || exit 0
        for s in $REAL_SERVICES; do
            if [[ "$*" == *"{{.Health}}"* ]]; then
                printf '%s:healthy\n' "$s"
            else
                printf '%s:running\n' "$s"
            fi
        done
        exit 0
        ;;
    start)
        # Starts an EXISTING container. Fails loudly when there is none.
        [[ "$project" == "$REAL_PROJECT" ]] && exit 0
        echo "no container to start: neo4j" >&2
        exit 1
        ;;
    up)
        # CONVERGE. Does not fail on a project with no containers — it CREATES them.
        printf 'CREATED neo4j in project %s\n' "$project" >> "$DOCKER_CREATED_LOG"
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
"""


class LibRun:
    def __init__(self, proc: subprocess.CompletedProcess[str], argv: str, created: str) -> None:
        self.rc = proc.returncode
        self.output = proc.stdout + proc.stderr
        self.argv = argv
        self.created = created


def _run_with_fake_docker(
    snippet: str, project: str, tmp_path: Path, source_lib: bool = True
) -> LibRun:
    """Run ``snippet`` with the fake docker on PATH (and, by default, the library sourced).

    ``source_lib=False`` is for the two FIXTURE-CALIBRATION tests, and it is not a
    convenience. They must demonstrate that the stub reproduces the defect *on the tree that
    has the defect* — where ``compose_project.sh`` does not exist. Sourcing it there would
    make them die on a missing file, i.e. fail for a reason that has nothing to do with what
    they assert, and a control that fails for the wrong reason has stopped controlling
    anything (the lesson ``restore_rehearsal.sh``'s ``expect_fail`` third argument exists to
    enforce).

    The snippet runs under the same ``set -euo pipefail`` the real scripts do: a harness
    running production code under a weaker ``set -...`` is not running production code.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)

    argv_log = tmp_path / "argv.log"
    created_log = tmp_path / "created.log"
    argv_log.write_text("")

    env = dict(os.environ)
    env.pop("COMPOSE_PROJECT_NAME", None)
    env.update(
        PATH=f"{bindir}:{env['PATH']}",
        DOCKER_ARGV_LOG=str(argv_log),
        DOCKER_CREATED_LOG=str(created_log),
        COMPOSE_FILE="compose/docker-compose.prod.yml",
        COMPOSE_PROJECT=project,
    )

    prelude = f'source "{LIB}"' if source_lib else ""
    script = f"""
set -euo pipefail
log() {{ printf '[%s] %s\\n' "$1" "${{*:2}}"; }}
{prelude}
{snippet}
"""
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, cwd=REPO_ROOT
    )
    return LibRun(
        proc,
        argv_log.read_text(),
        created_log.read_text() if created_log.exists() else "",
    )


# --- calibration: prove the fixture can express the defect ---------------------------


def test_the_replaced_ps_guard_cannot_tell_an_empty_project_from_a_healthy_one(
    tmp_path: Path,
) -> None:
    """The instrument check, and the whole diagnosis in one assertion.

    This runs the LITERAL guard that used to stand at backup.sh's preflight, verbatim,
    against a project with no containers — and it EXITS 0. That zero is what waved the run
    through to the dumps. It also calibrates every assertion below it: the fake docker
    demonstrably reproduces the failure, so its silence about the fixed code is a
    measurement and not a stub that cannot fail.
    """
    run = _run_with_fake_docker(
        'docker compose -f "$COMPOSE_FILE" ps --format json &>/dev/null; echo "old_guard_rc=$?"',
        project="compose",
        tmp_path=tmp_path,
        source_lib=False,
    )
    assert "old_guard_rc=0" in run.output, (
        "the fixture does not reproduce the defect — if `ps` on an empty project did NOT "
        "exit 0 here, nothing below this test is calibrated"
    )


def test_compose_up_against_an_empty_project_creates_a_container(tmp_path: Path) -> None:
    """The other half of the calibration. The stub RECORDS a creation when `up` is used, so
    the empty creation-log asserted in the next test is a real negative and not a log
    nothing ever writes to."""
    run = _run_with_fake_docker(
        'docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" up -d neo4j',
        project="compose",
        tmp_path=tmp_path,
        source_lib=False,
    )
    assert run.rc == 0, "`up` on an empty project does not fail — that is the point of it"
    assert "CREATED neo4j in project compose" in run.created


# --- the guards themselves, in both directions ---------------------------------------


def test_stack_presence_guard_refuses_a_project_with_no_containers(tmp_path: Path) -> None:
    """The replacement for the vacuous `ps` guard, against the exact project an unflagged
    call resolves to."""
    run = _run_with_fake_docker(
        "assert_stack_present postgres user-postgres neo4j",
        project="compose",
        tmp_path=tmp_path,
    )
    assert run.rc != 0, (
        "assert_stack_present passed against a project with NO containers — this is the "
        "vacuous zero of the guard it replaces (deploy#617)"
    )
    assert "is not running" in run.output
    assert "compose" in run.output
    # It must name what is missing, not merely refuse: an operator mid-incident reads the
    # last line, and "the stack is not there" and "you asked the wrong project" are
    # different problems with different fixes.
    for service in ("postgres", "user-postgres", "neo4j"):
        assert service in run.output


def test_stack_presence_guard_passes_against_the_project_the_stack_runs_in(
    tmp_path: Path,
) -> None:
    """The positive control. A guard that only ever refuses proves nothing: it must
    SEPARATE the two classes, not merely go red on one of them."""
    run = _run_with_fake_docker(
        "assert_stack_present postgres user-postgres neo4j",
        project=CANONICAL_PROJECT,
        tmp_path=tmp_path,
    )
    assert run.rc == 0, f"assert_stack_present refused the real stack:\n{run.output}"
    assert f"-p {CANONICAL_PROJECT}" in run.argv, (
        f"the compose call did not carry `-p {CANONICAL_PROJECT}`:\n{run.argv}"
    )


def test_neo4j_restart_fails_loudly_rather_than_creating_a_database(tmp_path: Path) -> None:
    """The stray-container behaviour, directly. Against a project with no neo4j, the backup
    path must REFUSE — not conjure one. `up -d neo4j` here created `compose-neo4j-1` and a
    fresh, empty `compose_neo4j_data` on stg."""
    run = _run_with_fake_docker("neo4j_start", project="compose", tmp_path=tmp_path)

    assert run.rc != 0, "neo4j_start reported success against a project with no neo4j container"
    assert run.created == "", (
        f"the backup path CREATED a neo4j container: {run.created!r} — a backup script must "
        f"not be able to bring a database into existence (deploy#617)"
    )
    assert " up " not in f" {run.argv} ", f"an `up` reached docker:\n{run.argv}"
    assert "start neo4j" in run.argv, f"neo4j_start did not issue `start`:\n{run.argv}"


def test_neo4j_restart_starts_the_container_it_stopped_in_the_real_project(
    tmp_path: Path,
) -> None:
    """Positive control for the above: `start` is not merely 'the verb that always fails'."""
    run = _run_with_fake_docker("neo4j_start", project=CANONICAL_PROJECT, tmp_path=tmp_path)
    assert run.rc == 0, f"neo4j_start could not restart the real stack's neo4j:\n{run.output}"
    assert run.created == ""
    assert f"compose -p {CANONICAL_PROJECT}" in run.argv


def test_the_library_addresses_the_project_it_was_given_on_every_call(tmp_path: Path) -> None:
    """dc() puts the project on the wire. Without this the flag is decorative."""
    run = _run_with_fake_docker(
        "dc ps -aq neo4j >/dev/null; dc stop neo4j >/dev/null",
        project=CANONICAL_PROJECT,
        tmp_path=tmp_path,
    )
    assert run.rc == 0
    calls = [ln for ln in run.argv.splitlines() if ln.startswith("compose")]
    assert calls, "no compose calls reached docker"
    for call in calls:
        assert f"-p {CANONICAL_PROJECT}" in call, f"compose call with no project: {call}"
