"""The real backup path, run under the real unit's real hardening — and PROVEN able to fail.

deploy#626 / deploy#627.

READ THIS FIRST, BECAUSE IT IS THE WHOLE POINT
----------------------------------------------
deploy#613 and deploy#623 are the same bug, shipped twice, past a full green CI and four
reviewers. The tests did not miss it because they were weak. They missed it because **they were
structurally incapable of failing**: every runner, container and dev box has a writable ``/tmp``,
and the defect only exists where ``/tmp`` is read-only. A suite that cannot express a defect is
not a weak instrument, it is not an instrument at all.

So the first duty of THIS module is not to test ``backup.sh``. It is to prove that it CAN. That
is what the calibration tests below are: a fixture copy of a real script with the defect put
back, asserted to go **RED**. Seven of them, spanning the class:

  1. a bare ``mktemp``                      — deploy#613/#623's spelling
  2. a ``> /tmp/…`` redirect                — NOT mktemp; a text-scan for mktemp sees nothing
  3. a ``$HOME`` write under ProtectHome=   — NOT mktemp, NOT /tmp
  4. b2_preflight's scratch back on /tmp    — deploy#613 ITSELF, restored, end to end
  5. the success gauge in the un-scraped parent dir     — deploy#565 ITSELF, restored
  6. the failure marker in the un-scraped parent dir    — deploy#565, in the OnFailure= handler
  7. a defect in the FIRST of several ``ExecStart=``    — a command that was never even RUN

If only (1) went red, this module would have reimplemented ``test_scratch_under_hardening.py``'s
static pin at 100x the cost and closed nothing. (2), (3) and (4) are the reason it exists.

(5), (6) and (7) exist because the first version of this module SHIPPED WITH ALL THREE HOLES, and
two reviewers found them by running it. The oracle was a substring match, so a gauge written to
the un-scraped parent — deploy#565, "emitted faithfully and scraped never" — passed while the
harness printed the wrong path in its own artifact listing. And ``ExecStart`` was read as
``vals[-1]``, so on a ``Type=oneshot`` unit (which legally takes several) a defect in the FIRST
command was silently never executed. The lesson is the module's own: **an oracle is not calibrated
until someone has made it fail.** (Nurul Hakim + Bereket Tadesse, #654 review.)

AND THE SANDBOX IS DERIVED, NOT RESTATED
----------------------------------------
The constraint is READ OUT OF ``systemd/isnad-backup.service`` — ``ProtectSystem=``,
``ReadWritePaths=``, ``ProtectHome=``, ``PrivateTmp=``. A hand-written "read-only /tmp" fixture
would be a THIRD place to encode the same assumption, stale the moment someone edits the unit.

"Derived" is a claim, so it is TESTED, twice, and both tests are two-sided — the SAME mutation,
under two different units, must produce two different verdicts:

  * ``test_a_write_is_refused_or_allowed_according_to_ReadWritePaths`` — grant the path in the
    unit and the write passes; take the grant away and the identical write fails.
  * ``test_PrivateTmp_yes_makes_a_bare_mktemp_legal_because_production_would`` — a bare
    ``mktemp`` is a defect *because the unit does not set PrivateTmp*. Set it, and the harness
    says PASS — because production would too.

A test that only ever says NO proves nothing (feedback_calibrate_the_mutation_before_counting_it).
Both directions, or it is theatre.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import unit_hardening as uh

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REAL_UNIT = REPO_ROOT / "systemd" / "isnad-backup.service"
MARKER_UNIT = REPO_ROOT / "systemd" / "isnad-backup-failure-marker.service"

# The directory node-exporter ACTUALLY SCRAPES, read out of compose's
# `--collector.textfile.directory` — never hand-typed. See uh.scraped_textfile_dir().
SCRAPED_DIR = uh.scraped_textfile_dir()
SUCCESS_GAUGE = f"{SCRAPED_DIR}/isnad_backup_success.prom"
FAILURE_GAUGE = f"{SCRAPED_DIR}/isnad_backup_failure.prom"


# ---------------------------------------------------------------------------
# "Could not find out" is not "found nothing wrong" — AT THE PYTEST LEVEL TOO
# ---------------------------------------------------------------------------
# The module already distinguishes rc=2 (no namespace) from rc=1 (a finding). The TESTS did not:
# they asserted `rc == 0` / `rc == 1`, so on a box with no namespace every one of them failed with
# `assert 2 == 1` — rendering an INSTRUMENT failure as a FINDING.
#
# That red-lined `pytest (scripts)`, a shared gate, for every future PR touching scripts/ on a
# stock Ubuntu 24.04 box (kernel.apparmor_restrict_unprivileged_userns=1). Bereket Tadesse caught
# it, and named it exactly right: deploy#613 was a broken instrument reporting GREEN; this was a
# broken instrument reporting RED. The red direction is the safe one and it is still fatal, because
# a suite that fails on every unrelated push is a suite people delete — which is the "gate people
# route around" failure arriving through the other door.
#
# So: ONE probe, at session scope, and the two outcomes are treated as the different facts they are.
#
#   * CI, or HARNESS_REQUIRE_NAMESPACE=1  -> HARD FAIL. Never skip green where the gate must hold.
#   * anywhere else                        -> skip LOUDLY, naming the sysctl.
#
# A bare pytest.skip would be the false green this whole module is fighting. It is defensible ONLY
# because backup-hardening.yml runs these same tests with AppArmor lifted and with
# HARNESS_REQUIRE_NAMESPACE set, so they CANNOT skip there. That guarantee is load-bearing, so it
# is asserted, not assumed — see test_the_dedicated_workflow_cannot_skip_this_suite.
_BACKEND = uh.sandbox_backend()
_REQUIRED = bool(os.environ.get("CI") or os.environ.get("HARNESS_REQUIRE_NAMESPACE"))

_NO_NAMESPACE = (
    "no private mount namespace on this box, so the hardening harness CANNOT RUN.\n"
    "This is rc=2 — COULD NOT FIND OUT — not a pass and not a finding.\n"
    "Ubuntu 24.04 blocks unprivileged user namespaces by default:\n"
    "    sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0\n"
    "...or run as root. CI lifts it (backup-hardening.yml, compose-validate.yml) and CANNOT skip."
)

if _BACKEND is None and _REQUIRED:
    raise RuntimeError("HARD FAIL (CI or HARNESS_REQUIRE_NAMESPACE is set): " + _NO_NAMESPACE)

needs_namespace = pytest.mark.skipif(_BACKEND is None, reason=_NO_NAMESPACE)

# Sentences that claim something about a subsystem the run never actually reached. deploy#613
# said "your B2 key is invalid" about a good key; deploy#623 said "is the daemon running?" about
# a healthy daemon. In this harness docker and B2 are HEALTHY BY CONSTRUCTION, so any of these
# appearing in the output is the script testifying about a system it never touched.
FALSE_CLAIMS = (
    "verdict=KEY_INVALID",
    "verdict=KEY_READ_ONLY",
    "verdict=BUCKET_UNREACHABLE",
    "is the daemon running",
    "Cannot reach Docker Compose",
    "Could not START neo4j",
    "Neo4j is NOT running",
)


def _stage(tmp_path: Path) -> Path:
    """A private copy of the tree the unit executes. Never the worktree — see stage_repo()."""
    return uh.stage_repo(tmp_path / "opt", source=REPO_ROOT)


def _run(repo: Path, unit: Path = REAL_UNIT) -> uh.Result:
    return uh.run_unit_under_hardening(unit, repo, extra_env=dict(uh.HARNESS_ENV))


def assert_gauge_is_scraped(res: uh.Result, gauge: str) -> None:
    """THE GAUGE ORACLE. One function, called by the real tests AND by their calibration.

    Deliberately not duplicated into the calibration as a copy of the assertion: a paraphrased
    oracle can drift away from the real one and go on passing while the real one rots (see
    feedback_paraphrase_in_the_product). The calibration below wraps THIS function in
    `pytest.raises`, so what is proven able to fail is the very code that guards the real run.

    `gauge` is a FULL path under compose's `--collector.textfile.directory`. A substring match
    here was how deploy#565 walked through the first version of this suite.
    """
    assert gauge in res.artifacts, (
        f"the gauge is not at {gauge} — the directory node-exporter ACTUALLY READS "
        f"(compose: --collector.textfile.directory).\n"
        f"A .prom written anywhere else is emitted faithfully and scraped NEVER. That is "
        f"deploy#565: the alert keying off this metric could not fire, for months.\n"
        f"artifacts={res.artifacts}"
    )


def _mutate(repo: Path, script: str, anchor: str, injected: str) -> None:
    """Put a defect back into a FIXTURE COPY of a real script.

    The anchor is a line from the real file, so if that line is ever reworded this raises
    instead of silently injecting nothing and reporting a calibration that never ran. A
    calibration that cannot fire is the same false green as a test that cannot fail
    (feedback_calibrate_the_mutation_before_counting_it).
    """
    path = repo / "scripts" / script
    text = path.read_text()
    if anchor not in text:
        raise AssertionError(
            f"calibration anchor not found in {script}: {anchor!r}\n"
            f"The mutation would have injected NOTHING and this test would have 'passed' by "
            f"doing nothing at all. Re-anchor it on a line that exists."
        )
    path.write_text(text.replace(anchor, anchor + "\n" + injected, 1))


# ===========================================================================
# 0. THE INSTRUMENT ITSELF
# ===========================================================================


def test_the_harness_refuses_to_testify_when_it_cannot_build_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `unshare` => rc=2, NEVER rc=0.

    A gate that silently skips green when its instrument is missing is precisely the false green
    this whole issue exists to kill. rc=2 is "I could not find out" and it is a different fact
    from "nothing is wrong" — the same distinction compose_project.sh had to learn the hard way
    (service_is_running: rc=1 is about the stack, rc=2 is about us).
    """
    monkeypatch.setattr(uh.shutil, "which", lambda _name: None)
    res = uh.run_unit_under_hardening(REAL_UNIT, _stage(tmp_path))
    assert res.rc == 2, "a missing `unshare` must be an INSTRUMENT failure, not a pass"
    assert res.rc != 0
    assert "CANNOT RUN" in res.output


