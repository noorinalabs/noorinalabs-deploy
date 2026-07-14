"""Run a systemd unit's ExecStart under THAT UNIT'S OWN hardening — in CI, rootlessly.

WHY THIS EXISTS
---------------
deploy#613, then deploy#623: a script under ``isnad-backup.service`` allocated scratch in
``/tmp``, which is **READ-ONLY** there (``ProtectSystem=strict``, ``PrivateTmp`` deliberately
unset — deploy#121 Bug A), and then reported its own broken instrument as a fact about the
subsystem it was meant to check. *"Your B2 key is invalid"* on a good key. *"Is the daemon
running?"* on a healthy daemon.

Both shipped past a full green CI and four reviewers. **Not because the tests were weak —
because they were structurally incapable of failing.** GitHub Actions runners, the rehearsal
containers and the dev box all have a writable ``/tmp``. Every test we had, *including* the
e2e tests that drive the real ``backup.sh``, ran in an environment where the defect class is
unreachable. A suite that cannot express the defect is not a weak instrument; it is not an
instrument at all.

THE CLASS IS NOT "BARE MKTEMP"
------------------------------
It is **"writes outside ``ReadWritePaths=``"**. ``scripts/tests/test_scratch_under_hardening.py``
pins the ``mktemp`` spelling structurally, and that pin is worth having — but a ``> /tmp/foo``,
a ``python -c 'tempfile.mkstemp()'``, a ``$HOME`` write under ``ProtectHome=yes`` are none of
them ``mktemp``, all fail identically under the unit, and a text-scan for ``mktemp`` catches
none of them. ``mktemp`` being the only current instance is a **coincidence**, and relying on
it is how we get a fourth one.

So this module does not scan text. It **runs the real script in a namespace built from the
unit's own directives** and observes what happens.

THE LOAD-BEARING PROPERTY: THE CONSTRAINT IS READ OUT OF THE UNIT, NEVER RE-TYPED
--------------------------------------------------------------------------------
``ProtectSystem=``, ``ReadWritePaths=``, ``ProtectHome=``, ``PrivateTmp=`` are **parsed out of
the .service file** and the sandbox is constructed from them. A hand-written "read-only /tmp"
job would be a **third place** to encode the same assumption, stale the moment somebody edits
``ReadWritePaths=``. Derived from the unit, the harness **cannot disagree with production about
what production allows** — grant a new path in the unit and the sandbox grants it too, in the
same commit, with no second edit anywhere.

AND WHAT IT REFUSES TO DO
-------------------------
A directive this module does not know how to emulate is an **instrument failure** (``rc=2``),
never a silent pass. If someone adds ``InaccessiblePaths=`` or switches to
``ProtectSystem=full``, the harness stops and says it cannot testify, rather than quietly
ignoring the directive and reporting green over an emulation that no longer matches the unit.
That refusal is the whole reason to trust a green run.

Exit codes (the driver, and every caller of it):

* ``0`` — the entry point ran under the unit's own hardening and exited 0.
* ``1`` — the entry point FAILED under the hardening. **A finding.**
* ``2`` — **WE COULD NOT FIND OUT.** No ``unshare``, a directive we cannot emulate, a sandbox
  that failed its own self-check. Never conflate this with ``0``: a silently-skipped job is
  exactly the false green this module exists to kill.

WHAT A GREEN RUN MEANS — AND WHAT IT DOES NOT
---------------------------------------------
Green means: **the real entry point, driven end-to-end, wrote only where the unit grants, and
produced its artifacts.** It does NOT mean the backup works against a real Postgres/Neo4j/B2 —
``docker``, ``rclone`` and ``zstd`` are stubbed (there is no daemon, no network and no B2 in
CI). The stubs are faithful to the *contract* each real tool has with the script, and they are
where the harness's remaining blind spots live. They are enumerated in ``_STUBS`` below, in
their own words.

One residual gap, stated plainly: a write outside the grant that the script *attempts, fails,
and swallows* (``> /tmp/x 2>/dev/null || true``) is invisible to an exit-code oracle. That is
why the caller asserts on **three** things and not one — the exit code, the **artifacts the run
left behind**, and the **absence of a false claim** in its output. deploy#613 was precisely a
swallowed failure, and it is caught here by the second and third of those, not the first.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SYSTEMD_DIR = REPO_ROOT / "systemd"

# Directories systemd's ProtectSystem= leaves alone (systemd.exec(5)). Not a policy choice of
# ours — a restatement of what the directive means, which is unavoidable when emulating it.
API_FILESYSTEMS = ("/proc", "/sys", "/dev")

# ProtectHome=yes makes these inaccessible (systemd.exec(5)).
HOME_PATHS = ("/home", "/root", "/run/user")

COMPOSE_FILE = REPO_ROOT / "compose" / "docker-compose.prod.yml"


def scraped_textfile_dir(compose_file: Path = COMPOSE_FILE) -> str:
    """The directory node-exporter ACTUALLY READS, out of compose. Never re-typed.

    deploy#565 is the bug where the backup gauge was written to `/var/lib/node_exporter` — the
    PARENT of the collector directory — and so was, in backup.sh's own words, "emitted faithfully
    and scraped never", for months, while the alert keyed off it could not fire.

    The first version of this harness asserted the gauge with a SUBSTRING match
    (`"isnad_backup_success.prom" in artifact`). That string is a substring of the WRONG path as
    well as the right one, so re-injecting deploy#565 verbatim left the gate GREEN — while the
    harness printed the wrong path in its own artifact listing (deploy#654 review — Nurul Hakim).

    The fix is not to hand-type the full path: that would be one more re-typed constant in a
    module whose entire thesis is *never re-type the constraint*. `--collector.textfile.directory`
    in compose is what decides which directory is scraped, so it is what the oracle reads. Change
    the flag and the assertion moves with it, in the same commit.
    """
    flag = "--collector.textfile.directory="
    for raw in compose_file.read_text().splitlines():
        if flag in raw:
            # Split on the flag FIRST, then strip the YAML quoting off the value. Stripping the
            # line first would eat the flag's own leading `--` along with the list dash.
            return raw.split(flag, 1)[1].strip().strip("\"',")
    raise Unsupported(
        f"no {flag} found in {compose_file}. The harness cannot tell which directory "
        f"node-exporter actually scrapes, so it CANNOT check that a gauge lands somewhere it "
        f"will be read — which is deploy#565 exactly. Refusing to guess."
    )


class Unsupported(Exception):
    """A unit directive we cannot faithfully emulate. Refuse to testify — never pass silently.

    This is the anti-drift core of the module. The moment the emulation and the unit can
    disagree, the harness must stop being an instrument and say so out loud.
    """


# Every directive that may appear in a unit we gate, and what this harness does with it.
#
# EMULATED  — changes what the process may write. Reproduced in the namespace.
# CONFIG    — tells the harness WHAT to run, not what to forbid. Consumed.
# INERT     — cannot affect where a shell script writes, and we say why. Ignored deliberately.
#
# A directive absent from this table raises Unsupported -> rc=2. That is deliberate and it is
# the reason a green run means anything: the harness cannot silently ignore a constraint that
# production applies. Adding a directive to a unit is therefore a two-file change — the unit
# and this table — and the CI gate will tell you so rather than drifting.
DIRECTIVES: dict[str, str] = {
    # --- hardening: emulated ---
    "ProtectSystem": "EMULATED",
    "ProtectHome": "EMULATED",
    "ReadWritePaths": "EMULATED",
    "PrivateTmp": "EMULATED",
    # --- config: consumed by the harness ---
    "ExecStart": "CONFIG",
    "WorkingDirectory": "CONFIG",
    "Environment": "CONFIG",
    "EnvironmentFile": "CONFIG",
    # --- inert for "where does this script write?", with the reason ---
    # No process here is setuid, so the no-new-privs bit changes nothing about reachable paths.
    "NoNewPrivileges": "INERT",
    # Restricts /dev to a minimal set. backup.sh writes no device node; /dev/null still works.
    "PrivateDevices": "INERT",
    # /proc/sys, /sys knobs, cgroup fs, SUID/SGID bits, personality(2). None is a path the
    # backup path writes, and none changes whether a write to /tmp or $HOME succeeds.
    "ProtectKernelTunables": "INERT",
    "ProtectKernelModules": "INERT",
    "ProtectControlGroups": "INERT",
    "RestrictSUIDSGID": "INERT",
    "LockPersonality": "INERT",
    # --- unit bookkeeping: no runtime effect on the process's filesystem view ---
    "Description": "INERT",
    "Documentation": "INERT",
    "After": "INERT",
    "Requires": "INERT",
    "OnFailure": "INERT",
    "Type": "INERT",
    "StandardOutput": "INERT",
    "StandardError": "INERT",
    "SyslogIdentifier": "INERT",
}


@dataclass
class Unit:
    """The hardening + config directives of a .service file, as written."""

    path: Path
    directives: dict[str, list[str]] = field(default_factory=dict)

    def one(self, key: str, default: str | None = None) -> str | None:
        vals = self.directives.get(key)
        return vals[-1] if vals else default

    @property
    def is_hardened(self) -> bool:
        return self.one("ProtectSystem") is not None

    @property
    def read_write_paths(self) -> list[str]:
        """Every path the unit GRANTS. Repeated ReadWritePaths= lines accumulate."""
        out: list[str] = []
        for line in self.directives.get("ReadWritePaths", []):
            # A leading '-' means "ignore if missing" (systemd.exec(5)); it is not part of the
            # path. Everything else is shell-like whitespace separation.
            out.extend(tok.lstrip("-") for tok in shlex.split(line))
        return out

    @property
    def exec_starts(self) -> list[str]:
        """EVERY ExecStart=, in order. Not the last one. (deploy#654 review — Nurul Hakim)

        This used to be `one("ExecStart")`, i.e. `vals[-1]` — the LAST command. Both our units
        are `Type=oneshot`, and systemd.service(5) is explicit that oneshot is exactly the type
        where **multiple ExecStart= lines are legal and run in sequence**. So a unit like

            ExecStart=/opt/noorinalabs-deploy/scripts/prestep.sh   <- writes /tmp, DIES on the box
            ExecStart=/opt/noorinalabs-deploy/scripts/backup.sh

        ran only `backup.sh`, reported PASS, and **never executed the command carrying the
        defect**. Nurul built that unit, put deploy#613's own bug in the first command, and the
        gate went green.

        That is the worst possible place for a silent skip, and the reason is structural: the
        DIRECTIVES table catches directives it does not KNOW. `ExecStart` is known — so a second
        one was not an instrument failure, it was a silently dropped command. The armour did not
        apply to the one directive that selects WHAT CODE RUNS AT ALL, which defeats this
        module's own doctrine ("never a silent pass") on the only directive where a silent pass
        means "we tested nothing".
        """
        lines = self.directives.get("ExecStart", [])
        if not lines:
            raise Unsupported(f"{self.path.name} has no ExecStart=")
        # ExecStart= may carry prefix characters (-, @, +, !) before the binary.
        return [shlex.split(line)[0].lstrip("-@+!") for line in lines]

    @property
    def environment(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for line in self.directives.get("Environment", []):
            for tok in shlex.split(line):
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    env[k] = v
        return env


def parse_unit(path: Path) -> Unit:
    """Read a .service file into directives, rejecting anything we cannot account for.

    Deliberately dumb: no systemd, no drop-ins, no specifier expansion. It reads the same bytes
    the box reads. If it meets a directive that is not in DIRECTIVES it raises — see Unsupported.
    """
    unit = Unit(path=path)
    in_service = False
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            in_service = line == "[Service]"
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not in_service:
            continue
        if key not in DIRECTIVES:
            raise Unsupported(
                f"{path.name} sets {key}= and this harness does not know what that means for "
                f"where the process may write.\n"
                f"It will NOT guess, and it will NOT report green over an emulation that no "
                f"longer matches the unit — that is exactly how deploy#613 shipped twice.\n"
                f"Teach it: add {key!r} to DIRECTIVES in {Path(__file__).name} as EMULATED "
                f"(and implement it) or INERT (and say why it cannot affect a write)."
            )
        unit.directives.setdefault(key, []).append(value)
    return unit


def hardened_units(systemd_dir: Path = SYSTEMD_DIR) -> list[Path]:
    """Every hardened unit on the box, discovered — never hard-coded.

    Add a unit tomorrow and it is gated from that moment, without anyone remembering to add it
    to a list here. An UNhardened unit has a writable /tmp and is genuinely not in scope.
    """
    found = []
    for unit in sorted(systemd_dir.glob("*.service")):
        try:
            if parse_unit(unit).is_hardened:
                found.append(unit)
        except Unsupported:
            # An unparseable unit is NOT a reason to skip it — it is the loudest possible
            # reason to look at it. Surface it to the caller, which will exit 2.
            found.append(unit)
    return found


# ---------------------------------------------------------------------------
# The stubs. Every external binary the entry points shell out to.
# ---------------------------------------------------------------------------
# There is no docker daemon, no B2 bucket and no network in CI, so these three cannot be real.
# They are the harness's blind spot and they are listed here rather than buried:
#
#   docker  — a HEALTHY stack. If the script under test blames docker, that accusation is
#             coming from its own broken scratch, not from docker. (deploy#623's whole shape.)
#   rclone  — a working, write-capable, delete-capable B2 key. Same logic: a KEY_INVALID
#             verdict out of this harness is never about the key. (deploy#613's whole shape.)
#   zstd    — real zstd is not installed on every dev box, and a gate that only runs where a
#             tool happens to be installed is the non-hermetic trap this whole issue is about.
#
# What the stubs CANNOT catch: a write the REAL tool would make outside the grant (rclone
# writing ~/.config/rclone/rclone.conf, say). The rclone stub pins the one property that keeps
# that from mattering today — see its RCLONE_CONFIG_ISNAD_* assertion, which is load-bearing
# and, until now, written down nowhere.
_STUBS: dict[str, str] = {
    "docker": r"""#!/usr/bin/env bash
# A HEALTHY docker. Any complaint about the daemon from the script under test is therefore
# a complaint about the script's own scratch, which is the entire point of the harness.
set -uo pipefail
STATE="${BACKUP_DIR:?BACKUP_DIR unset in docker stub}/.harness-docker-state"
[[ -f "$STATE" ]] || echo "neo4j=running" > "$STATE"
neo4j_state() { sed -n 's/^neo4j=//p' "$STATE"; }
set_neo4j() { echo "neo4j=$1" > "$STATE"; }

if [[ "${1:-}" == "compose" ]]; then
    shift
    # Consume `-p <proj> -f <file>`; the script always passes both (compose_project.sh dc()).
    while [[ "${1:-}" == -* ]]; do
        case "$1" in
            -p|-f) shift 2 ;;
            *) shift ;;
        esac
    done
    sub="${1:-}"; shift || true
    case "$sub" in
        ps)
            if [[ "${1:-}" == "-aq" ]]; then echo "harness-neo4j-cid"; exit 0; fi
            fmt=""
            [[ "${1:-}" == "--format" ]] && fmt="${2:-}"
            if [[ "$fmt" == *".Health"* ]]; then
                [[ "$(neo4j_state)" == "running" ]] && echo "neo4j:healthy"
                exit 0
            fi
            # Default: the {{.Service}}:{{.State}} listing.
            echo "postgres:running"
            echo "user-postgres:running"
            if [[ "$(neo4j_state)" == "running" ]]; then
                echo "neo4j:running"
            else
                echo "neo4j:exited"
            fi
            exit 0
            ;;
        exec)
            # `exec -T <svc> pg_dump …` — emit a non-empty dump on stdout. backup.sh rejects a
            # zero-byte dump, so an empty stub would look like a broken Postgres, not a pass.
            printf 'PGDMP\x00harness-fake-custom-format-dump\n'
            exit 0
            ;;
        stop)  set_neo4j exited;  exit 0 ;;
        start) set_neo4j running; exit 0 ;;
        *)     exit 0 ;;
    esac
