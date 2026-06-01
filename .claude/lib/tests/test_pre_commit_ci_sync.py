"""Tests for pre_commit_ci_sync — the pre-commit <-> CI drift gate (#327).

Verifies:
  1. Canonical kind extraction from both pre-commit configs and CI workflows.
  2. The drift direction that gates: CI-enforced-but-not-local is harmful;
     local-but-not-CI is stricter-local (informational, never a gate fail).
  3. ruff-format vs ruff-lint are not conflated.
  4. The deploy-specific `build`-kind narrowing: runtime `docker buildx`/
     `docker build` lines do NOT register as a build CI gate (they are this
     repo's actual job, not a mirrorable quality gate).
  5. The real deploy repo's pre-commit mirrors its CI kinds (no drift), run
     UNSCOPED over all workflows — the gate running against the very repo that
     ships it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Helper lives at .claude/lib/pre_commit_ci_sync.py; test is at
# .claude/lib/tests/test_*.py. parent.parent reaches the lib root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pre_commit_ci_sync import (  # noqa: E402
    _default_ci_paths,
    check_repo,
    compute_drift,
    kinds_from_ci,
    kinds_from_precommit,
)

# .claude/lib/tests/test_*.py → parents[3] reaches the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class PrecommitKindExtraction(unittest.TestCase):
    def test_ruff_format_and_lint_both_detected(self) -> None:
        cfg = """
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    hooks:
      - id: ruff-format
      - id: ruff
"""
        kinds = kinds_from_precommit(cfg)
        self.assertIn("ruff-format", kinds)
        self.assertIn("ruff-lint", kinds)

    def test_terraform_and_gitleaks_and_actionlint(self) -> None:
        cfg = """
repos:
  - repo: https://github.com/antonbabenko/pre-commit-terraform
    hooks:
      - id: terraform_fmt
  - repo: https://github.com/gitleaks/gitleaks
    hooks:
      - id: gitleaks
  - repo: https://github.com/rhysd/actionlint
    hooks:
      - id: actionlint
"""
        kinds = kinds_from_precommit(cfg)
        self.assertIn("terraform-fmt", kinds)
        self.assertIn("gitleaks", kinds)
        self.assertIn("actionlint", kinds)

    def test_comments_ignored(self) -> None:
        cfg = "# id: mypy is just a comment\nrepos: []\n"
        self.assertNotIn("mypy", kinds_from_precommit(cfg))


class CiKindExtraction(unittest.TestCase):
    def test_run_steps_detected(self) -> None:
        wf = """
jobs:
  lint:
    steps:
      - run: terraform fmt -check
      - run: mypy .claude/hooks/
      - run: actionlint -color
"""
        kinds = kinds_from_ci(wf)
        self.assertEqual(
            kinds & {"terraform-fmt", "mypy", "actionlint"},
            {"terraform-fmt", "mypy", "actionlint"},
        )

    def test_ruff_format_line_not_counted_as_lint(self) -> None:
        # A format-only line must NOT register ruff-lint.
        kinds = kinds_from_ci("      - run: ruff format --check .\n")
        self.assertIn("ruff-format", kinds)
        self.assertNotIn("ruff-lint", kinds)


class BuildKindNarrowing(unittest.TestCase):
    """deploy-specific: runtime `docker buildx`/`docker build` lines are this
    repo's job, NOT a mirrorable CI build-quality gate, so they must not
    register the `build` kind (which would create permanent un-mirrorable
    harmful drift)."""

    def test_docker_buildx_runtime_not_build_kind(self) -> None:
        for line in (
            "      - name: Set up Docker Buildx",
            "          docker buildx build --tag foo .",
            "          docker buildx imagetools create foo",
            "          docker build -t foo .",
        ):
            self.assertNotIn("build", kinds_from_ci(line), f"false-matched: {line!r}")

    def test_real_build_gate_still_detected(self) -> None:
        # The classifier scans `run:`/`uses:`/`- `-prefixed lines, so exercise
        # the real build-gate idioms as they appear on those lines.
        for line in (
            "      - run: npm run build",
            "      - uses: ./.github/actions/build-and-validate",
            "      - run: make build-and-test",
        ):
            self.assertIn("build", kinds_from_ci(line), f"missed real gate: {line!r}")


class DriftDirection(unittest.TestCase):
    def test_ci_enforced_not_local_is_harmful(self) -> None:
        harmful, stricter = compute_drift(
            precommit_kinds={"actionlint"},
            ci_kinds={"actionlint", "terraform-fmt", "mypy"},
        )
        self.assertEqual(harmful, {"terraform-fmt", "mypy"})
        self.assertEqual(stricter, set())

    def test_local_not_ci_is_stricter_only(self) -> None:
        harmful, stricter = compute_drift(
            precommit_kinds={"actionlint", "gitleaks"},
            ci_kinds={"actionlint"},
        )
        self.assertEqual(harmful, set())
        self.assertEqual(stricter, {"gitleaks"})

    def test_perfect_mirror_no_drift(self) -> None:
        harmful, stricter = compute_drift(
            precommit_kinds={"actionlint", "terraform-fmt", "mypy"},
            ci_kinds={"actionlint", "terraform-fmt", "mypy"},
        )
        self.assertEqual(harmful, set())
        self.assertEqual(stricter, set())


class RealDeployRepoHasNoDrift(unittest.TestCase):
    """The deploy pre-commit config must mirror its CI kinds, UNSCOPED over all
    workflows — this is the gate running against the very repo that ships it."""

    def test_deploy_precommit_mirrors_deploy_ci_unscoped(self) -> None:
        precommit = _REPO_ROOT / ".pre-commit-config.yaml"
        ci_paths = _default_ci_paths(_REPO_ROOT)
        self.assertTrue(precommit.is_file(), "deploy must have a pre-commit config")
        self.assertTrue(ci_paths, "deploy must have CI workflows")
        harmful, _ = check_repo(precommit, ci_paths)
        self.assertEqual(
            harmful,
            set(),
            f"deploy pre-commit must mirror CI; missing locally: {sorted(harmful)}",
        )


if __name__ == "__main__":
    unittest.main()