def test_an_unknown_directive_is_an_instrument_failure_not_a_silent_pass(tmp_path: Path) -> None:
    """Add a constraint the harness cannot emulate and it must STOP, not shrug.

    This is the anti-drift core. If someone adds `InaccessiblePaths=/var/lib/secrets` to the
    unit tomorrow, a harness that quietly ignored it would keep reporting green over a sandbox
    that no longer matches production — which is the exact shape of the original bug, one level
    up. The harness must say "I no longer know what production allows".
    """
    unit = tmp_path / "fixture.service"
    unit.write_text(
        REAL_UNIT.read_text() + "\nInaccessiblePaths=/var/lib/secrets\n",
    )
    with pytest.raises(uh.Unsupported, match="InaccessiblePaths"):
        uh.parse_unit(unit)


@needs_namespace
def test_every_hardened_unit_on_the_box_is_gated(tmp_path: Path) -> None:
    """The unit set is DISCOVERED from systemd/, never hand-listed.

    A hand-typed list drifts the moment someone adds a unit — and a new hardened unit with an
    unchecked scratch write is deploy#613 for the fourth time. Both of today's units must be
    found, and both must run clean.
    """
    units = uh.hardened_units()
    names = {u.name for u in units}
    assert names == {"isnad-backup.service", "isnad-backup-failure-marker.service"}, (
        f"the hardened-unit discovery does not see what is in systemd/: {sorted(names)}"
    )

    for unit in units:
        res = _run(_stage(tmp_path / unit.stem), unit=unit)
        assert res.rc == 0, (
            f"{unit.name}'s ExecStart FAILED under its own hardening (rc={res.rc}, "
            f"exec_rc={res.exec_rc}):\n{res.output}"
        )


