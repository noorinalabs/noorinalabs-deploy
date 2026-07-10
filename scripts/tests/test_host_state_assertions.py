"""Guards for the backup-timer and host-provisioning invariants (deploy#558, deploy#551).

`isnad-backup.timer` was never installed on stg or prod. Backups have never run, once,
in the history of this project. The units, the `tmpfiles.d` entry, and the cloud-init
`runcmd` that installs them were all committed and all correct.

Two things were wrong:

1. Both IaC paths ran ``systemctl enable`` without ``--now``. cloud-init's ``runcmd``
   executes during boot, *after* ``timers.target`` has been reached, so ``enable`` alone
   arms the timer for the next reboot and never starts it in this one. Both paths then
   printed "start the backup timer" as a manual runbook step, which nobody performed.

2. Nothing ever asserted the resulting state. An uninstalled timer passed deploy
   verification silently for months.

These tests pin the shape of the fix. The behavioural proof lives in
``scripts/assert_host_state.sh``, which is run against the real hosts by
``verify-deploy.yml`` after every deploy.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

BOOTSTRAP = SCRIPTS_DIR / "bootstrap-vps.sh"
CONVERGE = SCRIPTS_DIR / "converge_host.sh"
ASSERT = SCRIPTS_DIR / "assert_host_state.sh"
CLOUD_INIT = REPO_ROOT / "terraform" / "hetzner" / "modules" / "hetzner-vps" / "cloud-init.yaml.tpl"

# Files that install or arm the backup timer. Each must use `enable --now`.
TIMER_INSTALLERS = (BOOTSTRAP, CONVERGE, CLOUD_INIT)


def _code_only(text: str) -> str:
    """Drop whole-line comments (shell `#` and cloud-init YAML `#`).

    The fixes quote the old buggy invocation in their comments so the next reader knows
    what was wrong. A raw substring search would match the explanation rather than the
    executable line.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().lstrip("- ").startswith("#")
    )


# --------------------------------------------------------------------------
# `enable` without `--now` is the bug
# --------------------------------------------------------------------------


def test_every_timer_installer_uses_enable_now() -> None:
    for path in TIMER_INSTALLERS:
        code = _code_only(path.read_text())
        enables = re.findall(r"systemctl enable(?: --now)? isnad-backup\.timer", code)
        assert enables, f"{path.name} does not enable isnad-backup.timer at all"
        for match in enables:
            assert "--now" in match, (
                f"{path.name} runs `systemctl enable` without `--now`. cloud-init runcmd "
                "executes after timers.target is reached, so the timer will not start "
                "until the next reboot (deploy#558)."
            )


def test_no_installer_defers_starting_the_timer_to_a_human() -> None:
    """The manual `systemctl start` runbook step is what was never performed."""
    for path in TIMER_INSTALLERS:
        text = path.read_text()
        assert "systemctl start isnad-backup.timer" not in text, (
            f"{path.name} still instructs an operator to start the timer by hand. "
            "Use `systemctl enable --now` and assert the result."
        )


# --------------------------------------------------------------------------
# The deploy user must own its own home (deploy#551)
# --------------------------------------------------------------------------


# The three files spell the same chown three ways:
#   cloud-init:  chown deploy:deploy /home/deploy
#   bootstrap:   chown "$DEPLOY_USER:$DEPLOY_USER" "/home/$DEPLOY_USER"
#   converge:    chown "${DEPLOY_USER}:${DEPLOY_USER}" "$DEPLOY_HOME"
# Match the *target* of a non-recursive chown in any of those forms.
_HOME_TARGET = r'(?:"?/home/(?:deploy|\$\{?DEPLOY_USER\}?)"?|"?\$\{?DEPLOY_HOME\}?"?)'
_CHOWN_HOME = re.compile(rf"chown\s+(?!-R\b)\S+\s+{_HOME_TARGET}\s*$", re.MULTILINE)
_CHOWN_HOME_RECURSIVE = re.compile(rf"chown\s+-R\s+\S+\s+{_HOME_TARGET}\s*$", re.MULTILINE)


def test_provisioning_fixes_deploy_home_ownership() -> None:
    for path in (BOOTSTRAP, CONVERGE, CLOUD_INIT):
        code = _code_only(path.read_text())
        assert _CHOWN_HOME.search(code), (
            f"{path.name} does not correct the ownership of the deploy user's home "
            "(deploy#551): prod's /home/deploy is root:root and deploy-data-load.yml "
            "fails at its first mkdir"
        )


def test_deploy_home_chown_is_not_recursive() -> None:
    """A recursive chown would rewrite .ssh/ and .docker/config.json ownership."""
    for path in (BOOTSTRAP, CONVERGE, CLOUD_INIT):
        code = _code_only(path.read_text())
        assert not _CHOWN_HOME_RECURSIVE.search(code), (
            f"{path.name} chowns the deploy home recursively; .ssh/ and .docker/ modes matter"
        )


