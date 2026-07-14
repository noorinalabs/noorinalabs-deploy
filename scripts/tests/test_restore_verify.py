"""restore_verify.sh — the guards, and the comparator's ability to go RED (deploy#640).

WHAT THIS MODULE CAN AND CANNOT PROVE
--------------------------------------
It proves the SCRIPT's decision logic: that it refuses the live project, aborts before any
load when the Neo4j volume is not its own, refuses a dirty scratch stack, refuses an INERT
comparator, and — the whole point — that it separates an instrument failure (rc=2) from a
real negative (rc=1).

It cannot prove the CYPHER IS VALID or that it measures anything. No string test can. The
`n.name` defect that made the first hand-run's checksum read 0 on both sides and "agree"
was perfectly valid Cypher — it just addressed a property that does not exist. A unit test
over the query string is blind to that by construction (cf. deploy#606/#607: an
invalid-Cypher precheck passed 32 tests and two reviewers, and only executing it against a
real database caught it).

So the real-engine calibration is `restore_verify.sh --self-test`, run against two actual
Neo4j instances by the `self-test` job in .github/workflows/restore-verify.yml. THAT is the
comparator's calibration. This module is the guard suite around it.

EVERY FIXTURE HERE IS SHOWN CAPABLE OF EXPRESSING THE DEFECT BEFORE ITS SILENCE IS READ AS
PASSING (feedback_calibrate_the_mutation_before_counting_it, deploy#574): each guard test
has a companion that drives the SAME harness to the opposite verdict. A test that can only
ever report "BLOCKED" proves every guard holds, including the ones that do not exist.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
RESTORE_VERIFY = SCRIPTS_DIR / "restore_verify.sh"
RV_COMPOSE = REPO_ROOT / "compose" / "docker-compose.restore-verify.yml"

RC_VERIFIED, RC_FAILED, RC_INSTRUMENT = 0, 1, 2

LIVE_PROJECT = "noorinalabs"
RV_PROJECT = "isnad-restore-verify"


# ---------------------------------------------------------------------------
# Static scans
# ---------------------------------------------------------------------------
def _source() -> str:
    return RESTORE_VERIFY.read_text()


COMPOSE_CALL = re.compile(r"docker\s+compose\b([^\n|;&]*)")


def _unflagged_compose_calls(text: str) -> list[str]:
    """Every `docker compose` in EXECUTABLE code that does not name its project.

    Full-line comments are dropped first — the script documents the very rule this scan
    enforces, and prose about `docker compose` is not a call. Comments are dropped by
    LINE, not by stripping from the first `#`, because `#` is not a comment character
    inside `${NEO4J_AUTH#neo4j/}` and a naive strip would silently truncate the line the
    scan most needs to see. (feedback_a_scan_cannot_see_an_emptied_string, deploy#633:
    keep an orthogonal reading of the source in the loop.)
    """
    code = "\n".join(line for line in text.split("\n") if not line.lstrip().startswith("#"))
    return [m.group(0).strip() for m in COMPOSE_CALL.finditer(code) if "-p " not in m.group(1)]


def test_every_docker_compose_call_names_its_project() -> None:
    """`-f` names the FILE. With no `-p`, Compose derives the project from the compose
    file's DIRECTORY basename — `compose` — and a restore addressed to a project you never
    deployed into WRITES: `neo4j-admin database load --overwrite-destination` creates and
    fills a brand-new volume, reports success, and leaves the real graph untouched. That
    fallback is the whole of deploy#617.
    """
    unflagged = _unflagged_compose_calls(_source())
    assert not unflagged, (
        "docker compose invocation(s) without an explicit -p project:\n  " + "\n  ".join(unflagged)
    )


@pytest.mark.parametrize(
    "evasion",
    [
        "docker compose -f compose/x.yml up -d",
        '    docker compose -f "$RV_COMPOSE" down -v  # trailing comment',
        'dc() { docker compose -f "$F" "$@"; }',
    ],
)
def test_the_scan_can_actually_see_an_unflagged_call(evasion: str) -> None:
    """Calibrate the scan. An oracle that can only ever say "clean" proves every rule holds,
    including the ones it does not implement (feedback_calibrate_the_mutation_before_counting_it).

    The evasions are on BOTH sides of the table: bare, behind an inline comment, and hidden
    inside a wrapper function — the last is how the real defect looked in backup.sh.
    """
    assert _unflagged_compose_calls(evasion), (
        f"the scan cannot see an unflagged `docker compose` in {evasion!r} — it proves nothing"
    )


def test_the_scan_does_not_false_positive_on_a_comment() -> None:
    """The other direction: prose about the rule must not fail the build."""
    assert not _unflagged_compose_calls("# every `docker compose` here carries -p")


def _run_restore_body(text: str) -> str:
    m = re.search(r"^run_restore\(\) \{(.*?)^\}", text, re.S | re.M)
    assert m, "could not locate run_restore() — the scan must not silently find nothing"
    return m.group(1)


HANDOFF_EXPORTS = {
    "COMPOSE_PROJECT": (
        r'COMPOSE_PROJECT="\$(RV_PROJECT|\{RV_PROJECT\})"',
        "restore.sh resolves its own project and falls back to the LIVE one "
        "(compose_project.sh: COMPOSE_PROJECT=${COMPOSE_PROJECT:-...:-noorinalabs})",
    ),
    "COMPOSE_FILE": (
        r'COMPOSE_FILE="\$(RV_COMPOSE|\{RV_COMPOSE\})"',
        "restore.sh:62 defaults COMPOSE_FILE to compose/docker-compose.prod.yml — the second "
        "single point of failure of the same class, pointing at the same live stack",
    ),
}


@pytest.mark.parametrize("var", sorted(HANDOFF_EXPORTS))
def test_run_restore_hands_restore_sh_the_throwaway_target(var) -> None:
    """THE two lines the whole workflow's safety rests on.

    `restore.sh` takes no project or compose-file argument — it RESOLVES ITS OWN, and BOTH
    fallbacks point at the live stack. It is the process that runs `neo4j-admin database load
    --overwrite-destination`. So the ONLY thing between a scheduled restore-verify run and the
    production graph is that run_restore exports BOTH `COMPOSE_PROJECT="$RV_PROJECT"` and
    `COMPOSE_FILE="$RV_COMPOSE"`. Drop either and the workflow can address the live stack, on a
    cron, unattended — and every other guard still passes, because they inspect the stack the
    load will not touch.

    This scan is the same shape as the `-p` scan above, applied to the env handoff instead of
    the compose argv. It exists because deleting one of these lines left the whole suite green.
    (Weronika Zielinska, PR#642 review — both the project and the COMPOSE_FILE sibling.)
    """
    pattern, why = HANDOFF_EXPORTS[var]
    body = _run_restore_body(_source())
    assert re.search(pattern, body), (
        f'run_restore() does not pass {var}="$RV_..." to restore.sh.\n{why}.\n'
        "It overwrites the PRODUCTION GRAPH. Unrecoverable."
    )


@pytest.mark.parametrize("dropped", sorted(HANDOFF_EXPORTS))
def test_the_handoff_scan_can_actually_see_each_missing_export(dropped) -> None:
    """Calibrate the scan against the EXACT mutations that must be caught: delete each of the
    two `RV_` exports from run_restore's var-prefix, one at a time.

    Without this, the scan could be a no-op and its silence would mean nothing — which is
    precisely the state the suite was in when it reported `40 passed` on a tree that would have
    destroyed the production graph.
    """
    lines = {
        "COMPOSE_PROJECT": '    COMPOSE_PROJECT="$RV_PROJECT" \\\n',
        "COMPOSE_FILE": '    COMPOSE_FILE="$RV_COMPOSE" \\\n',
    }
    kept = "".join(v for k, v in lines.items() if k != dropped)
    mutated = (
        "run_restore() {\n"
        "    local rc=0\n"
        f"{kept}"
        '        RESTORE_DIR="$RESTORE_STAGING_DIR" \\\n'
        '        "${SCRIPT_DIR}/restore.sh" --force latest || rc=$?\n'
        "}\n"
    )
    pattern = HANDOFF_EXPORTS[dropped][0]
    body = _run_restore_body(mutated)
    assert not re.search(pattern, body), (
        f"the scan cannot see a run_restore() that DROPPED the {dropped} export — "
        "it would not have caught that overwrite mutation, so it proves nothing"
    )


def test_restore_sh_still_emits_the_volume_line_the_handoff_check_reads() -> None:
    """run_restore verifies the handoff by reading restore.sh's own "Resolved Neo4j data
    volume:" announcement. That string belongs to someone else's file.

    A producer-side rename would not break the parser — it would QUIETLY NARROW it, the guard
    downstream would stop guarding, and the tests would stay green
    (feedback_paraphrase_in_the_product). So pin the anchor against the REAL producer: if
    restore.sh's wording changes, this fails and names the guard that depends on it, rather than
    the guard silently going blind.
    """
    restore_sh = (SCRIPTS_DIR / "restore.sh").read_text()
    assert "Resolved Neo4j data volume:" in restore_sh, (
        "restore.sh no longer emits 'Resolved Neo4j data volume:'. run_restore()'s handoff "
        "verification reads that exact string and will now refuse to certify any restore "
        "(rc=2). Update BOTH, or the guard is dead."
    )


def test_the_measurement_path_never_discards_stderr() -> None:
    """`_run_capture` is the only function in the script that produces a value that is read.

    A real verification script in this project printed `*** DATA LOSS ***` about a perfectly
    intact backup because a permission error was swallowed and `wc -l` turned "could not
    authenticate" into the number 0 (deploy#632/#641). On the failure path the diagnostic IS
    the finding.
    """
    src = _source()
    body = src.split("_run_capture() {", 1)[1].split("\n}", 1)[0]
    assert "2>/dev/null" not in body, "_run_capture discards stderr — the diagnostic IS the finding"
    assert 'while IFS= read -r line; do log "ERROR"' in body, (
        "_run_capture must EMIT the captured stderr on the failure path, not merely avoid "
        "discarding it"
    )


def test_an_empty_reading_is_refused_rather_than_returned_as_a_value() -> None:
    """ "The query returned nothing" and "the query could not run" are indistinguishable in
    the output, and only one of them is a measurement. A silent zero is the single most
    dangerous thing an instrument can return, because it is indistinguishable from good news.
    """
    body = _source().split("_run_capture() {", 1)[1].split("\n}", 1)[0]
    assert 'if [[ -z "$MEASURED" ]]' in body
    assert "EMPTY reading" in body


def test_no_teardown_trap_references_a_function_local() -> None:
    """An EXIT trap runs AFTER the installing function's scope is gone. A trap that reads a
    `local` dies on `set -u` with "unbound variable" — and it dies BEFORE tearing anything
    down, leaking the stack and its volumes.

    This is not hypothetical. `self_test`'s cleanup trap referenced a `local ref_project`, and
    on its first CI run all four calibration cases passed and then the teardown crashed on
    exactly that line, leaving both stacks up. The leak is the same class the hand-run hit
    when compose's `${POSTGRES_USER:?}` guard aborted `down -v`.

    Every name a teardown trap touches must outlive the function that installed it.
    """
    src = _source()
    locals_declared = set(re.findall(r"^\s*local\s+([A-Za-z_][A-Za-z0-9_]*)", src, re.M))

    offenders = []
    for fn in ("teardown", "_st_cleanup"):
        m = re.search(rf"^\s*{re.escape(fn)}\(\) \{{(.*?)^\s*\}}", src, re.S | re.M)
        assert m, f"could not locate {fn}() — the scan must not silently find nothing"
        body = m.group(1)
        # Names the trap READS, minus the ones it declares itself.
        own = set(re.findall(r"^\s*local\s+([A-Za-z_][A-Za-z0-9_]*)", body, re.M))
        for var in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", body):
            if var in locals_declared and var not in own:
                offenders.append(f"{fn}() reads ${var}, which is a `local` elsewhere")

    assert not offenders, "teardown trap(s) reference a function-local:\n  " + "\n  ".join(
        dict.fromkeys(offenders)
    )


@pytest.mark.parametrize("name", ["ref_project", "ST_REF_PROJECT"])
def test_the_trap_scan_can_see_the_defect_it_was_written_for(name: str) -> None:
    """Calibrate the scan against the ACTUAL defect, verbatim: a trap reading a local declared
    by the function that installed it.

    BOTH CASINGS ARE FIXTURES, and that is not padding. The first version of this scan matched
    only `[a-z_]`, so when the fix renamed the variable to the UPPERCASE `ST_REF_PROJECT` the
    guard went blind to precisely the line it had just been written to protect — and a mutation
    re-introducing the bug SURVIVED. Shell locals are freely cased; a case-sensitive scan of
    them is a scan with a hole in it.

    An author cannot calibrate their own guard by reading it
    (feedback_measurement_is_the_thing_that_breaks, deploy#624): the fixture shared the
    filter's blind spot by construction, exactly as it did there. Only running the mutation
    exposed it.
    """
    broken = textwrap.dedent(
        f"""\
        self_test() {{
            local {name}="isnad-rv-selftest-ref"
            _st_cleanup() {{
                local r=$?
                _st_dc "${name}" down -v || true
                exit "$r"
            }}
            trap _st_cleanup EXIT
        }}
        """
    )
    locals_declared = set(re.findall(r"^\s*local\s+([A-Za-z_][A-Za-z0-9_]*)", broken, re.M))
    m = re.search(r"^\s*_st_cleanup\(\) \{(.*?)^\s*\}", broken, re.S | re.M)
    assert m
    own = set(re.findall(r"^\s*local\s+([A-Za-z_][A-Za-z0-9_]*)", m.group(1), re.M))
    reads = set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", m.group(1)))
    assert (reads & locals_declared) - own == {name}, (
        f"the scan cannot see a trap reading the function-local {name!r} — it proves nothing"
    )


def test_instrument_failure_is_a_distinct_exit_code_from_a_real_negative() -> None:
    """A check that cannot run must say so, and must NOT testify. rc=2 ("could not find
    out") must never collapse into rc=1 ("the backup is broken").
    """
    src = _source()
    assert "readonly RC_INSTRUMENT=2" in src
    assert "readonly RC_FAILED=1" in src
    assert 'exit "$RC_INSTRUMENT"' in src


# ---------------------------------------------------------------------------
# The behavioural harness
# ---------------------------------------------------------------------------
# A fake `docker` that models the ONE fact the guards turn on: which compose project a call
# is addressed to, and what that project's stores contain. It also models the state
# TRANSITION — the scratch stack is empty until restore.sh runs — so a restore that loads
# NOTHING is a case this harness can actually express. If it could not, the "scratch must be
# empty first" guard would be untestable and its passing would mean nothing.

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys

world = json.load(open(os.environ["FAKE_WORLD"]))
argv = sys.argv[1:]
LIVE = os.environ["LIVE_PROJECT"]
RV = os.environ["RV_PROJECT"]

# WHICH project did restore.sh actually load into?
#
# The scratch stack becomes populated ONLY if the restore was addressed to IT. A restore
# aimed at the live project loads the PRODUCTION graph and leaves the scratch stack empty —
# which is exactly what would have happened in production, and exactly what the old model
# (a bare "did restore.sh run?" marker) could not express. That inability is the root cause
# of the surviving mutation: a harness that cannot represent the catastrophe cannot rule it
# out. (Weronika Zielinska, PR#642 review.)
_pf = os.environ["RESTORE_PROJECT_FILE"]
restored_into = open(_pf).read().strip() if os.path.exists(_pf) else None
restored = restored_into == RV

def log_call():
    with open(os.environ["FAKE_CALLS"], "a") as fh:
        fh.write(" ".join(argv) + "\n")

log_call()

def stacks(project):
    if project == LIVE:
        return world["live"]
    # The scratch stack is EMPTY until restore.sh has run.
    return world["restored"] if restored else world["empty"]

def emit(header, value):
    # cypher-shell --format plain prints a header line then rows; the script strips the
    # header with `tail -n +2`. A fake that omitted it would make every reading empty.
    print(header)
    print(value)

def die(msg, rc=1):
    print(msg, file=sys.stderr)
    sys.exit(rc)

if argv[0] == "info":
    # A REAL directory: the script runs `df` against it. Returning /var/lib/docker on a box
    # without docker would make `df` fail, and the script would exit on the df — which is a
    # different fault than the one under test.
    print(os.environ["FAKE_DOCKER_ROOT"]); sys.exit(0)

def _project_of_filter():
    for a in argv:
        if a.startswith("label=com.docker.compose.project="):
            return a.split("=", 2)[2]
    return None

# The leak model. `leaks` names the projects whose objects SURVIVE `down -v`; `unremovable`
# names the ones whose objects also survive the direct sweep. Without a fixture that can
# express a leak, every "no stray objects" assertion would be vacuous.
LEAKS = set(world.get("leaks", []))
UNREMOVABLE = set(world.get("unremovable", []))
SWEPT = os.environ["FAKE_SWEPT"]

def _still_present(project):
    if project not in LEAKS:
        return False
    if project in UNREMOVABLE:
        return True                       # the sweep cannot remove it
    return not os.path.exists(SWEPT)      # gone once the sweep has run

if argv[0] == "volume":
    if argv[1] == "inspect":
        sys.exit(1)   # absent -> nothing leaked
    if argv[1] == "ls":
        p = _project_of_filter()
        if p and _still_present(p):
            print("vol-" + p)
        sys.exit(0)
    if argv[1] == "rm":
        target = argv[-1]
        if any(u in target for u in UNREMOVABLE):
            print("Error: volume is in use", file=sys.stderr)
            sys.exit(1)
        open(SWEPT, "a").close()
        sys.exit(0)
    sys.exit(0)

if argv[0] == "network":
    if argv[1] == "ls":
        sys.exit(0)   # networks modelled as always cleaned
    if argv[1] == "rm":
        sys.exit(0)
    sys.exit(0)

if argv[0] == "ps":
    sys.exit(0)       # no stray containers modelled

if argv[0] == "rm":
    sys.exit(0)

if argv[0] == "inspect":
    fmt = argv[2]
    cid = argv[-1]
    if "Mounts" in fmt:
        proj = cid.split("|")[1]
        print(world["volumes"].get(proj, proj + "_rv_neo4j_data")); sys.exit(0)
    if "State.Health" in fmt:
        print("healthy"); sys.exit(0)
    die("unknown inspect format")

if argv[0] != "compose":
    die("unexpected docker subcommand: " + argv[0])

# compose -p P -f F <rest...>
assert argv[1] == "-p", "every compose call must carry -p"
project = argv[2]
assert argv[3] == "-f"
rest = argv[5:]
S = stacks(project)

if rest[0] in ("up", "down", "start", "stop"):
    sys.exit(0)
if rest[0] == "ps":
    if "-q" in rest or "-aq" in rest:
        print("cid|" + project); sys.exit(0)
    print("(ps table)"); sys.exit(0)

if rest[0] == "exec":
    if "neo4j" in rest and "du" in rest:
        print("8000000\t/data"); sys.exit(0)

    if "neo4j" in rest:
        q = rest[-1]
        if "SKIP 1" in q:
            emit("fp", '"%s"' % S["fp_minus1"]); sys.exit(0)
        if "AS parts" in q:
            emit("parts", '"%s"' % S["parts"]); sys.exit(0)
        if "AS fp" in q:
            emit("fp", '"%s"' % S["fp"]); sys.exit(0)
        if "UNWIND labels" in q:
            emit("h", S["label_hist"]); sys.exit(0)
        if "type(r)" in q:
            emit("h", S["rel_hist"]); sys.exit(0)
        if "-[r]->" in q:
            emit("count(r)", S["rels"]); sys.exit(0)
        if "count(n)" in q:
            emit("count(n)", S["nodes"]); sys.exit(0)
        die("fake docker: unmodelled cypher: " + q)

    if "postgres" in rest and "user-postgres" not in rest:
        sql = rest[-1]
        if "information_schema" in sql:
            print(S["pg_tables"]); sys.exit(0)
        if "OFFSET 1" in sql:
            print(S["hadith_md5_minus1"]); sys.exit(0)
        if "md5" in sql:
            print(S["hadith_md5"]); sys.exit(0)
        if "count(*)" in sql:
            print(S["hadiths"]); sys.exit(0)
        die("fake docker: unmodelled sql: " + sql)

    if "user-postgres" in rest:
        sql = rest[-1]
        if "information_schema" in sql:
            print(S["upg_tables"]); sys.exit(0)
        for key, tok in (("users_md5", "md5"), ("users", "FROM users"),
                         ("oauth", "oauth_accounts"), ("sessions", "sessions"),
                         ("audit", "audit_log"), ("alembic", "alembic_version")):
            if tok in sql:
                print(S[key]); sys.exit(0)
        die("fake docker: unmodelled user sql: " + sql)

die("fake docker: unmodelled call: " + " ".join(argv))
"""

# A populated, self-consistent world. The scratch stack starts EMPTY and becomes a copy of
# live once the (fake) restore.sh has run — i.e. the happy path.
LIVE_WORLD = {
    "nodes": "107951",
    "rels": "250000",
    "fp": "50000/300000/450000/350000",
    "fp_minus1": "49999/299994/449991/349993",  # DIFFERS -> the comparator can go red
    "parts": "50000/300000/450000/350000",
    "label_hist": "Hadith=20000 Narrator=50000",
    "rel_hist": "NARRATED=90000 NARRATED_TO=160000",
    "hadiths": "34000",
    "hadith_md5": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "hadith_md5_minus1": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",  # DIFFERS
    "pg_tables": "12",
    "upg_tables": "6",
    "users": "42",
    "users_md5": "cccccccccccccccccccccccccccccccc",
    "oauth": "7",
    "sessions": "3",
    "audit": "900",
    "alembic": "a1b2c3d4",
}

EMPTY_WORLD = {
    **{k: "0" for k in LIVE_WORLD},
    "fp": "0/0/0/0",
    "fp_minus1": "0/0/0/0",
    "parts": "0/0/0/0",
    "label_hist": "",
    "rel_hist": "",
    "hadith_md5": "",
    "hadith_md5_minus1": "",
    "alembic": "",
}


def _world(
    live=None, restored=None, empty=None, volumes=None, leaks=None, unremovable=None
) -> dict:
    return {
        "live": {**LIVE_WORLD, **(live or {})},
        # By default the restore reproduces live exactly.
        "restored": {**LIVE_WORLD, **(restored or {})},
        "empty": {**EMPTY_WORLD, **(empty or {})},
        "volumes": volumes or {RV_PROJECT: f"{RV_PROJECT}_rv_neo4j_data"},
        "leaks": leaks or [],
        "unremovable": unremovable or [],
    }


class Harness:
    def __init__(self, tmp: Path, world: dict, env_extra: dict | None = None):
        self.tmp = tmp
        self.scripts = tmp / "scripts"
        self.scripts.mkdir(parents=True)
        self.bin = tmp / "bin"
        self.bin.mkdir()

        # The script under test, in a scripts/ dir whose sibling restore.sh we control.
        shutil.copy(RESTORE_VERIFY, self.scripts / "restore_verify.sh")
        os.chmod(self.scripts / "restore_verify.sh", 0o755)

        (tmp / "compose").mkdir()
        shutil.copy(RV_COMPOSE, tmp / "compose" / "docker-compose.restore-verify.yml")
        (tmp / "compose" / "docker-compose.prod.yml").write_text("services: {}\n")

        self.marker = tmp / "restore_ran"
        self.calls = tmp / "calls.log"
        self.calls.write_text("")
        # WHICH PROJECT the restore was actually addressed to. This file is the whole point of
        # the harness rework: without it, "restore.sh loaded into the LIVE project" was a state
        # the world model could not represent, and the mutation that causes it survived.
        self.restore_project = tmp / "restore_project"

        # Which COMPOSE_FILE the restore resolved — the second single point of failure of the
        # same class (restore.sh:62 defaults COMPOSE_FILE to compose/docker-compose.prod.yml).
        self.restore_file = tmp / "restore_file"

        # A fake restore.sh that RESOLVES ITS OWN PROJECT AND COMPOSE_FILE exactly as the real
        # one does — by reading the REAL definitions, not paraphrasing them
        # (feedback_paraphrase_in_the_product) — records both, and announces the volume it would
        # load into, including the LIVE volume when it has been aimed at the live project. Whether
        # it is reached at all is the assertion for every guard that must abort BEFORE any load.
        #
        #   * COMPOSE_PROJECT: source the REAL compose_project.sh BY ABSOLUTE PATH. Its fallback
        #     (`${COMPOSE_PROJECT:-${COMPOSE_PROJECT_NAME:-noorinalabs}}`) is what makes a dropped
        #     export catastrophic. Sourcing by absolute path (not a copy) means ITS OWN evolving
        #     dependencies — e.g. scratch.sh, added in #643 — resolve against the real scripts
        #     dir. A copy drifts and breaks the harness the next time that file grows a dep; the
        #     original does not.
        #   * COMPOSE_FILE: eval the REAL `^COMPOSE_FILE=` default line out of restore.sh, so a
        #     change to restore.sh's default is reflected here rather than silently diverging.
        real_cp = SCRIPTS_DIR / "compose_project.sh"
        real_restore = SCRIPTS_DIR / "restore.sh"
        (self.scripts / "restore.sh").write_text(
            "#!/usr/bin/env bash\n"
            "set -uo pipefail\n"
            'log() { echo "[$1] ${*:2}"; }\n'
            f'eval "$(grep -E \'^COMPOSE_FILE=\' "{real_restore}" | head -1)"\n'
            "# shellcheck source=/dev/null\n"
            f'source "{real_cp}"\n'
            f'printf "%s" "${{COMPOSE_PROJECT}}" > {self.restore_project}\n'
            f'printf "%s" "${{COMPOSE_FILE}}" > {self.restore_file}\n'
            f"echo RESTORE_INVOKED >> {self.marker}\n"
            # The volume it resolves is the /data volume of the neo4j container in WHATEVER
            # project it was pointed at. Aimed at `noorinalabs`, that is the production graph.
            'if [ "${COMPOSE_PROJECT}" = "' + LIVE_PROJECT + '" ]; then\n'
            f'  VOL="{LIVE_PROJECT}_neo4j_data"\n'
            "else\n"
            '  VOL="${COMPOSE_PROJECT}_rv_neo4j_data"\n'
            "fi\n"
            'VOL="${FAKE_RESOLVED_VOLUME:-$VOL}"\n'
            'RFILE="${FAKE_RESOLVED_FILE:-${COMPOSE_FILE}}"\n'
            'log "INFO" "Resolved Neo4j data volume: ${VOL}'
            ' (project ${COMPOSE_PROJECT}, ${RFILE})"\n'
            "exit ${FAKE_RESTORE_RC:-0}\n"
        )
        os.chmod(self.scripts / "restore.sh", 0o755)

        (self.bin / "docker").write_text(FAKE_DOCKER)
        os.chmod(self.bin / "docker", 0o755)
        for stub in ("rclone", "zstd", "sha256sum"):
            (self.bin / stub).write_text("#!/bin/sh\nexit 0\n")
            os.chmod(self.bin / stub, 0o755)

        self.world_file = tmp / "world.json"
        self.world_file.write_text(json.dumps(world))

        (tmp / ".env").write_text(
            textwrap.dedent(
                """\
                B2_BUCKET=noorinalabs-backups
                B2_KEY_ID=k
                B2_APP_KEY=s
                BACKUP_PREFIX=prod
                POSTGRES_USER=IsnadDbUseer
                POSTGRES_DB=Isnad-Prod
                USER_POSTGRES_USER=user_service
                USER_POSTGRES_DB=user_service
                """
            )
        )
        self.env_extra = env_extra or {}

    def run(self, **env_over) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "FAKE_WORLD": str(self.world_file),
            "FAKE_CALLS": str(self.calls),
            "FAKE_DOCKER_ROOT": str(self.tmp),
            "FAKE_SWEPT": str(self.tmp / "swept"),
            "RESTORE_MARKER": str(self.marker),
            "RESTORE_PROJECT_FILE": str(self.restore_project),
            "LIVE_PROJECT": LIVE_PROJECT,
            "RV_PROJECT": RV_PROJECT,
            "ENV_FILE": str(self.tmp / ".env"),
            # Keep the resource gate satisfiable in CI.
            "REQUIRED_MEM_MB": "1",
            "DISK_MARGIN_MB": "0",
            **self.env_extra,
            **env_over,
        }
        return subprocess.run(
            ["bash", str(self.scripts / "restore_verify.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    @property
    def restore_was_invoked(self) -> bool:
        return self.marker.exists()

    @property
    def restored_into(self) -> str | None:
        """The compose project restore.sh actually resolved — i.e. the project whose Neo4j
        volume `neo4j-admin database load --overwrite-destination` would have written to."""
        if not self.restore_project.exists():
            return None
        return self.restore_project.read_text().strip()

    @property
    def restored_with_file(self) -> str | None:
        """The COMPOSE_FILE restore.sh actually resolved — the second single point of failure."""
        if not self.restore_file.exists():
            return None
        return self.restore_file.read_text().strip()


@pytest.fixture
def harness(tmp_path):
    def _make(world=None, **env_extra):
        return Harness(tmp_path, world or _world(), env_extra)

    return _make


# ---------------------------------------------------------------------------
# The happy path — and it must be a REAL pass, not a vacuous one
# ---------------------------------------------------------------------------
def test_a_faithful_restore_verifies(harness) -> None:
    h = harness()
    r = h.run()
    assert r.returncode == RC_VERIFIED, (
        f"expected VERIFIED\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "CALIBRATION PASSED" in r.stdout
    assert "VERIFIED" in r.stdout
    assert h.restore_was_invoked


def test_the_happy_path_actually_exercised_the_comparator(harness) -> None:
    """Calibrates the test above. If the happy path passed without ever READING the stores,
    its green would be worth nothing — which is the exact failure mode this whole issue is
    about. Require the comparison table to have been printed with real values in it.
    """
    h = harness()
    r = h.run()
    assert r.returncode == RC_VERIFIED
    assert "neo4j narrator fp" in r.stdout
    assert LIVE_WORLD["fp"] in r.stdout, "the fingerprint was never actually read"
    assert "pg hadiths md5" in r.stdout
    assert r.stdout.count("MATCH") >= 10, "too few metrics compared to call this a verification"


# ---------------------------------------------------------------------------
# The comparator must be able to go RED
# ---------------------------------------------------------------------------
def test_a_restore_that_loses_one_narrator_is_caught(harness) -> None:
    """The single case the whole design turns on."""
    lossy = {**LIVE_WORLD, "fp": "49999/299994/449991/349993", "nodes": "107950"}
    h = harness(_world(restored=lossy))
    r = h.run()
    assert r.returncode == RC_FAILED, f"a lossy restore must FAIL\n{r.stdout}"
    assert "*** MISMATCH ***" in r.stdout
    assert "real negative" in r.stdout


def test_a_restore_with_the_right_counts_but_corrupted_content_is_caught(harness) -> None:
    """Counts can match while the bytes are garbage. A comparator that only counted rows
    would go green here, which is why the fingerprints checksum the CONTENT.
    """
    corrupt = {**LIVE_WORLD, "hadith_md5": "dddddddddddddddddddddddddddddddd"}
    h = harness(_world(restored=corrupt))
    r = h.run()
    assert r.returncode == RC_FAILED
    assert "pg hadiths md5" in r.stdout
    assert "*** MISMATCH ***" in r.stdout


def test_a_restore_that_silently_did_nothing_is_caught(harness) -> None:
    """`pg_restore --clean` can warn-and-succeed while restoring nothing, and restore.sh
    would exit 0. The verdict is never the exit code.
    """
    h = harness(_world(restored=EMPTY_WORLD))
    r = h.run()
    assert r.returncode != RC_VERIFIED, "a no-op restore must NOT verify"
    assert r.returncode in (RC_FAILED, RC_INSTRUMENT)


# ---------------------------------------------------------------------------
# INERT COMPARATOR -> refuse to testify (rc=2). The n.name defect, as a guard.
# ---------------------------------------------------------------------------
def test_a_fingerprint_that_cannot_see_a_missing_narrator_refuses_to_testify(harness) -> None:
    """If the minus-one fingerprint equals the full one, the instrument is blind to a whole
    missing node and every MATCH it reports is meaningless. It must abort — and it must NOT
    report the backup as broken, because it has learned nothing about the backup.
    """
    inert = {**LIVE_WORLD, "fp_minus1": LIVE_WORLD["fp"]}
    h = harness(_world(live=inert))
    r = h.run()
    assert r.returncode == RC_INSTRUMENT, f"an inert comparator must exit 2\n{r.stdout}"
    assert "CANNOT SEE A MISSING NARRATOR" in r.stdout
    assert "REFUSING TO TESTIFY" in r.stdout
    assert not h.restore_was_invoked, "must abort BEFORE spending 20 minutes on a restore"


def test_the_n_name_defect_a_dead_property_component_refuses_to_testify(harness) -> None:
    """THE defect, reproduced. `n.name` does not exist on :Narrator, so the fingerprint
    component sums to 0 — on BOTH stacks. They "agree" perfectly and the checksum is 0 on
    both sides.

    A both-sides-zero is not a match. It is the instrument reading nothing, twice.
    """
    dead = {**LIVE_WORLD, "parts": "50000/0/0/0"}
    h = harness(_world(live=dead))
    r = h.run()
    assert r.returncode == RC_INSTRUMENT, f"a dead fingerprint component must exit 2\n{r.stdout}"
    assert "REFUSING TO TESTIFY" in r.stdout
    assert "does not exist on :Narrator" in r.stdout
    assert not h.restore_was_invoked


def test_an_empty_live_graph_refuses_to_testify(harness) -> None:
    """Zero narrators on the reference side means a "match" would be two empty readings
    agreeing. That is the vacuous green the calibration exists to prevent.
    """
    h = harness(_world(live={**LIVE_WORLD, "parts": "0/0/0/0"}))
    r = h.run()
    assert r.returncode == RC_INSTRUMENT
    assert "ZERO :Narrator" in r.stdout
    assert not h.restore_was_invoked


# ---------------------------------------------------------------------------
# THE HANDOFF: which project does the destructive load actually land in?
# ---------------------------------------------------------------------------
# The load is NOT performed by any compose call in restore_verify.sh. It is performed by
# restore.sh, which resolves its OWN project and falls back to the LIVE one. Everything below
# is about that handoff, and none of it was expressible before the fake restore.sh was taught
# to source the real compose_project.sh and record what it resolved.
def test_the_restore_is_addressed_to_the_throwaway_project(harness) -> None:
    h = harness()
    r = h.run()
    assert r.returncode == RC_VERIFIED
    assert h.restored_into == RV_PROJECT, (
        f"restore.sh loaded into project {h.restored_into!r}, not the throwaway one"
    )


def test_the_restore_is_NEVER_addressed_to_the_live_project(harness) -> None:
    """The catastrophe, as an assertion. `-p noorinalabs` + service `neo4j` resolves the LIVE
    container, whose /data is `noorinalabs_neo4j_data`. restore.sh would resolve it, log it, and
    load into it with --overwrite-destination. Unrecoverable.
    """
    h = harness()
    h.run()
    assert h.restored_into not in (LIVE_PROJECT, "", None), (
        f"THE RESTORE WAS AIMED AT {h.restored_into!r} — this overwrites the production graph"
    )


def test_the_harness_can_express_the_catastrophe(harness) -> None:
    """THE ROOT CAUSE OF THE SURVIVING MUTATION, made into a test.

    The old fake restore.sh ignored COMPOSE_PROJECT entirely, and the old fake docker decided
    what the scratch stack contained purely from a "did restore.sh run?" marker. So *"restore.sh
    loaded into the LIVE project"* was **a state the world model could not represent** — and a
    mutation deleting the one line that prevents it passed 33/33.

    A mutation harness that cannot express the catastrophe cannot rule it out. This test proves
    the harness now can: drive restore.sh with no COMPOSE_PROJECT and watch it resolve
    `noorinalabs` through the REAL compose_project.sh.
    """
    h = harness()
    env = {
        "PATH": f"{h.bin}:{os.environ['PATH']}",
        "FAKE_WORLD": str(h.world_file),
        "FAKE_CALLS": str(h.calls),
        "RESTORE_MARKER": str(h.marker),
        "RESTORE_PROJECT_FILE": str(h.restore_project),
        "LIVE_PROJECT": LIVE_PROJECT,
        "RV_PROJECT": RV_PROJECT,
        "COMPOSE_FILE": str(h.tmp / "compose" / "docker-compose.restore-verify.yml"),
    }
    # Exactly what run_restore does MINUS the COMPOSE_PROJECT export — i.e. the mutation.
    subprocess.run(
        ["bash", str(h.scripts / "restore.sh"), "--force", "latest"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert h.restored_into == LIVE_PROJECT, (
        "the harness STILL cannot express a restore aimed at the live project — so a green "
        "suite would still not rule out overwriting production"
    )


def test_a_restore_that_reports_a_live_volume_is_refused(harness) -> None:
    """Fix 4: verify the handoff, do not trust it. If restore.sh announces that it resolved a
    volume that is not ours, the run must refuse — loudly — rather than proceed to compare.
    """
    h = harness(FAKE_RESOLVED_VOLUME=f"{LIVE_PROJECT}_neo4j_data")
    r = h.run()
    assert r.returncode == RC_INSTRUMENT, f"a live-volume handoff must be refused\n{r.stdout}"
    assert "HANDOFF VIOLATION" in r.stdout
    assert "UNRECOVERABLE" in r.stdout


def test_a_restore_that_reports_a_non_throwaway_compose_file_is_refused(harness) -> None:
    """The COMPOSE_FILE sibling, as a runtime assertion. restore.sh:62 defaults COMPOSE_FILE to
    `compose/docker-compose.prod.yml`; a throwaway run that resolved the prod compose file is a
    handoff violation even if the volume happens to look right. Require the reported compose file
    to be OURS. (Weronika Zielinska, PR#642 review — fix both siblings or neither.)
    """
    h = harness(FAKE_RESOLVED_FILE="compose/docker-compose.prod.yml")
    r = h.run()
    assert r.returncode == RC_INSTRUMENT, f"a prod-compose-file handoff must be refused\n{r.stdout}"
    assert "HANDOFF VIOLATION" in r.stdout
    assert "compose-file" in r.stdout


def test_the_restore_uses_the_throwaway_compose_file(harness) -> None:
    """The positive: restore.sh actually resolved OUR compose file, recorded independently."""
    h = harness()
    r = h.run()
    assert r.returncode == RC_VERIFIED
    assert h.restored_with_file == str(h.tmp / "compose" / "docker-compose.restore-verify.yml"), (
        f"restore.sh resolved compose file {h.restored_with_file!r}, not the throwaway one"
    )


def test_the_handoff_check_accepts_our_own_volume(harness) -> None:
    """Calibrate the guard above: it must be able to say YES, or it is an unconditional abort."""
    h = harness()
    r = h.run()
    assert r.returncode == RC_VERIFIED
    assert "handoff verified" in r.stdout


@pytest.mark.parametrize("blank", ["", "   "])
def test_the_env_file_cannot_blank_the_project(harness, blank) -> None:
    """Fix 1: EMPTY is as dangerous as ABSENT — and more dangerous than WRONG.

    `${VAR:-default}` treats "" exactly like unset, so a blank RV_PROJECT does not no-op: it
    resolves to `noorinalabs` inside restore.sh and aims --overwrite-destination at production.
    `guard_not_live`'s `!= noorinalabs` check passes happily on "".

    THE REACHABLE PATH IS THE .env, NOT AN INHERITED ENV VAR — and getting this wrong is
    instructive. An inherited `RV_PROJECT=""` is REPAIRED by the script's own
    `RV_PROJECT="${RV_PROJECT:-…}"` default before any guard sees it, so a test that exported an
    empty var would pass for entirely the wrong reason and prove nothing.

    The dangerous ordering is the one Weronika identified: `load_env()` sources the host `.env`
    with `set -a` AFTER that default has already been applied. An `RV_PROJECT=` line there lands
    downstream of the repair, and nothing has refused it since. That is the path under test.
    """
    h = harness()
    (h.tmp / ".env").write_text((h.tmp / ".env").read_text() + f"RV_PROJECT={blank}\n")
    r = h.run()
    assert r.returncode == RC_INSTRUMENT, f"a blanked RV_PROJECT must be refused\n{r.stdout}"
    assert "EMPTY or unset" in r.stdout
    assert not h.restore_was_invoked, (
        "restore.sh was invoked with a blank project — it would resolve `noorinalabs` and "
        "overwrite the production graph"
    )


# ---------------------------------------------------------------------------
# The deploy#617 guard — abort BEFORE any load
# ---------------------------------------------------------------------------
def test_it_refuses_when_the_neo4j_volume_is_not_its_own(harness) -> None:
    """`neo4j-admin database load` runs with `--overwrite-destination`. Against the live
    volume that is unrecoverable. The volume must be asserted BEFORE anything is loaded.
    """
    h = harness(_world(volumes={RV_PROJECT: f"{LIVE_PROJECT}_neo4j_data"}))
    r = h.run()
    assert r.returncode == RC_INSTRUMENT
    assert "volume isolation" in r.stdout
    assert "ABORTING" in r.stdout
    assert not h.restore_was_invoked, "it loaded into a live volume — this is the #617 disaster"


def test_it_refuses_a_stray_volume_that_is_neither_live_nor_its_own(harness) -> None:
    """THE deploy#617 SHAPE, and the one case only the primary guard can catch.

    #617 was not a volume named `noorinalabs_*`. Compose fell back to the compose file's
    DIRECTORY basename — `compose` — and `up` CREATED a new, empty Neo4j in a project called
    `compose`. A guard that only refuses volumes starting with the live project's name would
    wave that straight through, because `compose_neo4j_data` is not live-prefixed.

    This test exists because a mutation run showed the previous one passing with the primary
    guard DELETED: the belt-and-braces live-prefix check was catching it, so the assertion
    that was supposed to be under test was never exercised at all. An author cannot calibrate
    their own guard by looking at it (feedback_measurement_is_the_thing_that_breaks).
    """
    h = harness(_world(volumes={RV_PROJECT: "compose_neo4j_data"}))
    r = h.run()
    assert r.returncode == RC_INSTRUMENT, f"a stray non-own volume must abort\n{r.stdout}"
    assert "volume isolation" in r.stdout
    assert "ABORTING" in r.stdout
    assert not h.restore_was_invoked


def test_the_volume_guard_passes_on_its_own_volume(harness) -> None:
    """Calibrates the test above: the guard must also be able to say YES, or it is just an
    unconditional abort and proves nothing.
    """
    h = harness()
    r = h.run()
    assert r.returncode == RC_VERIFIED
    assert "volume isolation: the restore targets" in r.stdout


def test_it_refuses_to_run_against_the_live_project(harness) -> None:
    h = harness(RV_PROJECT=LIVE_PROJECT)
    r = h.run()
    assert r.returncode == RC_INSTRUMENT
    assert "IS the live project" in r.stdout
    assert not h.restore_was_invoked


@pytest.mark.parametrize("var", ["COMPOSE_FILE", "COMPOSE_PROJECT", "COMPOSE_PROJECT_NAME"])
def test_it_refuses_an_inherited_compose_target(harness, var) -> None:
    """restore.sh reads COMPOSE_FILE and COMPOSE_PROJECT. An ambient value pointing at the
    live stack is a loaded gun, and we refuse to run beside one.
    """
    h = harness()
    r = h.run(**{var: "noorinalabs" if "PROJECT" in var else "compose/docker-compose.prod.yml"})
    assert r.returncode == RC_INSTRUMENT
    assert "inherited" in r.stdout
    assert not h.restore_was_invoked


# ---------------------------------------------------------------------------
# A dirty scratch stack cannot be allowed to pass
# ---------------------------------------------------------------------------
def test_it_refuses_when_the_scratch_stores_are_not_empty(harness) -> None:
    """If the scratch graph already holds yesterday's restore, a restore that loads NOTHING
    still "matches" live. The stores must be empty first or a no-op passes.
    """
    h = harness(_world(empty={**EMPTY_WORLD, "nodes": "107951"}))
    r = h.run()
    assert r.returncode == RC_INSTRUMENT
    assert "already holds" in r.stdout
    assert not h.restore_was_invoked


# ---------------------------------------------------------------------------
# TEARDOWN: it must run on EVERY exit path, and it must never speak for the verdict
# ---------------------------------------------------------------------------
# A teardown that dies partway is how volumes leak, and a leaked scratch volume holds a full
# copy of production. The hand-run leaked exactly this way when compose's `${POSTGRES_USER:?}`
# guard aborted `down -v`, and the self-test leaked again on its first CI run when its EXIT
# trap read a function-local.
def _torn_down(h: Harness, project: str = RV_PROJECT) -> bool:
    """Did the TEARDOWN run — as opposed to merely some `down -v`?

    NOT `any("down -v" in c)`. `start_stack()` issues a PRE-CLEAN `down -v` before `up`, and it
    is textually identical to the teardown's. A helper that matched on `down -v` alone returned
    True even when the EXIT trap never fired at all — and a mutation that deleted the teardown
    entirely left `test_teardown_runs_when_the_restore_itself_FAILS` GREEN.

    The teardown is the only caller of `leak_audit()`, and `leak_audit()` is the only thing that
    enumerates by compose's project LABEL. So that enumeration is the signature that cannot be
    forged by the pre-clean.

    Third time this session that a helper shared the blind spot of the thing it was measuring
    (feedback_measurement_is_the_thing_that_breaks). Only the mutation showed it.
    """
    calls = h.calls.read_text().splitlines()
    audited = any(f"label=com.docker.compose.project={project}" in c and "ls" in c for c in calls)
    downed = any(f"-p {project}" in c and "down" in c and "-v" in c for c in calls)
    return audited and downed


def test_teardown_runs_when_the_comparator_PASSES(harness) -> None:
    h = harness()
    r = h.run()
    assert r.returncode == RC_VERIFIED
    assert _torn_down(h), "the stack was left up after a passing run"
    assert "no stray containers, volumes or networks remain" in r.stdout


def test_teardown_runs_when_the_comparator_FAILS(harness) -> None:
    """The path that actually matters. A failing run is exactly when an author stops paying
    attention — and it is the run most likely to leave a stack up.
    """
    h = harness(_world(restored={**LIVE_WORLD, "nodes": "1"}))
    r = h.run()
    assert r.returncode == RC_FAILED
    assert _torn_down(h), "the stack was left up after a FAILING run — this is the leak"
    assert "no stray containers, volumes or networks remain" in r.stdout


def test_teardown_runs_when_the_restore_itself_FAILS(harness) -> None:
    h = harness(FAKE_RESTORE_RC="1")
    r = h.run()
    assert r.returncode == RC_FAILED
    assert _torn_down(h)
    assert "no stray containers, volumes or networks remain" in r.stdout


def test_the_teardown_calibration_fixture_can_actually_express_a_leak(harness) -> None:
    """Calibrate the three assertions above. If the harness could not model a stack that
    SURVIVES `down -v`, then "no stray objects" would be true by construction and every
    teardown assertion here would be vacuous — the exact shape of a guard that cannot go red.
    """
    h = harness(_world(leaks=[RV_PROJECT], unremovable=[RV_PROJECT]))
    r = h.run()
    assert "STRAY OBJECTS REMAIN" in r.stdout, (
        "the fixture cannot express a leak, so the clean-teardown assertions prove nothing"
    )


def test_a_leak_on_a_GREEN_run_does_not_exit_zero(harness) -> None:
    """The restore verified, but the box is not clean: a scratch volume holding a full copy of
    production is still sitting there. That must not exit 0. It is an INSTRUMENT fault (2), and
    it is reported as exactly what it is — the comparator passed AND the teardown leaked.
    """
    h = harness(_world(leaks=[RV_PROJECT], unremovable=[RV_PROJECT]))
    r = h.run()
    assert r.returncode == RC_INSTRUMENT, f"a leak must not pass as green\n{r.stdout}"
    assert "The comparator PASSED, but the teardown LEAKED" in r.stdout


def test_a_leak_CANNOT_overwrite_a_failing_verdict(harness) -> None:
    """THE rule. A cleanup fault must never overwrite a failing comparator result — in either
    direction. Here the comparator FAILED (rc=1) and the teardown ALSO leaked; the run must
    still exit 1, because "the artifact does not restore" is the more important fact and it is
    the one an operator acts on. If the leak re-wrote this to 2, a real negative would be
    reported as a broken instrument and the backup would look merely unverified.
    """
    h = harness(
        _world(
            restored={**LIVE_WORLD, "nodes": "1"},  # a real negative
            leaks=[RV_PROJECT],
            unremovable=[RV_PROJECT],  # and a leaking teardown on top of it
        )
    )
    r = h.run()
    assert r.returncode == RC_FAILED, (
        f"the teardown's fault overwrote the comparator's verdict (got {r.returncode}, want 1)"
    )
    assert "*** MISMATCH ***" in r.stdout
    assert "STRAY OBJECTS REMAIN" in r.stdout, "the leak must still be REPORTED, just not obeyed"


def test_a_recoverable_leak_is_swept_and_the_run_stays_green(harness) -> None:
    """`down -v` left the volume behind, the direct sweep removed it. That is a clean box, and
    a green run — not an alarm. The distinction is what stops this check from crying wolf.
    """
    h = harness(_world(leaks=[RV_PROJECT]))  # leaks, but IS removable by the sweep
    r = h.run()
    assert r.returncode == RC_VERIFIED
    assert "survived 'down -v'" in r.stdout, "the sweep never actually ran"
    assert "no stray containers, volumes or networks remain" in r.stdout


# ---------------------------------------------------------------------------
# Instrument failure vs real negative — the distinction must survive
# ---------------------------------------------------------------------------
def test_a_failing_restore_is_a_real_negative_not_an_instrument_failure(harness) -> None:
    h = harness(FAKE_RESTORE_RC="1")
    r = h.run()
    assert r.returncode == RC_FAILED, "restore.sh failing IS a real negative"
    assert "did not restore" in r.stdout


def test_a_prefix_mismatch_refuses_rather_than_verify_the_wrong_environment(harness) -> None:
    """The host's .env says prod. A matrix leg that believes it is checking stg must not
    restore prod's artifact and compare it against prod's live stack while reporting `stg`.
    Before #632 the backup watchdog had exactly this defect.
    """
    h = harness(EXPECT_PREFIX="stg")  # the .env fixture says prod
    r = h.run()
    assert r.returncode == RC_INSTRUMENT
    assert "prefix mismatch" in r.stdout
    assert not h.restore_was_invoked


def test_a_matching_prefix_proceeds(harness) -> None:
    """Calibrates the guard above — it must be able to say yes."""
    h = harness(EXPECT_PREFIX="prod")
    r = h.run()
    assert r.returncode == RC_VERIFIED


def test_a_missing_env_file_is_an_instrument_failure(harness) -> None:
    h = harness()
    r = h.run(ENV_FILE=str(h.tmp / "nonexistent.env"))
    assert r.returncode == RC_INSTRUMENT
    assert "INSTRUMENT FAILURE" in r.stdout
    assert not h.restore_was_invoked


def test_an_unreadable_store_is_an_instrument_failure_not_a_backup_verdict(harness) -> None:
    """A query that CANNOT RUN must not be reported as a backup that does not restore.
    Collapsing rc=2 into rc=1 is how a broken credential became `*** DATA LOSS ***`
    (deploy#632/#641).
    """
    broken = _world()
    del broken["live"]["parts"]  # the fake will KeyError -> non-zero rc + stderr
    h = harness(broken)
    r = h.run()
    assert r.returncode == RC_INSTRUMENT, "an unreadable store is not a backup verdict"
    assert "INSTRUMENT FAILURE" in r.stdout


def test_the_stderr_of_a_failed_reading_reaches_the_operator(harness) -> None:
    """The diagnostic IS the finding on the failure path. If it is swallowed, the operator
    is left to reverse-engineer, at 3am, why the instrument said nothing.
    """
    broken = _world()
    del broken["live"]["parts"]
    h = harness(broken)
    r = h.run()
    combined = r.stdout + r.stderr
    assert "KeyError" in combined or "Traceback" in combined, (
        "the failing tool's own diagnostic was discarded — this is the 2>/dev/null defect"
    )