# ===========================================================================
# 1. POSITIVE CONTROL — the real thing, unmutated, must go GREEN
# ===========================================================================


@needs_namespace
def test_the_real_backup_runs_clean_under_the_real_units_own_hardening(tmp_path: Path) -> None:
    """The baseline. Without it, every RED below is satisfied by a harness that always fails.

    This is also the honest test of the sandbox's fidelity in the OTHER direction: the sandbox
    hides more than the unit does in places (it tmpfs's the ancestors of the granted paths), and
    if that over-restriction broke the real script, it would show up right here as a false RED.
    It does not — backup.sh runs end to end.
    """
    res = _run(_stage(tmp_path))

    assert res.rc == 0, f"the REAL backup.sh failed under the REAL unit's hardening:\n{res.output}"
    assert res.exec_rc == 0

    # rc alone is not enough, and this is not belt-and-braces. deploy#613 was a write that failed
    # and was SWALLOWED; the exit code was collateral, the LIE was the product. So assert on what
    # the run actually produced, and on what it said.
    #
    # THE FULL PATH, NOT A SUBSTRING. (deploy#654 review — Nurul Hakim.)
    #
    # This was `any("isnad_backup_success.prom" in a for a in res.artifacts)`. That string is a
    # substring of the WRONG path as well as the right one, so deploy#565 — the gauge written to
    # `/var/lib/node_exporter`, the UN-SCRAPED PARENT of the collector dir, "emitted faithfully
    # and scraped never" for months — walked straight through it. The harness printed the wrong
    # path in its own artifact listing and passed. The evidence was in `res.artifacts`; the oracle
    # just did not look at it.
    #
    # SCRAPED_DIR comes from compose's `--collector.textfile.directory`, so this asserts the gauge
    # lands where node-exporter ACTUALLY READS, and it is not one more re-typed constant.
    assert_gauge_is_scraped(res, SUCCESS_GAUGE)
    for claim in FALSE_CLAIMS:
        assert claim not in res.output, (
            f"the run claimed {claim!r}. docker and B2 are HEALTHY in this harness by "
            f"construction, so this is the script testifying about a subsystem it never "
            f"reached — deploy#613/#623 exactly.\n{res.output}"
        )


@needs_namespace
def test_the_failure_marker_lands_where_node_exporter_actually_reads(tmp_path: Path) -> None:
    """The OnFailure= handler's ENTIRE PURPOSE, asserted. It was not. (Nurul Hakim, #654 review)

    `test_every_hardened_unit_on_the_box_is_gated` checked `rc == 0` for
    isnad-backup-failure-marker.service and NOTHING ELSE. The one thing that unit exists to do —
    land `isnad_backup_failure.prom` in the directory node-exporter reads — was never asserted,
    even though the harness already captured it in its artifact listing.

    This is the script that runs AFTER everything else is already broken. If it is wrong, the
    failure signal itself never arrives and you find out during the incident, from the silence.

    deploy#565 is exactly this bug in this exact file: it used to write to the PARENT directory,
    which node-exporter does not read, so the metric was emitted and scraped never.
    """
    res = _run(_stage(tmp_path), unit=MARKER_UNIT)

    assert res.rc == 0, f"the failure-marker unit failed under its own hardening:\n{res.output}"
    # This unit IS the OnFailure= handler. A marker written anywhere node-exporter does not read
    # means a failed backup raises no signal at all — and you learn that during the incident.
    assert_gauge_is_scraped(res, FAILURE_GAUGE)