# --------------------------------------------------------------------------
# The assertion is the deliverable
# --------------------------------------------------------------------------


def test_assert_script_checks_installed_enabled_active_and_last_run() -> None:
    code = _code_only(ASSERT.read_text())
    assert "is-enabled" in code, "must assert the timer is enabled"
    assert "is-active" in code, (
        "must assert the timer is ACTIVE — `enable` without `--now` leaves it enabled "
        "but inactive, which is the exact state both hosts were in"
    )
    assert "LastTriggerUSec" in code, "must inspect last-run state"
    assert "NextElapseUSecRealtime" in code, "must assert the timer will actually fire again"
    assert "NEVER_RUN_GRACE_HOURS" in code, (
        "a timer installed long ago that has never triggered must fail, not pass"
    )


def test_assert_script_does_not_credit_a_grace_window_to_an_absent_timer() -> None:
    """ "Installed 0h ago" and "not installed at all" are different facts.

    The first version of the script awarded a PASS ("within its post-install grace
    window") on a host where the unit file did not exist.
    """
    code = _code_only(ASSERT.read_text())
    assert "cannot evaluate last-run state" in code, (
        "an absent timer unit must fail the last-run check rather than fall into the "
        "post-install grace-window branch"
    )


def test_assert_script_checks_staging_dir_and_deploy_home() -> None:
    code = _code_only(ASSERT.read_text())
    assert "/var/lib/noorinalabs-backups" in code
    assert "/home/deploy" in code


def test_converge_is_root_only_and_reads_units_from_the_repo() -> None:
    code = _code_only(CONVERGE.read_text())
    assert "id -u" in code, "converge must refuse to run as non-root"
    assert "REPO_DIR" in code and "systemd" in code
    assert "exit 1" in code, "a missing unit source must fail loudly, not skip silently"


# --------------------------------------------------------------------------
# Wiring: converge on deploy, assert on verify
# --------------------------------------------------------------------------


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _step_names(wf: dict, job: str) -> list[str]:
    return [s.get("name", "") for s in wf["jobs"][job]["steps"]]


def test_both_deploy_workflows_converge_the_host() -> None:
    for name in ("deploy-stg.yml", "deploy-prod.yml"):
        wf = _workflow(name)
        steps = wf["jobs"]["deploy"]["steps"]
        scripts = [str(s.get("with", {}).get("script", "")) for s in steps]
        assert any("converge_host.sh" in x for x in scripts), (
            f"{name} must converge host provisioning; cloud-init only runs on first boot "
            "so a template-only fix repairs no existing host (deploy#558)"
        )


def test_converge_runs_after_the_host_repo_is_synced() -> None:
    """converge_host.sh installs unit files from the host's repo checkout."""
    for name in ("deploy-stg.yml", "deploy-prod.yml"):
        wf = _workflow(name)
        steps = wf["jobs"]["deploy"]["steps"]
        sync_idx = next(
            i for i, s in enumerate(steps) if "write-deploy-env" in str(s.get("uses", ""))
        )
        converge_idx = next(
            i
            for i, s in enumerate(steps)
            if "converge_host.sh" in str(s.get("with", {}).get("script", ""))
        )
        assert converge_idx > sync_idx, (
            f"{name}: converge must run after write-deploy-env resets the host repo, or it "
            "installs stale unit files"
        )


def test_verify_deploy_asserts_host_state_on_both_environments() -> None:
    wf = _workflow("verify-deploy.yml")
    for job in ("verify-stg", "verify-prod"):
        steps = wf["jobs"][job]["steps"]
        scripts = [str(s.get("with", {}).get("script", "")) for s in steps]
        assert any("assert_host_state.sh" in x for x in scripts), (
            f"verify-deploy.yml job '{job}' must assert host state. An uninstalled backup "
            "timer passed deploy verification silently for months (deploy#558)."
        )


def test_verify_assertion_invokes_a_linted_script_rather_than_inline_shell() -> None:
    """CI does not shellcheck ssh-action `script:` bodies (deploy#555).

    Keeping the body a single invocation of a repo script means the logic is covered by
    the `shellcheck + bash -n (infra & scripts)` job.
    """
    wf = _workflow("verify-deploy.yml")
    for job in ("verify-stg", "verify-prod"):
        for step in wf["jobs"][job]["steps"]:
            script = str(step.get("with", {}).get("script", ""))
            if "assert_host_state.sh" not in script:
                continue
            assert len(script.strip().splitlines()) == 1, (
                "the ssh-action script body must be a single invocation of the linted "
                "repo script, since ssh-action bodies are not shellchecked (deploy#555)"
            )
