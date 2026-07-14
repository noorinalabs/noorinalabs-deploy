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

BOOTSTRAP = SCRIPTS_DIR / "bootstrap-vps.sh"
PROD_COMPOSE = REPO_ROOT / "compose" / "docker-compose.prod.yml"

# The scripts that talk to the LIVE stack. restore_rehearsal.sh is in scope because it
# drives restore.sh: it is the one caller that must NOT resolve to `noorinalabs`.
SCANNED = (LIB, BACKUP, RESTORE, REHEARSAL)


# The whole EXECUTABLE surface that can address the prod stack (deploy#618 review). The
# original scan stopped at the four files above, and the class was still live in three
# places it could not see: `bootstrap-vps.sh` PRINTED an unflagged `up -d` as the repo's own
# fresh-VPS bring-up instruction, and deploy-prod.yml / deploy-isnad-graph.yml each ran an
# unflagged `logs --tail=50 api` in their api_health FAILURE branch — so on a failed PROD
# deploy the one diagnostic the operator got was EMPTY.
#
# Docs are deliberately NOT scanned. ~40 lines under docs/runbooks/** run
# `docker compose -f compose/docker-compose.prod.yml exec|logs|ps …` by hand, and the fix for
# those is not a flag in each one — it is `name: noorinalabs` in the compose file itself,
# which makes the DERIVED project correct for every caller, including an operator typing one
# of them at 3am. test_the_compose_file_pins_the_project_name pins that.
def _executable_surface() -> list[Path]:
    paths = sorted(SCRIPTS_DIR.glob("*.sh"))
    paths += sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    paths += sorted((REPO_ROOT / ".github" / "actions").rglob("*.yml"))
    paths.append(REPO_ROOT / ".pre-commit-config.yaml")
    return [p for p in paths if p.is_file()]


# Subcommands that do NOT resolve a project, so a missing `-p` cannot misaddress anything.
# `config` parses the file; `version` asks the binary; `ls` ENUMERATES projects rather than
# selecting one (it is what an operator runs to find out which project the stack is actually
# in, and compose_project.sh's own diagnostic now tells them to). Everything else — ps, exec,
# logs, up, stop, start, pull, run, cp, restart, down — operates on one project's containers.
#
# The property being enforced is not "a `-p` appears on every line". It is "no compose call
# can resolve the WRONG project". These three cannot resolve one at all.
PROJECT_AGNOSTIC_SUBCOMMANDS = {"config", "version", "ls"}

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
                # Strip trailing shell/prose punctuation. A `docker compose ls` quoted INSIDE a
                # diagnostic string ("Run 'docker compose ls' and ...") tokenizes as `ls'`, and
                # an un-normalized token would miss the project-agnostic exemption and flag the
                # message as if it were a call. A real invocation's subcommand never carries
                # quotes, so this cannot mask one.
                subcommand = token.strip("\"'`;.,)")
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
            if subcommand in PROJECT_AGNOSTIC_SUBCOMMANDS:
                continue
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


def test_the_compose_file_pins_the_project_name() -> None:
    """The single source of truth, and the only fix that reaches the runbooks.

    Compose precedence: `-p` > COMPOSE_PROJECT_NAME > compose-file `name:` > the file's
    DIRECTORY. Without `name:`, that last rung applied — and this file lives in `compose/`,
    so every caller passing `-f compose/docker-compose.prod.yml` with no `-p` addressed a
    project literally called `compose`, which nothing ever deployed into. `name:` makes the
    DERIVED project correct by construction for every caller the scan cannot reach: the ~40
    hand-run `docker compose -f compose/docker-compose.prod.yml exec|logs|ps` lines in
    docs/runbooks/**, and the operator typing one of them mid-incident.
    """
    text = PROD_COMPOSE.read_text()
    assert re.search(rf"^name:\s*{CANONICAL_PROJECT}\s*$", text, re.MULTILINE), (
        f"compose/docker-compose.prod.yml has no top-level `name: {CANONICAL_PROJECT}` — the "
        f"project a caller without `-p` derives falls back to the file's DIRECTORY "
        f"(`compose`), which is deploy#617"
    )