@needs_namespace
def test_calibration_deploy565_the_un_scraped_parent_dir_goes_red(tmp_path: Path) -> None:
    """CALIBRATION for the two tests above: re-inject deploy#565 verbatim. Must go RED.

    Nurul's reproduction, turned into a permanent gate. Point `TEXTFILE_DIR` at
    `/var/lib/node_exporter` — the un-scraped PARENT — exactly as the code did before deploy#565.
    The gauge is still written. The run still exits 0. Prometheus still never sees it.

    Under the old substring oracle this passed. It must now fail, or the oracle is decoration.
    """
    repo = _stage(tmp_path)
    path = repo / "scripts" / "backup.sh"
    text = path.read_text()
    anchor = 'TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}"'
    assert anchor in text, "backup.sh no longer sites TEXTFILE_DIR the way this calibration mutates"
    parent = str(Path(SCRAPED_DIR).parent)
    path.write_text(text.replace(anchor, f'TEXTFILE_DIR="${{TEXTFILE_DIR:-{parent}}}"'))

    res = _run(repo)

    # The run still SUCCEEDS — that is the whole horror of deploy#565, and why an exit-code oracle
    # cannot see it. The gauge is written faithfully. It is simply written where nothing reads it.
    assert res.rc == 0, (
        "sanity: the deploy#565 mutation does not break the run, it breaks the SCRAPE"
    )
    assert any(a.endswith("isnad_backup_success.prom") for a in res.artifacts), (
        "sanity: the mutated run must still WRITE a gauge — otherwise this calibration is "
        "measuring a crash, not the un-scraped-directory defect."
    )
    assert SUCCESS_GAUGE not in res.artifacts, (
        "the mutation did not actually move the gauge — this calibration is inert."
    )

    # THE ORACLE MUST NOW REJECT IT — and it is the REAL oracle, the same function the real test
    # calls, not a paraphrase of it. A copied assertion could drift from the original and keep
    # passing while the original rotted (feedback_paraphrase_in_the_product).
    with pytest.raises(AssertionError, match="deploy#565"):
        assert_gauge_is_scraped(res, SUCCESS_GAUGE)


@needs_namespace
def test_calibration_the_failure_marker_in_the_wrong_dir_goes_red(tmp_path: Path) -> None:
    """And the failure-marker oracle must be able to fail too, or it is decoration.

    Same defect, the other unit: point the marker at the un-scraped parent. The `.prom` is still
    written, the unit still exits 0 — and the failure signal is invisible. deploy#565 lived in
    THIS file originally.
    """
    repo = _stage(tmp_path)
    path = repo / "scripts" / "emit-backup-failure-marker.sh"
    text = path.read_text()
    anchor = 'TEXTFILE_DIR="/var/lib/node_exporter/textfile_collector"'
    assert anchor in text, "emit-backup-failure-marker.sh no longer sites TEXTFILE_DIR this way"
    parent = str(Path(SCRAPED_DIR).parent)
    path.write_text(text.replace(anchor, f'TEXTFILE_DIR="{parent}"'))

    res = _run(repo, unit=MARKER_UNIT)
    assert res.rc == 0, "sanity: the mutation must not crash the marker — it misplaces its output"

    with pytest.raises(AssertionError, match="deploy#565"):
        assert_gauge_is_scraped(res, FAILURE_GAUGE)


@needs_namespace
def test_calibration_a_defect_in_the_FIRST_of_several_ExecStarts_goes_red(tmp_path: Path) -> None:
    """Nurul's blocker 2, turned into a gate. `Type=oneshot` legally takes SEVERAL ExecStart=.

    `Unit.one()` returned `vals[-1]` — the LAST ExecStart — so a unit like

        ExecStart=…/prestep.sh    <- writes the read-only /tmp; DIES on the box
        ExecStart=…/backup.sh

    ran only backup.sh and reported PASS. **The command carrying the defect was never executed.**

    This was the sharpest finding in the review, because of WHERE it lands: the DIRECTIVES table
    is armour against directives the harness does not KNOW. `ExecStart` is known — so a second one
    was not an instrument failure, it was a silently dropped command, on the single directive that
    selects what code runs at all.

    The fix runs all of them in order (systemd's own oneshot semantics), so the defect executes and
    the gate goes red.
    """
    repo = _stage(tmp_path)

    # A prestep carrying deploy#613's own defect: scratch in the read-only /tmp.
    prestep = repo / "scripts" / "prestep.sh"
    prestep.write_text('#!/usr/bin/env bash\nset -euo pipefail\nMUT="$(mktemp)"\necho "$MUT"\n')
    prestep.chmod(0o755)

    text = REAL_UNIT.read_text()
    anchor = "ExecStart=/opt/noorinalabs-deploy/scripts/backup.sh"
    assert anchor in text
    unit = tmp_path / "multi.service"
    unit.write_text(
        text.replace(anchor, "ExecStart=/opt/noorinalabs-deploy/scripts/prestep.sh\n" + anchor)
    )

    # Both ExecStarts must be SEEN...
    sb = uh.plan(uh.parse_unit(unit))
    assert sb.exec_starts == [
        "/opt/noorinalabs-deploy/scripts/prestep.sh",
        "/opt/noorinalabs-deploy/scripts/backup.sh",
    ], f"the harness did not parse both ExecStart= lines: {sb.exec_starts}"

    # ...and the defect in the FIRST one must be EXECUTED, and must go RED.
    res = _run(repo, unit=unit)
    assert res.rc == 1, (
        f"THE FIRST ExecStart WAS NOT RUN. It writes to the read-only /tmp and would die on the "
        f"box; the harness reported PASS. A silently skipped command is a gate that tested "
        f"nothing.\n{res.output}"
    )
    assert "Read-only file system" in res.output
    assert "[harness] ExecStart: /opt/noorinalabs-deploy/scripts/prestep.sh" in res.output, (
        "the prestep was never even announced — it was not executed at all"
    )
    # oneshot stops at the first failure: backup.sh must NOT have run.
    assert "ExecStart NOT REACHED" in res.output, (
        "systemd's Type=oneshot stops at the first failing ExecStart=; the harness must too, or "
        "it is running code the box would never reach."
    )