fi

case "${1:-}" in
    inspect) echo "harness_neo4j_data"; exit 0 ;;
    run)
        # `docker run … -v <LOCAL_BACKUP_PATH>:/backups … neo4j-admin database dump`.
        # Write the dump where the REAL neo4j-admin would: the host side of the /backups bind.
        backups=""
        while [[ $# -gt 0 ]]; do
            if [[ "$1" == "-v" && "${2:-}" == *":/backups" ]]; then backups="${2%%:/backups}"; fi
            shift
        done
        [[ -n "$backups" ]] || { echo "stub: no :/backups bind in docker run" >&2; exit 1; }
        printf 'harness-fake-neo4j-dump\n' > "${backups}/neo4j.dump"
        exit 0
        ;;
esac
exit 0
""",
    "rclone": r"""#!/usr/bin/env bash
# A working, write-capable, delete-capable B2 key.
#
# THE ASSERTION BELOW IS THE POINT OF THIS STUB, AND IT IS WRITTEN DOWN NOWHERE ELSE.
#
# The unit runs ProtectHome=yes, so $HOME is INACCESSIBLE. Real rclone reads its config from
# ~/.config/rclone/rclone.conf — and it escapes ProtectHome today ONLY because backup.sh
# configures it entirely through RCLONE_CONFIG_ISNAD_* environment variables and never writes
# a config file. That is load-bearing and it is one refactor away from being the next defect:
# swap the env vars for `rclone config create` and every backup dies under the unit, with
# rclone blaming the credential.
#
# So the stub REFUSES to answer unless it was configured the way that survives the hardening.
# A refactor to a config file turns this into a hard failure instead of a mystery. (Nino
# Kavtaradze raised this in the #624 review; nobody had written it down.)
set -uo pipefail
for v in RCLONE_CONFIG_ISNAD_TYPE RCLONE_CONFIG_ISNAD_ACCOUNT RCLONE_CONFIG_ISNAD_KEY; do
    if [[ -z "${!v:-}" ]]; then
        # A SENTINEL FILE, not just a message on stderr.
        #
        # b2_preflight runs `rclone lsd … > "$lsd_out" 2>&1` — it captures BOTH streams into a
        # scratch file it then parses. So a complaint on stderr is swallowed whole, and all the
        # caller sees is a non-zero rc, which its classifier renders as `verdict=KEY_INVALID`:
        # a statement about a CREDENTIAL, caused by a refactor that never touched one. That is
        # deploy#613's exact shape, one level up, and it is why the stub's objection must land
        # somewhere the capture cannot eat. BACKUP_DIR is granted, and the harness lists what it
        # finds there as an artifact.
        echo "rclone stub: ${v} is not set." >&2
        echo "  The caller is no longer configuring rclone through the environment. Under" >&2
        echo "  ProtectHome=yes the unit CANNOT read ~/.config/rclone/rclone.conf, so a" >&2
        echo "  config-file-based rclone breaks every backup on the box. Keep the env form." >&2
        : > "${BACKUP_DIR:-/dev/null}/.harness-rclone-unconfigured" 2>/dev/null || true
        exit 3
    fi
done
if [[ -n "${RCLONE_DUMP:-}" ]]; then
    echo "rclone stub: RCLONE_DUMP is set — it leaks credentials into logs. Refusing." >&2
    exit 3
fi

case "${1:-}" in
    lsd)
        # b2_preflight reads the LAST field of each line as a bucket name.
        printf '          -1 2026-01-01 00:00:00        -1 %s\n' "${B2_BUCKET:?}"
        ;;
    lsf)   ;;  # no pre-existing date dirs -> the retention prune has nothing to purge
    copyto|deletefile|copy|purge) ;;
    *) ;;