def test_no_unflagged_project_scoped_call_against_the_prod_stack() -> None:
    """The sweep. Every compose call naming the prod stack file, anywhere in the executable
    surface, must carry `-p` — `config`/`version` excepted, since they resolve no project.

    FAILS on origin/main in three places the original 4-file scan could not see:
    bootstrap-vps.sh's printed `pull`/`up -d`, and the api_health `logs` in deploy-prod.yml
    and deploy-isnad-graph.yml.
    """
    offenders = []
    for path in _executable_surface():
        for line, flags, subcommand in _compose_invocations(path):
            if "docker-compose.prod.yml" not in line:
                continue
            if subcommand in PROJECT_AGNOSTIC_SUBCOMMANDS:
                continue
            if PROJECT_FLAGS & set(flags):
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {line}  (subcommand={subcommand})")

    assert not offenders, (
        "project-scoped `docker compose` call(s) against the prod stack with no `-p` — each "
        "addresses the project derived from the compose file's DIRECTORY (`compose`), not "
        "the one the stack runs in (deploy#617):\n  " + "\n  ".join(offenders)
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
    """The rehearsal names the project it drives restore.sh against — defence in depth.

    An earlier version of this docstring claimed that WITHOUT this line the rehearsal would
    restore into the real `noorinalabs` stack. That is FALSE, and compose_project.sh disproves
    it: `COMPOSE_PROJECT="${COMPOSE_PROJECT:-${COMPOSE_PROJECT_NAME:-noorinalabs}}"`, and
    run_restore() also exports COMPOSE_PROJECT_NAME, so the fallback still resolves to the
    rehearsal project (deploy#618 review, Weronika Zielinska).

    What this pins is real but narrower: the rehearsal must not depend on ANOTHER file's
    fallback rung — a detail of compose_project.sh, free to change — to stay aimed at the
    scratch stack. guard()'s refusal of an inherited project name is the actual protection,
    and test_the_rehearsal_refuses_an_inherited_project pins that.
    """
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
        "assert_stack_present postgres user-postgres",
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
    for service in ("postgres", "user-postgres"):
        assert service in run.output


def test_stack_presence_guard_passes_against_the_project_the_stack_runs_in(
    tmp_path: Path,
) -> None:
    """The positive control. A guard that only ever refuses proves nothing: it must
    SEPARATE the two classes, not merely go red on one of them."""
    run = _run_with_fake_docker(
        "assert_stack_present postgres user-postgres",
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


def test_the_rehearsal_refuses_an_inherited_project() -> None:
    """guard() is the ACTUAL protection against a wrongly-aimed rehearsal (deploy#618 review).

    The explicit COMPOSE_PROJECT in run_restore() is defence in depth; this refusal is what
    stops an ambient project name from steering a `--overwrite-destination` Neo4j load at a
    stack the rehearsal does not own. It is the exact shape of the file's existing
    COMPOSE_FILE refusal, which exists for the same reason one layer up.
    """
    code = "\n".join(
        ln for ln in REHEARSAL.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    for var in ("COMPOSE_PROJECT", "COMPOSE_PROJECT_NAME"):
        assert var in code, f"restore_rehearsal.sh does not guard an inherited {var}"
    assert "Refusing to run with an inherited ${var}" in code, (
        "restore_rehearsal.sh's guard() does not refuse an inherited project name — an "
        "ambient COMPOSE_PROJECT could aim the rehearsal's restores at another stack"
    )


# --------------------------------------------------------------------------
# End-to-end: the partial-backup contract (deploy#618 review)
# --------------------------------------------------------------------------
# The first version of this PR gated backup.sh on `postgres user-postgres neo4j` — all three
# running — and that SILENTLY REPEALED the script's oldest contract, 270 lines below the
# gate: upload what you got, attest `complete=false`, exit non-zero. "A partial backup beats
# none." On any night Neo4j was down, the gate would have taken ZERO backups instead of two,
# and one of the two it discarded is `user-postgres` — the only store here that no artifact
# can rebuild (deploy#559). backup.sh can even leave Neo4j stopped ITSELF (the neo4j_start
# failure branch), so one bad night would have disarmed every backup after it.
#
# These tests run the REAL backup.sh, under its real `set -euo pipefail`, with docker and
# rclone stubbed — the same shape as test_b2_preflight.py's end-to-end harness, and for the
# same reason: a harness running production code under a weaker `set -...` is not running
# production code. The fake rclone COPIES the staging directory to an inspectable path, so
# the assertions are made against WHAT WAS ACTUALLY UPLOADED — not against a log line
# claiming it was.

FAKE_DOCKER_E2E = r"""#!/usr/bin/env bash
set -u
REAL_PROJECT="noorinalabs"

printf '%s\n' "$*" >> "$DOCKER_ARGV_LOG"

# --- bare `docker` verbs (NOT `docker compose`) -----------------------------------------
# The Neo4j leg resolves its data volume with `docker inspect` and dumps with a bare
# `docker run` (not `compose run`). Without these the leg can never SUCCEED here: NEO4J_OK
# stays false, the partial gate fires, and the run exits non-zero for a reason unrelated to
# the branch under test. A test that goes green because a DIFFERENT gate fired has measured
# nothing (deploy#574) — and the graph-down test needs a COMPLETE artifact before the
# restart fails.
case "${1:-}" in
    inspect)
        printf '%s_neo4j_data\n' "$REAL_PROJECT"
        exit 0
        ;;
    run)
        # `neo4j-admin database dump --to-path=/backups/` writes /backups/neo4j.dump; the
        # host side of that bind mount is $LOCAL_BACKUP_PATH.
        dest=""
        while [[ $# -gt 0 ]]; do
            if [[ "$1" == "-v" && "${2:-}" == */backups ]]; then
                dest="${2%%:*}"
            fi
            shift
        done
        [[ -n "$dest" ]] || exit 1
        { printf 'fake-neo4j-dump\n'; head -c 2048 /dev/zero | tr '\0' 'n'; } > "${dest}/neo4j.dump"
        exit 0
        ;;
esac

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
# No -p: Compose derives the project from the compose file's directory.
[[ -n "$project" ]] || project="compose"

sub="${1:-}"
shift || true

# Anything addressed to a project we never deployed into is EMPTY — and `ps` says so with a
# ZERO, which is the whole of deploy#617.
[[ "$project" == "$REAL_PROJECT" ]] || {
    case "$sub" in
        ps) exit 0 ;;
        *)  echo "no such service in project ${project}" >&2; exit 1 ;;
    esac
}

case "$sub" in
    ps)
        # `-aq <svc>` asks for CONTAINER IDS including STOPPED ones (that is the `-a`) — it is
        # how backup.sh resolves the data volume AFTER stopping the graph. It must keep
        # answering once neo4j is stopped, or the volume is unresolvable, the leg dies in the
        # "refusing to guess" branch, and the test exercises the wrong code path entirely.
        if [[ "$*" == *-aq* ]]; then
            svc=""
            for a in "$@"; do case "$a" in -*) ;; *) svc="$a" ;; esac; done
            for s in $FAKE_RUNNING; do
                [[ "$s" == "$svc" ]] && { printf 'fake-cid-%s\n' "$svc"; exit 0; }
            done
            exit 0   # not in this project — empty, exactly like the real thing
        fi

        # Container state must be REAL state, not a constant. A service that has been STOPPED
        # must stop reporting itself running, or backup.sh's stop-wait loop spins to its 30s
        # timeout, SKIPS the dump, and exits down the stop-timeout path — never reaching the
        # restart branch under test.
        want_health=0
        [[ "$*" == *Health* ]] && want_health=1

        for s in $FAKE_RUNNING; do
            if [[ -f "${DOCKER_STOPPED_FLAG}.${s}" ]]; then
                [[ "$want_health" == "1" ]] || printf '%s:exited\n' "$s"
            elif [[ "$want_health" == "1" ]]; then
                printf '%s:healthy\n' "$s"
            else
                printf '%s:running\n' "$s"
            fi
        done
        exit 0
        ;;
    exec)
        while [[ $# -gt 0 && "$1" == -* ]]; do shift; done
        svc="${1:-}"
        for s in $FAKE_RUNNING; do
            if [[ "$s" == "$svc" ]]; then
                # A plausible, non-empty pg_dump. backup.sh rejects a zero-byte dump.
                printf 'PGDMP-fake-dump-of-%s\n' "$svc"
                head -c 2048 /dev/zero | tr '\0' 'x'
                exit 0
            fi
        done
        echo "service \"${svc}\" is not running" >&2
        exit 1
        ;;
    stop)
        for a in "$@"; do case "$a" in -*) ;; *) : > "${DOCKER_STOPPED_FLAG}.${a}" ;; esac; done
        exit 0
        ;;
    start)
        # FAKE_START_FAILS models the graph refusing to come back after the dump — a
        # crash-loop, a corrupt store, an OOM. `start` cannot create, so it simply fails: the
        # run has produced a COMPLETE artifact and left production DOWN.
        if [[ "${FAKE_START_FAILS:-0}" == "1" ]]; then
            echo "Error response from daemon: cannot start neo4j" >&2
            exit 1
        fi
        for a in "$@"; do case "$a" in -*) ;; *) rm -f "${DOCKER_STOPPED_FLAG}.${a}" ;; esac; done
        exit 0
        ;;
    up)
        printf 'CREATED %s in project %s\n' "$*" "$project" >> "$DOCKER_CREATED_LOG"
        exit 0
        ;;
    *) exit 0 ;;
esac
"""

# Enough rclone to drive the B2 preflight to verdict=OK and to capture the upload.
FAKE_RCLONE_E2E = r"""#!/usr/bin/env bash
set -u
case "${1:-}" in
    lsd)
        # b2_preflight greps the LAST field of each line for $B2_BUCKET.
        printf '          -1 2026-01-01 00:00:00        -1 %s\n' "$B2_BUCKET"
        exit 0
        ;;
    copyto|deletefile|purge) exit 0 ;;
    copy)
        # $2 = local staging dir, $3 = remote. Capture what would have been uploaded —
        # backup.sh's EXIT trap deletes the staging dir, so this is the only chance to see it.
        mkdir -p "$UPLOAD_DIR"
        cp -a "$2"/. "$UPLOAD_DIR"/ 2>/dev/null || true
        exit 0
        ;;
    lsf) exit 0 ;;
    *) exit 0 ;;
esac
"""


class BackupRun:
    def __init__(
        self,
        proc: subprocess.CompletedProcess[str],
        upload: Path,
        argv: str,
        created: str,
        textfile_dir: Path,
    ) -> None:
        self.rc = proc.returncode
        self.output = proc.stdout + proc.stderr
        self.upload = upload
        self.argv = argv
        self.created = created
        self.textfile_dir = textfile_dir

    def textfiles(self) -> list[str]:
        """What the run actually left on the box for node-exporter to scrape."""
        if not self.textfile_dir.is_dir():
            return []
        return sorted(f.name for f in self.textfile_dir.iterdir() if f.suffix == ".prom")

    def uploaded(self) -> list[str]:
        if not self.upload.is_dir():
            return []
        return sorted(p.name for p in self.upload.iterdir())

    def manifest(self) -> str:
        return "".join(
            p.read_text()
            for p in self.upload.glob("_backup_manifest-*.txt")
            if self.upload.is_dir()
        )


def _run_backup(
    running: str,
    tmp_path: Path,
    project: str = CANONICAL_PROJECT,
    *,
    start_fails: bool = False,
) -> BackupRun:
    """Run the REAL backup.sh with `running` as the set of services actually up.

    start_fails — `docker compose start neo4j` returns 1: the graph does not come back after
    the dump. Every dump still succeeds, so the ARTIFACT is complete and the HOST is broken.
    """
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "docker").write_text(FAKE_DOCKER_E2E)
    (stub / "rclone").write_text(FAKE_RCLONE_E2E)
    # backup.sh's command preflight requires zstd on PATH before it does anything else. It is
    # only ever INVOKED on the Neo4j leg, which neither of these scenarios reaches — but the
    # preflight does not know that, and a missing binary would abort the run before the gate
    # under test ever fired, i.e. the test would pass or fail for a reason unrelated to what
    # it asserts.
    (stub / "zstd").write_text("#!/usr/bin/env bash\nexit 0\n")
    for f in stub.iterdir():
        f.chmod(0o755)

    upload = tmp_path / "uploaded"
    argv_log = tmp_path / "argv.log"
    created_log = tmp_path / "created.log"
    argv_log.write_text("")

    env = dict(os.environ)
    env.pop("COMPOSE_PROJECT_NAME", None)
    env["PATH"] = f"{stub}:{env['PATH']}"
    env.update(
        B2_KEY_ID="fake-key-id",
        B2_APP_KEY="fake-app-key",
        B2_BUCKET="noorinalabs-backups",
        BACKUP_DIR=str(tmp_path / "backups"),
        COMPOSE_FILE="compose/docker-compose.prod.yml",
        COMPOSE_PROJECT=project,
        FAKE_RUNNING=running,
        UPLOAD_DIR=str(upload),
        DOCKER_ARGV_LOG=str(argv_log),
        DOCKER_CREATED_LOG=str(created_log),
        FAKE_START_FAILS="1" if start_fails else "0",
        # Container state must be real state: `stop` writes a marker here and `ps` reads it.
        DOCKER_STOPPED_FLAG=str(tmp_path / "stopped"),
        # Where the run leaves its metrics. backup.sh `rm -f`s the FAILURE textfile from here
        # when it emits the success gauge — which is how "the backup that took the graph down
        # cleared its own failure marker" happened. Point it somewhere inspectable so the
        # assertions read what the run ACTUALLY left on the box, not a log line.
        TEXTFILE_DIR=str(tmp_path / "textfile"),
    )

    proc = subprocess.run(
        ["bash", str(BACKUP)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=180,
    )
    return BackupRun(
        proc,
        upload,
        argv_log.read_text(),
        created_log.read_text() if created_log.exists() else "",
        tmp_path / "textfile",
    )


def test_neo4j_down_still_backs_up_both_postgres_stores(tmp_path: Path) -> None:
    """THE PARTIAL-BACKUP CONTRACT. A Neo4j outage must cost the Neo4j leg, not the run.

    `user-postgres` holds accounts, sessions and audit_log, and no pipeline artifact can
    rebuild it. An all-three presence gate would have thrown it away on every night the graph
    was down — including a night backup.sh's own failed restart caused.
    """
    run = _run_backup("postgres user-postgres", tmp_path)
    uploaded = run.uploaded()

    pg = [f for f in uploaded if f.startswith("isnad-pg-") and f.endswith(".dump")]
    userpg = [f for f in uploaded if f.startswith("isnad-userpg-") and f.endswith(".dump")]
    neo = [f for f in uploaded if f.startswith("isnad-neo4j-")]

    assert pg, f"the isnad Postgres dump was NOT uploaded — a partial backup beats none: {uploaded}"
    assert userpg, (
        f"the user-postgres dump was NOT uploaded. It holds accounts, sessions and audit_log, "
        f"and NOTHING can rebuild it (deploy#559) — a Neo4j outage must not discard it: {uploaded}"
    )
    assert not neo, f"a Neo4j dump appeared with Neo4j down: {uploaded}"

    # Both dumps are real bytes, not empty files that happen to exist.
    for name in pg + userpg:
        assert (run.upload / name).stat().st_size > 0, f"{name} uploaded EMPTY"

    assert "complete=false" in run.manifest(), (
        f"the run must ATTEST its incompleteness — restore.sh refuses a partial artifact "
        f"without --allow-partial, and that refusal is driven by this manifest: "
        f"{run.manifest()!r}"
    )
    assert "SKIPPING the Neo4j dump" in run.output
    assert run.rc != 0, "a partial backup is a FAILED backup: it must exit non-zero so the "
    "systemd OnFailure marker fires and the success gauge is not advanced (deploy#565)"

    # And it must not have conjured the database it could not find.
    assert run.created == "", f"backup.sh CREATED a container: {run.created!r}"


def test_a_stray_neo4j_alone_does_not_satisfy_the_project_gate(tmp_path: Path) -> None:
    """The state stg was ACTUALLY found in — and why "refuse only if ZERO services resolve"
    is not the fix.

    Project `compose` held a stray, running `neo4j` (created by backup.sh's own `up`) and no
    Postgres at all. A zero-services rule would have passed that: one of three matched. The
    backup would then have dumped the stray, EMPTY graph, checksummed it, uploaded it, and
    reported success — the silently-empty success this whole change exists to prevent.

    The Postgres pair is the witness precisely because nothing in this path can create it.
    """
    run = _run_backup("neo4j", tmp_path)

    assert run.rc != 0, "a project holding only a stray neo4j must not be backed up"
    assert "Refusing to back up a stack that is not present" in run.output
    assert run.uploaded() == [], (
        f"something was uploaded from a project with no Postgres in it: {run.uploaded()}"
    )
    # The gate must fire BEFORE the dumps, not after they fail.
    assert "pg_dump" not in run.argv, (
        "a dump was attempted against a project the gate should have refused outright"
    )
    assert "Stopping Neo4j" not in run.output, (
        "the stray, empty graph was about to be dumped — this is exactly the artifact that "
        "would have checksummed cleanly and restored as an empty database"
    )


# --------------------------------------------------------------------------
# The producer must not lie about the HOST (deploy#618 review — Aisha Idrissi)
# --------------------------------------------------------------------------


def test_a_run_that_leaves_the_graph_down_does_not_report_success(tmp_path: Path) -> None:
    """Every dump succeeds; then Neo4j does not come back. This run took production down.

    Before NEO4J_DOWN, that run exited **0**. NEO4J_OK was set true at the dump; the
    restart-failure branch logged "the graph is DOWN" and set nothing; the final gate read
    only the three _OK flags. So systemd marked isnad-backup.service SUCCEEDED,
    `OnFailure=isnad-backup-failure-marker.service` never fired, and emit_success_metric's
    `rm -f "$FAILURE_TEXTFILE"` meant THE BACKUP THAT TOOK THE GRAPH DOWN CLEARED ITS OWN
    FAILURE MARKER. The operator's only surviving signal was a generic ServiceDown page with
    nothing tying it to the backup.

    The correct behaviour is NOT simply "fail". Two facts are true at once and BOTH must
    reach the box:

      * the backup IS complete and restorable -> emit the success gauge. Suppressing it would
        hide a good artifact and make BackupStale fire on a backup that exists — the
        alert-fatigue trap deploy#559/#565 closed.
      * the run DID break production -> exit non-zero, so OnFailure fires and a human comes.
    """
    run = _run_backup("postgres user-postgres neo4j", tmp_path, start_fails=True)
    uploaded = run.uploaded()

    # The artifact is COMPLETE: all three stores dumped and uploaded.
    assert any(f.startswith("isnad-pg-") for f in uploaded), f"no isnad dump uploaded: {uploaded}"
    assert any(f.startswith("isnad-userpg-") for f in uploaded), (
        f"no user-postgres dump uploaded: {uploaded}"
    )
    assert any("neo4j" in f for f in uploaded), (
        f"no neo4j dump uploaded: {uploaded} — the fixture must reach a COMPLETE artifact, or "
        "this test passes off the partial gate and proves nothing about the restart branch"
    )
    assert "complete=true" in run.manifest(), (
        "every dump succeeded, so the artifact IS complete and the manifest must say so"
    )

    # ...and it is ATTESTED: a good backup must not go invisible.
    assert "isnad_backup_success.prom" in run.textfiles(), (
        "the success gauge was NOT emitted for a COMPLETE backup. Suppressing it hides a good "
        "artifact and makes BackupStale fire falsely (deploy#559/#565). 'the artifact is good' "
        "and 'the host is broken' are different facts and need different signals."
    )

    # ...and the run FAILS, because it left production down.
    assert run.rc != 0, (
        "a run that left the graph DOWN exited 0. systemd marks the unit SUCCEEDED, OnFailure= "
        "never fires the failure marker, and nobody is told — while the script's own log says "
        "'the graph is DOWN'."
    )
    assert "the graph is DOWN" in run.output


def test_the_health_timeout_branch_also_raises_the_graph_down_flag() -> None:
    """The same structural hole, one state further on.

    `start` succeeds but the graph never becomes HEALTHY within MAX_HEALTH_WAIT. That branch
    logged a WARNING and walked straight into a green exit. A graph that never came back
    healthy is a graph that is down.

    Pinned separately because it is a DIFFERENT branch from the restart failure — and the
    first fixup pass closed one and left the other open, which is exactly how this class of
    bug survives its own fix.
    """
    src = BACKUP.read_text()
    assert "NEO4J_DOWN=true" in src, "the graph-down flag is gone entirely"

    before, _, after = src.partition("did not become healthy")
    assert after, "the health-timeout branch vanished — retarget this test, do not delete it"
    window = before[-500:] + after[:300]
    assert "NEO4J_DOWN=true" in window, (
        "the health-timeout branch does not raise NEO4J_DOWN. A graph that never came back "
        "healthy is DOWN, and this branch would exit 0 over it — the same hole as the "
        "restart-failure branch, which is why both are pinned."
    )