# ===========================================================================
# 2. CALIBRATION — the harness must be PROVEN able to fail, across the CLASS
# ===========================================================================


@needs_namespace
def test_calibration_bare_mktemp_goes_red(tmp_path: Path) -> None:
    """(1) deploy#613/#623's own spelling. `mktemp` with no -p lands in the read-only /tmp."""
    repo = _stage(tmp_path)
    _mutate(repo, "backup.sh", "umask 077", 'HARNESS_MUT="$(mktemp)"')
    res = _run(repo)

    assert res.rc == 1, (
        f"A BARE MKTEMP RAN CLEAN. /tmp is read-only under ProtectSystem=strict, so this must "
        f"fail — if it does not, the sandbox is not the unit's and every green in this file is "
        f"worthless.\n{res.output}"
    )
    assert "Read-only file system" in res.output


@needs_namespace
def test_calibration_a_tmp_redirect_goes_red_and_it_is_not_a_mktemp(tmp_path: Path) -> None:
    """(2) THE TEST THAT JUSTIFIES THIS MODULE'S EXISTENCE.

    A `> /tmp/foo` redirect contains no `mktemp`. The static pin in
    test_scratch_under_hardening.py — which scans for `mktemp` calls that do not name a parent —
    cannot see it. Neither could a repo-wide lint banning bare `mktemp`.

    It fails in production identically to deploy#613. If this does not go RED, then this harness
    has merely reimplemented the static pin, at enormous cost, and closed NOTHING.
    """
    repo = _stage(tmp_path)
    _mutate(repo, "backup.sh", "umask 077", "printf 'x\\n' > /tmp/harness-scratch.txt")

    # Prove the mutation is invisible to the mktemp-shaped guard, or the claim above is hot air.
    assert "mktemp" not in (repo / "scripts" / "backup.sh").read_text().split("umask 077")[1][:80]

    res = _run(repo)
    assert res.rc == 1, (
        f"A `> /tmp/…` REDIRECT RAN CLEAN under the unit's hardening. This is the whole class "
        f"the mktemp pin cannot see, and it is the reason deploy#626 exists.\n{res.output}"
    )
    assert "Read-only file system" in res.output


@needs_namespace
def test_calibration_a_home_write_goes_red_under_ProtectHome(tmp_path: Path) -> None:
    """(3) Not mktemp. Not /tmp. Still fatal — and only because ProtectHome= is read out too.

    `ProtectHome=yes` makes $HOME inaccessible. A script that drops a config or a credential
    under ~/.config dies on the box and nowhere else. Note this is the exact hazard behind the
    rclone note in the stub: rclone escapes ProtectHome today ONLY because backup.sh configures
    it through RCLONE_CONFIG_ISNAD_* env vars rather than a config file.
    """
    repo = _stage(tmp_path)
    _mutate(repo, "backup.sh", "umask 077", "printf 'x\\n' > \"${HOME}/.harness-rclone.conf\"")
    res = _run(repo)

    assert res.rc == 1, (
        f"A WRITE TO $HOME RAN CLEAN. ProtectHome=yes makes it inaccessible on the box. This "
        f"mutation contains no mktemp and never touches /tmp — no text scan would catch it, and "
        f"it is fatal in production.\n{res.output}"
    )