esac
exit 0
""",
    "zstd": r"""#!/usr/bin/env bash
# `zstd -3 --rm <in> -o <out>` — write <out>, remove <in>. Writes exactly where real zstd
# writes, which is all this harness observes about it.
set -uo pipefail
inp=""; out=""; rm_src=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o) out="${2:-}"; shift 2 ;;
        --rm) rm_src=1; shift ;;
        -*) shift ;;
        *) inp="$1"; shift ;;
    esac
done
[[ -n "$inp" && -n "$out" ]] || { echo "zstd stub: need <in> -o <out>" >&2; exit 1; }
cp -- "$inp" "$out"
[[ "$rm_src" -eq 1 ]] && rm -f -- "$inp"
exit 0
""",
    "systemd-cat": r"""#!/usr/bin/env bash
# There is no journal in the sandbox. emit-backup-failure-marker.sh pipes its journal line
# through systemd-cat under `set -o pipefail`, so an absent binary would break the pipeline and
# be read as a failure of the marker script. Drain stdin; the .prom file is the assertable part.
set -uo pipefail
cat > /dev/null
exit 0
""",
}


def _write_stubs(stub_dir: Path) -> None:
    stub_dir.mkdir(parents=True, exist_ok=True)
    for name, body in _STUBS.items():
        p = stub_dir / name
        p.write_text(body)
        p.chmod(0o755)


def sandbox_backend() -> list[str] | None:
    """How to get a private mount namespace on THIS box — or None, which means rc=2.

    Two backends, and the order matters:

      1. ROOTLESS (`unshare --user --map-root-user --mount`). The one that matters, because it
         is the one a developer gets from `pre-commit` with no sudo and no ceremony. A gate that
         only runs in CI is a gate people discover after they push.
      2. ROOT (`unshare --mount`). Ubuntu 24.04 can refuse (1) outright —
         `kernel.apparmor_restrict_unprivileged_userns=1` blocks unprivileged user namespaces —
         and ubuntu-latest is 24.04. When the job already runs as root, no user namespace is
         needed at all.

    PROBED, not assumed: each candidate is actually executed. `unshare` being on PATH says
    nothing about whether the kernel will hand you a namespace, and "the binary exists" is the
    kind of proxy that produces a green tick over a check that never ran.

    None means we could not get a namespace. The caller MUST turn that into rc=2 — never 0.
    """
    # `unshare` absent entirely is a missing instrument, not a crash. Bereket Tadesse removed the
    # binary for real in the #654 review; callers (including this module's test suite, at import)
    # must get a clean None rather than a FileNotFoundError out of the probe.
    if shutil.which("unshare") is None:
        return None

    candidates = [
        ["unshare", "--user", "--map-root-user", "--mount"],
        *([["unshare", "--mount"]] if os.geteuid() == 0 else []),
    ]
    for argv in candidates:
        try:
            probe = subprocess.run([*argv, "true"], capture_output=True, text=True)
        except OSError:
            continue
        if probe.returncode == 0:
            return argv
    return None


# ---------------------------------------------------------------------------
# The sandbox
# ---------------------------------------------------------------------------


@dataclass
class Sandbox:
    """A mount plan derived from a unit. Every field traces to a directive, or it is a bug."""

    rw_paths: list[str]
    masked_paths: list[str]  # ProtectHome=yes
    private_tmp: bool
    workdir: str
    exec_starts: list[str]  # ALL of them, in order — Type=oneshot runs them in sequence
    deploy_root: str
    environment: dict[str, str]

    def granted(self, path: str) -> bool:
        """Does the unit's ReadWritePaths= cover this path?

        THE ONE RULE. Everything else in the sandbox falls out of it: the repo tree is
        read-write under isnad-backup.service (which grants /opt/noorinalabs-deploy) and
        READ-ONLY under isnad-backup-failure-marker.service (which does not). Nothing
        special-cases either unit — they just have different ReadWritePaths=, and the sandbox
        reads it.
        """
        p = Path(path)
        return any(p == Path(rw) or Path(rw) in p.parents for rw in self.rw_paths)

    @property
    def denied_probe_paths(self) -> list[str]:
        """Paths the sandbox must REFUSE to write, derived from the grant list.

        Not a hand-typed "/tmp must be read-only" — that is the third-place restatement this
        whole module exists to avoid. These are simply paths the unit does NOT grant. Add /tmp
        to ReadWritePaths= and it drops out of here automatically, because it would then be
        writable in production too.
        """
        candidates = ["/etc", "/usr", "/var", *([] if self.private_tmp else ["/tmp"])]
        # The parent of each granted path: production grants the CHILD, not the parent, and a
        # sandbox that let the parent through would be strictly LESS restrictive than the unit.
        candidates += [str(Path(p).parent) for p in self.rw_paths]
        # And the repo tree itself, when the unit does not grant it — which is exactly the case
        # for isnad-backup-failure-marker.service.
        candidates.append(self.deploy_root)
        return [c for c in dict.fromkeys(candidates) if not self.granted(c)]


def plan(unit: Unit) -> Sandbox:
    """Turn a parsed unit into a mount plan. Every unemulatable value raises."""
    protect_system = unit.one("ProtectSystem")
    if protect_system != "strict":
        raise Unsupported(
            f"{unit.path.name} has ProtectSystem={protect_system!r}; this harness emulates "
            f"'strict' only.\n"
            f"'yes' and 'full' make a DIFFERENT set of paths read-only, so running under a "
            f"'strict' sandbox would test something production does not do. Refusing to "
            f"testify. Implement the value in plan(), or accept that the gate is red."
        )

    protect_home = unit.one("ProtectHome", "no")
    if protect_home not in ("yes", "no"):
        raise Unsupported(
            f"{unit.path.name} has ProtectHome={protect_home!r}; this harness emulates "
            f"'yes' and 'no' only ('read-only' and 'tmpfs' are not implemented)."
        )

    private_tmp = unit.one("PrivateTmp", "no") == "yes"

    rw = unit.read_write_paths
    if not rw:
        raise Unsupported(
            f"{unit.path.name} is ProtectSystem=strict with NO ReadWritePaths= — it cannot "
            f"write anywhere at all. That is almost certainly not what was meant."
        )

    # Where the repo has to be for ExecStart= to resolve at all. The units name an absolute
    # path — /opt/noorinalabs-deploy/scripts/backup.sh — so the harness must site the tree
    # there. Derived from ExecStart and this repo's layout (scripts/ sits at the root), never
    # from a hard-coded "/opt/noorinalabs-deploy": the self-check below re-verifies by asserting
    # ExecStart is executable inside the sandbox, so a wrong derivation is rc=2, not a lie.
    exec_starts = unit.exec_starts
    roots = {str(Path(e).parent.parent) for e in exec_starts}
    if len(roots) != 1:
        raise Unsupported(
            f"{unit.path.name}: its ExecStart= commands live under different roots "
            f"({sorted(roots)}). The harness sites ONE tree, so it cannot run them all. Refusing "
            f"to run a subset — that is the silent skip this whole module exists to prevent."
        )
    deploy_root = roots.pop()
    if deploy_root == "/":
        raise Unsupported(
            f"{unit.path.name}: ExecStart={exec_starts[0]} is not <root>/scripts/<script>, so the "
            f"harness cannot work out where to site the repo."
        )

    # systemd runs a unit with no WorkingDirectory= from `/`. backup.sh resolves COMPOSE_FILE
    # relatively and therefore sets one; the failure-marker script uses absolute paths and does
    # not. Take whichever the unit says — including its absence.
    workdir = (unit.one("WorkingDirectory") or "/").rstrip("/") or "/"

    return Sandbox(
        rw_paths=rw,
        masked_paths=list(HOME_PATHS) if protect_home == "yes" else [],
        private_tmp=private_tmp,
        workdir=workdir,
        exec_starts=exec_starts,
        deploy_root=deploy_root,
        environment=unit.environment,
    )


def _nearest_existing_ancestor(path: str) -> str:
    p = Path(path)
    for parent in p.parents:
        if parent.exists():
            return str(parent)
    return "/"


def render_namespace_script(sb: Sandbox, repo_root: Path) -> str:
    """The bash program that BUILDS the namespace and then execs the entry point.

    Read the ordering as the argument it is:

      1. Seal EVERYTHING read-only            <- ProtectSystem=strict, in one line
      2. Carve the granted paths back out     <- ReadWritePaths=, and nothing else
      3. Mask $HOME                           <- ProtectHome=yes
      4. SELF-CHECK, and refuse to run if the sandbox is not what the unit says  <- rc=2
      5. exec the real ExecStart

    Step 4 is not decoration. An instrument that has not been checked against both classes —
    "the granted path IS writable" and "the ungranted path is NOT" — is exactly the instrument
    that shipped deploy#613 twice (cf. feedback_calibrate_the_mutation_before_counting_it,
    feedback_silent_zero_is_not_a_measurement).
    """
    q = shlex.quote
    lines: list[str] = [
        "set -uo pipefail",
        "",
        "# rc=2 is 'could not find out'. It must NEVER be confused with a clean run.",
        'instrument_fail() { echo "[harness] INSTRUMENT FAILURE: $*" >&2; exit 2; }',
        "",
        "mount --make-rprivate / || instrument_fail 'cannot make mounts private'",
        "",
    ]

    # --- 2a. Substrate for the granted paths, BEFORE the read-only pass. -------------------
    # A granted path usually does not exist on a CI runner (/var/lib/noorinalabs-backups), and
    # its parent is root-owned, so we cannot mkdir it even as namespace-root. So: tmpfs the
    # nearest EXISTING ancestor, create the path inside it, then give the path its own writable
    # tmpfs and seal the ancestor read-only again. The ancestor ends up read-only exactly as
    # production has it — a tmpfs left writable there would make the sandbox LESS restrictive
    # than the unit, which is a false-green hole.
    # Every path that needs a mountpoint: the granted paths, plus wherever the repo has to sit
    # for ExecStart= to resolve (whether or not the unit grants the repo read-write).
    needs_mountpoint = [*sb.rw_paths, sb.deploy_root]
    ancestors: list[str] = []
    for path in needs_mountpoint:
        anc = _nearest_existing_ancestor(path)
        if anc == "/":
            raise Unsupported(
                f"{path} has no existing ancestor below /. The harness would have to tmpfs the "
                f"root filesystem to create it, which would hide the very tree under test. "
                f"Create the parent on the runner, or use a path under one that exists."
            )
        if anc not in ancestors:
            ancestors.append(anc)

    lines.append("# --- Mountpoints. A granted path rarely exists on a CI runner, and its ---")
    lines.append("# --- parent is root-owned, so even namespace-root cannot mkdir it. tmpfs ---")
    lines.append("# --- the nearest existing ancestor and build inside it. The ancestor is  ---")
    lines.append("# --- NOT granted, and the read-only pass below seals it right back up.   ---")
    for anc in ancestors:
        lines.append(f"mount -t tmpfs tmpfs {q(anc)} || instrument_fail 'tmpfs on {anc}'")
    for path in needs_mountpoint:
        lines.append(f"mkdir -p {q(path)} || instrument_fail 'mkdir {path}'")

    lines += [
        "",
        "# The tree ExecStart= lives in. Bound READ-WRITE here; if the unit does not grant it",
        "# (isnad-backup-failure-marker.service does not), the read-only pass below takes it",
        "# straight back — because it is not in ReadWritePaths=. One rule, no special cases.",
        f"mount --bind {q(str(repo_root))} {q(sb.deploy_root)} "
        f"|| instrument_fail 'bind repo at {sb.deploy_root}'",
        "",
        "# --- ReadWritePaths=: the ONLY writable paths, straight out of the unit ---",
    ]
    for rw in sb.rw_paths:
        if rw == sb.deploy_root:
            continue  # already the repo bind, and it is granted -> stays read-write
        lines.append(f"mount -t tmpfs tmpfs {q(rw)} || instrument_fail 'tmpfs on granted {rw}'")

    # --- 2b. ProtectSystem=strict: everything else read-only. -----------------------------
    lines += [
        "",
        "# --- ProtectSystem=strict: the whole hierarchy read-only, except the API fs ---",
        "# THIS IS THE ONE RULE, AND IT IS THE UNIT'S: everything the unit does not grant in",
        "# ReadWritePaths= is read-only. The ancestors we tmpfs'd above, the repo tree when it",
        "# is not granted, /tmp, /etc, everything.",
        "#",
        "# Deepest-first, so a child is sealed before its parent. Anything that refuses to go",
        "# read-only is DETACHED instead (strictly more restrictive; can only false-RED). If it",
        "# will do neither, we do not have the unit's sandbox and we must not pretend we do.",
        "while IFS= read -r mp; do",
        '  [ -z "$mp" ] && continue',
        '  case "$mp" in',
        "    " + "|".join(f"{p}|{p}/*" for p in API_FILESYSTEMS) + ") continue ;;",
    ]
    for rw in sb.rw_paths:
        lines.append(f"    {rw}|{rw}/*) continue ;;")
    lines += [
        "  esac",
        '  mount -o remount,bind,ro "$mp" 2>/dev/null && continue',
        '  umount -l "$mp" 2>/dev/null && continue',
        '  instrument_fail "could not make $mp read-only or detach it — the sandbox is not the'
        " unit's, so nothing it reports would mean anything\"",
        "done <<EOF",
        "$(awk '{print $5}' /proc/self/mountinfo | sed 's/\\\\040/ /g' "
        "| awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2-)",
        "EOF",
    ]

    # --- 3. PrivateTmp / ProtectHome ------------------------------------------------------
    if sb.private_tmp:
        lines += [
            "",
            "# PrivateTmp=yes — a private, WRITABLE /tmp. (The unit does not set this today;",
            "# deploy#121 Bug A explains why. If it ever does, a bare mktemp becomes legal and",
            "# this harness will say so, because it reads the unit rather than a rule.)",
            "mount -t tmpfs tmpfs /tmp || instrument_fail 'PrivateTmp tmpfs'",
            "mount -t tmpfs tmpfs /var/tmp || instrument_fail 'PrivateTmp tmpfs (/var/tmp)'",
        ]
    if sb.masked_paths:
        lines += ["", "# ProtectHome=yes — $HOME inaccessible."]
        for hp in sb.masked_paths:
            lines.append(
                f"[ -d {q(hp)} ] && {{ mount -t tmpfs -o ro,mode=0000 tmpfs {q(hp)} "
                f"|| instrument_fail 'mask {hp}'; }}"
            )

    # --- 4. Self-check: calibrate the instrument before reading it. ------------------------
    lines += [
        "",
        "# --- SELF-CHECK. Prove the sandbox separates granted from ungranted, BOTH ways. ---",
        "# A sandbox that refuses every write would pass every negative assertion and look",
        "# like a perfect gate. A sandbox that allows every write would pass the positive one.",
        "# Require BOTH, or this is theatre.",
    ]
    for rw in sb.rw_paths:
        lines.append(
            f"touch {q(rw + '/.harness-selfcheck')} 2>/dev/null "
            f"|| instrument_fail 'the unit GRANTS {rw} (ReadWritePaths=) but the sandbox will "
            f"not let us write there — the sandbox is broken, not the script'"
        )
        lines.append(f"rm -f {q(rw + '/.harness-selfcheck')}")
    for denied in sb.denied_probe_paths:
        lines.append(
            f"if touch {q(denied + '/.harness-selfcheck')} 2>/dev/null; then "
            f"rm -f {q(denied + '/.harness-selfcheck')}; "
            f"instrument_fail 'the unit does NOT grant {denied}, but the sandbox let us write "
            f"there. It is LAXER than production: a script writing {denied} would pass here and "
            f"die on the box. Refusing to testify.'; fi"
        )
    if sb.masked_paths:
        lines.append(
            f"[ -e {q(sb.masked_paths[0] + '/.harness-selfcheck')} ] "
            f"&& instrument_fail 'ProtectHome mask did not take'"
        )
    for ex in sb.exec_starts:
        lines.append(
            f"[ -x {q(ex)} ] || instrument_fail 'ExecStart {ex} is not executable inside the "
            f"sandbox — the repo bind failed, or the unit names a script that is not there'"
        )

    # --- 5. Run it. -----------------------------------------------------------------------
    lines += [
        "",
        "echo '[harness] sandbox verified: granted paths writable, ungranted paths refused.'",
        f"# WorkingDirectory={sb.workdir} (systemd uses / when the unit does not set one).",
        f"cd {q(sb.workdir)} || instrument_fail 'cd {sb.workdir}'",
        "",
        "# The entry point's own exit status is DATA, not the harness's verdict. Capture it,",
        "# report it on a marker line, and let the caller decide. A missing marker means the",
        "# namespace script itself died -> the caller reads that as rc=2, could-not-find-out.",
        "#",
        "# EVERY ExecStart=, IN ORDER — Type=oneshot runs them in sequence, and stops at the",
        "# first one that fails (systemd.service(5)). Taking only the LAST one silently skipped",
        "# any command before it, so a defect in the FIRST ExecStart was never executed and the",
        "# gate went green over it (deploy#654 review — Nurul Hakim).",
        "exec_rc=0",
    ]
    for ex in sb.exec_starts:
        lines += [
            "if [ $exec_rc -eq 0 ]; then",
            f"  echo '[harness] ExecStart: {ex}'",
            f"  {q(ex)} || exec_rc=$?",
            "else",
            f"  echo '[harness] ExecStart NOT REACHED (a previous one failed): {ex}'",
            "fi",
        ]
    lines += [
        "",
        "# What the run LEFT BEHIND, listed from INSIDE the namespace — it is torn down on exit,",
        "# so this is the caller's only window onto the product. An exit code alone cannot see a",
        "# write that failed, was swallowed, and quietly corrupted the output; deploy#613 was",
        "# exactly that, and it is caught by looking at the artifacts and the verdict text, not",
        "# by looking at $?.",
        "#",
        "# The repo tree is deliberately NOT listed: the harness pre-populated it, so its",
        "# contents are not evidence of anything this run did.",
    ]
    for rw in sb.rw_paths:
        if rw == sb.deploy_root:
            continue
        lines.append(f"find {q(rw)} -type f -printf '[harness] artifact %p\\n' 2>/dev/null || true")
    lines += [
        'echo "[harness] exec_rc=${exec_rc}"',
        "exit 0",
    ]
    return "\n".join(lines) + "\n"


@dataclass
class Result:
    rc: int  # 0 clean | 1 finding | 2 could-not-find-out
    exec_rc: int | None
    stdout: str
    stderr: str
    artifacts: list[str]

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def run_unit_under_hardening(
    unit_path: Path,
    repo_root: Path,
    extra_env: dict[str, str] | None = None,
    timeout: int = 300,
) -> Result:
    """Run `unit_path`'s ExecStart under `unit_path`'s own hardening. See module docstring."""
    backend = None if shutil.which("unshare") is None else sandbox_backend()
    if backend is None:
        return Result(
            2,
            None,
            "",
            "[harness] INSTRUMENT FAILURE: this box will not give us a private mount namespace, "
            "so the unit's sandbox cannot be built and this check CANNOT RUN.\n"
            "  Tried: unshare --user --map-root-user --mount (rootless)"
            + ("" if os.geteuid() == 0 else "; run as root for the second backend")
            + "\n"
            "  On Ubuntu 24.04 unprivileged user namespaces may be blocked by AppArmor:\n"
            "    sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0\n"
            "  ...or run this as root, which needs no user namespace at all.\n"
            "It is reporting rc=2 (COULD NOT FIND OUT), not rc=0. A gate that skips green when "
            "its instrument is missing is the false green this check exists to kill — that is "
            "the whole of deploy#613/#623, and it applies to this check too.\n",
            [],
        )

    unit = parse_unit(unit_path)  # raises Unsupported -> caller maps to rc=2
    sb = plan(unit)

    # The stubs live INSIDE the repo tree, not in /tmp: /tmp is read-only in the sandbox (and,
    # under PrivateTmp=yes, a fresh empty tmpfs — the stubs would simply vanish).
    stub_dir = repo_root / ".harness-stubs"
    _write_stubs(stub_dir)

    # systemd hands the process Environment= and the contents of EnvironmentFile= as plain
    # process environment. EnvironmentFile is a CONFIG directive — it constrains nothing — so
    # the harness supplies its variables directly, exactly as systemd would have.
    env = {
        "PATH": f"{sb.deploy_root}/.harness-stubs:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        # A root service's HOME. It is MASKED by ProtectHome=yes — that is the point.
        "HOME": "/root",
        "LANG": "C",
        **sb.environment,
        **(extra_env or {}),
    }

    script = render_namespace_script(sb, repo_root)
    proc = subprocess.run(
        # --noprofile --norc: systemd execve()s ExecStart directly. It does not run a shell that
        # sources /etc/bash.bashrc and ~/.bashrc first — but bash here DID, which (a) printed a
        # spurious "/root/.bashrc: Permission denied" on every run once ProtectHome masked $HOME,
        # and (b) let the DEV BOX'S shell config — aliases, exports, a stray PATH — leak into a
        # run whose entire purpose is to reproduce production exactly. A harness that runs prod
        # code under someone's dotfiles is not running prod code.
        [*backend, "bash", "--noprofile", "--norc", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )

    artifacts = [
        ln.removeprefix("[harness] artifact ")
        for ln in proc.stdout.splitlines()
        if ln.startswith("[harness] artifact ")
    ]
    marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("[harness] exec_rc=")]

    if proc.returncode == 2 or not marker:
        # Either the namespace script said so, or it died before it could say anything. Both
        # are "we could not find out", and neither may be rendered as a pass.
        return Result(2, None, proc.stdout, proc.stderr, artifacts)

    exec_rc = int(marker[-1].removeprefix("[harness] exec_rc="))
    return Result(0 if exec_rc == 0 else 1, exec_rc, proc.stdout, proc.stderr, artifacts)


# The environment the unit gets from EnvironmentFile=/opt/noorinalabs-deploy/.env on the box.
# Values are fake; the stubs accept any. BACKUP_PREFIX is `:?`-required by backup.sh (deploy#632).
HARNESS_ENV = {
    "B2_KEY_ID": "harness-key-id",
    "B2_APP_KEY": "harness-app-key",
    "B2_BUCKET": "harness-bucket",
    "BACKUP_PREFIX": "stg",
}


def stage_repo(dest: Path, source: Path = REPO_ROOT) -> Path:
    """A copy of the tree the unit executes.

    A COPY, not the worktree itself. The unit grants /opt/noorinalabs-deploy read-write, so a
    script under test — or a deliberately broken fixture of one — can legitimately write into
    the tree it is running from. A harness that let that land in the real checkout would be
    editing the code it is testing.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for sub in ("scripts", "systemd", "compose"):
        src = source / sub
        if src.is_dir():
            shutil.copytree(src, dest / sub, dirs_exist_ok=True)
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "units",
        nargs="*",
        type=Path,
        help="unit files to gate (default: every hardened unit in systemd/)",
    )
    args = ap.parse_args(argv)

    units = args.units or hardened_units()
    if not units:
        print(
            "[harness] INSTRUMENT FAILURE: no hardened units found in systemd/. This gate "
            "found nothing to check, which is not the same as finding nothing wrong.",
            file=sys.stderr,
        )
        return 2

    worst = 0
    for unit in units:
        print(f"\n=== {unit} ===", flush=True)
        with tempfile.TemporaryDirectory() as td:
            repo = stage_repo(Path(td) / "opt")
            try:
                res = run_unit_under_hardening(unit, repo, extra_env=dict(HARNESS_ENV))
            except Unsupported as exc:
                print(f"[harness] INSTRUMENT FAILURE: {exc}", file=sys.stderr)
                worst = max(worst, 2)
                continue
        sys.stdout.write(res.stdout)
        sys.stderr.write(res.stderr)
        verdict = {0: "PASS", 1: "FAIL", 2: "COULD NOT FIND OUT"}[res.rc]
        print(f"[harness] {unit.name}: {verdict} (exec_rc={res.exec_rc})")
        # 2 outranks 1: an instrument we do not trust cannot be allowed to report a finding.
        worst = 2 if 2 in (worst, res.rc) else max(worst, res.rc)

    return worst


if __name__ == "__main__":
    sys.exit(main())