@needs_namespace
def test_calibration_deploy613_itself_restored_goes_red(tmp_path: Path) -> None:
    """(4) THE ORIGINAL DEFECT, PUT BACK, END TO END.

    b2_preflight.sh's scratch moved off /tmp and onto BACKUP_DIR precisely because of deploy#613.
    Move it back and the preflight cannot allocate its scratch dir under the read-only /tmp.

    The strongest calibration available: the harness must reproduce, from the real scripts, the
    bug that shipped TWICE past a green CI. Note what the FIXED code does when its scratch dies —
    it reports PROBE_FAILED, which is honest. The PRE-#613 code reported KEY_INVALID about a
    perfectly good key. Either way the run must be RED here.
    """
    repo = _stage(tmp_path)
    pre = repo / "scripts" / "b2_preflight.sh"
    text = pre.read_text()
    anchor = 'scratch_parent="${BACKUP_DIR:-${TMPDIR:-/tmp}}"'
    assert anchor in text, "b2_preflight.sh no longer sites its scratch the way this test mutates"
    # Send it back to /tmp — the pre-deploy#613 behaviour, verbatim in effect.
    pre.write_text(text.replace(anchor, 'scratch_parent="/tmp"'))

    res = _run(repo)
    assert res.rc == 1, (
        f"deploy#613 ITSELF ran clean. The B2 preflight allocated scratch in a READ-ONLY /tmp "
        f"and the harness did not notice. This is the bug that killed every backup this project "
        f"ever attempted.\n{res.output}"
    )
    assert "PROBE_FAILED" in res.output, (
        "the preflight failed but did not report PROBE_FAILED. If it condemned the CREDENTIAL "
        f"instead, deploy#613's false verdict is back.\n{res.output}"
    )
    assert "verdict=KEY_INVALID" not in res.output, (
        "THE PREFLIGHT BLAMED THE KEY. The key is fine — the harness's rclone stub is a healthy, "
        f"write-capable key by construction. This is deploy#613's lie, verbatim.\n{res.output}"
    )


# ===========================================================================
# 3. DERIVED FROM THE UNIT — not restated. Both tests are TWO-SIDED.
# ===========================================================================


def _unit_with(tmp_path: Path, name: str, old: str, new: str) -> Path:
    text = REAL_UNIT.read_text()
    assert old in text, f"fixture anchor {old!r} is not in the real unit any more"
    unit = tmp_path / name
    unit.write_text(text.replace(old, new))
    return unit


@needs_namespace
def test_a_write_is_refused_or_allowed_according_to_ReadWritePaths(tmp_path: Path) -> None:
    """PROOF THE SANDBOX IS DERIVED: the SAME write, two units, two verdicts.

    The identical mutation — a write to /var/lib/harness-extra — must FAIL under the real unit
    and PASS under a unit that grants that path in ReadWritePaths=. Nothing in the harness
    mentions /var/lib/harness-extra; the only thing that changed is a line in a .service file.

    This is what "derived from the unit" has to mean to be worth saying. Grant a path in the unit
    tomorrow and the gate grants it in the same commit — no second edit, nowhere to drift.
    """
    grant_line = (
        "ReadWritePaths=/var/lib/noorinalabs-backups /opt/noorinalabs-deploy /var/lib/node_exporter"
    )
    # Two SIMPLE commands, deliberately. The first draft wrote `mkdir X && printf … > X/f`, and
    # under `set -e` a failure on the LEFT of an `&&` does not abort — so the mutation swallowed
    # its own failure, the run exited 0, and the calibration "passed" while proving nothing. Same
    # family as feedback_errexit_kills_assignment_guard, met head-on while calibrating a guard
    # against it. A mutation that cannot fire is not evidence; see test_the_gate_cannot_see_a_
    # write_the_script_itself_swallows for the boundary this exposed.
    mutation = "mkdir -p /var/lib/harness-extra\nprintf 'x\\n' > /var/lib/harness-extra/f"

    # (a) NOT granted -> the write must be refused.
    ungranted = _stage(tmp_path / "ungranted")
    _mutate(ungranted, "backup.sh", "umask 077", mutation)
    res_denied = _run(ungranted, unit=REAL_UNIT)
    assert res_denied.rc == 1, (
        f"a write to a path the unit does NOT grant was allowed:\n{res_denied.output}"
    )

    # (b) granted in the unit -> the SAME write must now succeed.
    granted_unit = _unit_with(
        tmp_path, "granted.service", grant_line, grant_line + " /var/lib/harness-extra"
    )
    granted = _stage(tmp_path / "granted")
    _mutate(granted, "backup.sh", "umask 077", mutation)
    res_allowed = _run(granted, unit=granted_unit)
    assert res_allowed.rc == 0, (
        f"THE SANDBOX IS NOT DERIVED FROM THE UNIT. The unit's ReadWritePaths= grants "
        f"/var/lib/harness-extra, production would allow this write, and the harness refused it. "
        f"A gate that disagrees with production about what production allows is worse than no "
        f"gate: it trains people to work around it.\n{res_allowed.output}"
    )


@needs_namespace
def test_PrivateTmp_yes_makes_a_bare_mktemp_legal_because_production_would(tmp_path: Path) -> None:
    """PROOF, THE OTHER WAY ROUND: the harness has no opinion about mktemp. It reads the unit.

    A bare `mktemp` is a defect *because isnad-backup.service does not set PrivateTmp* (deploy#121
    Bug A explains why it cannot). Set PrivateTmp=yes and /tmp becomes a private, WRITABLE tmpfs —
    and the identical `mktemp` that goes RED in the calibration above must go GREEN here, because
    on that box it would work.

    A harness with "bare mktemp is bad" wired into it would fail this test. That harness would be
    a third restatement of the rule, and it would drift the day the unit changed. This one cannot:
    it does not know what mktemp is.
    """
    unit = _unit_with(
        tmp_path,
        "privatetmp.service",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes\nNoNewPrivileges=yes",
    )
    repo = _stage(tmp_path)
    _mutate(repo, "backup.sh", "umask 077", 'HARNESS_MUT="$(mktemp)"')

    res = _run(repo, unit=unit)
    assert res.rc == 0, (
        f"With PrivateTmp=yes the unit HAS a writable /tmp, so a bare mktemp is legal there and "
        f"the harness must say so. It said rc={res.rc}. That means the read-only /tmp is baked "
        f"into the harness rather than read out of the unit — which is the restatement this "
        f"whole issue exists to prevent.\n{res.output}"
    )


# ===========================================================================
# 4. THE PROPERTY NOBODY WROTE DOWN
# ===========================================================================


@needs_namespace
def test_rclone_is_configured_by_env_not_by_a_file_under_the_masked_HOME(tmp_path: Path) -> None:
    """rclone escapes ProtectHome=yes ONLY because backup.sh configures it through the env.

    Nino Kavtaradze raised this in the #624 review and it was never written down anywhere. Real
    rclone reads ~/.config/rclone/rclone.conf. The unit sets ProtectHome=yes, so $HOME is
    INACCESSIBLE — a config-file-based rclone would fail on every backup on the box, and rclone
    would blame the credential while doing it (deploy#613's exact failure mode).

    It works today only because backup.sh exports RCLONE_CONFIG_ISNAD_TYPE/ACCOUNT/KEY and never
    writes a file. That is load-bearing, invisible, and one refactor from being the next defect.

    So the rclone stub REFUSES to answer unless those variables are set, and this test proves the
    refusal is real rather than a comment: strip the export from a fixture copy and the run must
    go RED, naming the reason.
    """
    repo = _stage(tmp_path)
    path = repo / "scripts" / "backup.sh"
    text = path.read_text()
    anchor = 'export RCLONE_CONFIG_ISNAD_ACCOUNT="${B2_KEY_ID}"'
    assert anchor in text, "backup.sh no longer configures rclone through the environment"
    path.write_text(text.replace(anchor, "# (removed by the harness to prove the guard bites)"))

    res = _run(repo)
    assert res.rc == 1, (
        f"backup.sh stopped configuring rclone through RCLONE_CONFIG_ISNAD_* and nothing "
        f"noticed. Under ProtectHome=yes the unit cannot read ~/.config/rclone/rclone.conf, so "
        f"the env form is the ONLY thing keeping rclone working on the box.\n{res.output}"
    )
    # The stub's objection is asserted via its SENTINEL, not its stderr — and that is a finding
    # in itself. b2_preflight captures `rclone lsd … > "$lsd_out" 2>&1`, so the stub's message is
    # swallowed by the capture and the operator sees only `verdict=KEY_INVALID`: an accusation
    # against a CREDENTIAL, produced by a refactor that never touched one. deploy#613's shape,
    # one level up. The sentinel lands in BACKUP_DIR, where nothing can eat it.
    assert any(".harness-rclone-unconfigured" in a for a in res.artifacts), (
        f"the run failed, but not because rclone was left unconfigured — so this test is not "
        f"measuring what it claims to.\nartifacts={res.artifacts}\n{res.output}"
    )

    # WHAT IS DELIBERATELY *NOT* ASSERTED HERE, AND WHY. (deploy#654 review — Bereket Tadesse.)
    #
    # Today this run ends with `verdict=KEY_INVALID`: b2_preflight renders a CONFIGURATION failure
    # as an accusation against a CREDENTIAL — from a fault that never touched one. That is
    # deploy#613's exact signature, in a new place, and it is tracked as **deploy#658**.
    #
    # The first version of this test ASSERTED that string. That was a mistake, and a bad one: a
    # test that encodes a bug as the contract makes the bug LOAD-BEARING. The next person to fix
    # b2_preflight would have seen this test go red and "fixed" their fix. A pinned lie is worse
    # than no test.
    #
    # So the verdict text is not asserted in either direction. What IS asserted is the honest,
    # stable fact — the run FAILS and the sentinel says why — which holds both before and after
    # #658 lands.
    assert res.exec_rc != 0


# ===========================================================================
# 5. WHAT THIS DOES *NOT* CATCH — stated, not hidden
# ===========================================================================


@needs_namespace
def test_a_bare_cd_into_tmp_is_NOT_a_finding_and_that_is_correct(tmp_path: Path) -> None:
    """The honest boundary of the gate, pinned so nobody later 'fixes' it into a false positive.

    A read-only directory is still TRAVERSABLE. `cd /tmp` succeeds under ProtectSystem=strict —
    on the real box, today. It is only the WRITE that fails. So this harness does not flag it,
    and it must not: a gate that fails on a legal operation gets disabled by the third person it
    inconveniences.

    The write that follows the `cd` is what dies, and that IS caught — by
    test_calibration_a_tmp_redirect_goes_red.
    """
    repo = _stage(tmp_path)
    _mutate(repo, "backup.sh", "umask 077", "cd /tmp && cd - > /dev/null")
    res = _run(repo)
    assert res.rc == 0, (
        f"`cd /tmp` was flagged. It is legal under ProtectSystem=strict — read-only is not "
        f"inaccessible — and flagging it would make this gate lie in the other direction.\n"
        f"{res.output}"
    )


@needs_namespace
def test_the_gate_cannot_see_a_write_the_script_itself_swallows(tmp_path: Path) -> None:
    """THE HONEST LIMIT OF THIS GATE. Pinned, with the exact shape, so nobody has to discover it.

    I found this by writing it into my own calibration by accident, which is the only reason it
    is documented rather than latent:

        mkdir -p /var/lib/harness-extra && printf 'x' > /var/lib/harness-extra/f

    Under `set -e`, a command that fails on the LEFT of an `&&` does NOT abort the script (same
    family as feedback_errexit_kills_assignment_guard). So the forbidden write is attempted,
    refused by the kernel, and the script sails on and exits 0. This harness's oracle is the
    entry point's exit status plus its artifacts and its verdict text — and a write that fails
    silently, changes no artifact and produces no false claim leaves NONE of those marks.

    So this gate does not catch it, and this test says so out loud rather than letting a green
    tick imply otherwise (feedback_silent_zero_is_not_a_measurement).

    Two things bound the damage, and they are the reason this is a limit and not a hole:

      * A swallowed write that changes nothing observable has, by construction, no effect on the
        backup. The dangerous ones — deploy#613 and #623 — were not silent: they surfaced as a
        wrong exit code and a false verdict, which is what the other tests in this file assert on.
      * The STATIC pin (test_scratch_under_hardening.py, widened over the unit-reachable closure
        in deploy#626 item 1) reads the source text and does not care whether the failure
        propagates. The two gates are complementary, and this is precisely the seam where the
        static one still earns its keep.

    If this test ever starts FAILING, that is good news: something taught the harness to observe
    the write itself (an strace/LD_PRELOAD oracle on EROFS). Delete it and say so.
    """
    repo = _stage(tmp_path)
    _mutate(
        repo,
        "backup.sh",
        "umask 077",
        "mkdir -p /var/lib/harness-swallowed && printf 'x\\n' > /var/lib/harness-swallowed/f",
    )
    res = _run(repo)
    assert res.rc == 0, (
        "The harness caught a SWALLOWED write outside ReadWritePaths=. That is better than the "
        "documented behaviour — so update this test and the module docstring: the gate is now "
        "stronger than they claim."
    )


def test_the_dedicated_workflow_cannot_skip_this_suite() -> None:
    """The guarantee that makes the local `skipif` defensible. ASSERTED, not assumed.

    `needs_namespace` skips when there is no mount namespace. On its own that would be the exact
    false green this module exists to kill — a gate that quietly tests nothing.

    It is only defensible because ONE workflow runs these same tests with AppArmor lifted and
    `HARNESS_REQUIRE_NAMESPACE=1` set, where a missing namespace is a HARD FAIL and a skip is
    impossible. That guarantee is load-bearing, so it is checked here rather than trusted
    (Bereket Tadesse, #654 review: "assert it, do not assume it").

    If someone deletes the sysctl step, or the env var, or stops running this file in CI, this
    test goes red and tells them what they just disarmed.
    """
    wf = (REPO_ROOT / ".github" / "workflows" / "backup-hardening.yml").read_text()

    assert "kernel.apparmor_restrict_unprivileged_userns=0" in wf, (
        "backup-hardening.yml no longer lifts the AppArmor restriction, so on ubuntu-latest the "
        "harness cannot build a namespace — and every namespace test would SKIP. The local "
        "skipif is only safe because this workflow cannot skip."
    )
    assert "HARNESS_REQUIRE_NAMESPACE" in wf, (
        "backup-hardening.yml no longer sets HARNESS_REQUIRE_NAMESPACE, so a missing namespace "
        "would SKIP GREEN there instead of hard-failing. That is the false green this whole "
        "module exists to prevent."
    )
    assert Path(__file__).name in wf, (
        f"backup-hardening.yml no longer runs {Path(__file__).name}, so nothing guarantees these "
        f"calibrations ever execute where they cannot skip."
    )


def test_the_stub_set_is_declared_so_the_blind_spots_are_countable(tmp_path: Path) -> None:
    """docker/rclone/zstd/systemd-cat are STUBS. That is the harness's blind spot; name it.

    A green run here does NOT mean the backup works against a real Postgres, a real Neo4j or a
    real B2 — there is no daemon, no network and no bucket in CI. It means the SCRIPT wrote only
    where the unit grants. Anyone reading a green tick should be able to find out, in one grep,
    exactly which binaries were not real.
    """
    assert set(uh._STUBS) == {"docker", "rclone", "zstd", "systemd-cat"}, (
        "the stub set changed. Update the module docstring's blind-spot list with it — an "
        "undeclared stub is an undeclared blind spot."
    )
    assert shutil.which("bash"), "the harness needs a real bash; everything else is stubbed"
