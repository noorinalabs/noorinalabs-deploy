#!/usr/bin/env python3
"""Per-env env-restructure validation gate (deploy#363, design deploy#332).

Implements the four checks the accepted env-restructure design
(`docs/env-restructure-design.md`) names, in the design's priority order:

  1. settings-load  — for each service, assemble the non-secret env tiers
                      (env/base.env -> env/services/<svc>.env -> env/<env>.env)
                      plus PLACEHOLDER values for every key in
                      secrets/<env>.env.example, then construct a Pydantic
                      Settings model and assert it loads without ValidationError.
                      (The forcing function — build first.)
  2. secret-keys    — assert secrets/<env>.env.example is a subset of the
                      deploy-<env>.yml env_keys passlist, and that every
                      ${VAR:?...} guard in compose/docker-compose.prod.yml is
                      satisfied by SOME tier (env/* or the secret catalogue).
                      Eliminates the env:/env_keys hand-sync drift independent
                      of storage backend. Also fails if any tracked file under
                      secrets/ is not a *.env.example catalogue.
  3. os-environ     — grep gate over **/src/** flagging os.environ/os.getenv,
                      with an allowlist for tests/integration-tests/scripts
                      (decision #3: Settings mandatory in service src/).
  4. inventory      — re-run scripts/env-inventory.py and fail if
                      docs/env-inventory.{csv,md} is stale. SKIPS when the
                      sibling org tree is not checked out (deploy CI checks out
                      this repo alone) — see notes in cmd_inventory.

Why per-service SCHEMA SHIMS rather than importing each service's real
Settings class: the deploy repo's CI checks out only the deploy repo, so the
service `src/` packages (and their deps) are not importable here. The design
explicitly blesses "a thin per-service shim" as the alternative. The shims in
scripts/env_validate_schemas.py mirror the security-relevant constraints of the
real Settings classes (e.g. user-service's ENVIRONMENT allowlist and the OAuth
override prod-guard) so the gate catches the failures that matter while staying
hermetic and fast.

Run all checks:   python3 scripts/env_validate.py all
Run one check:    python3 scripts/env_validate.py settings-load
Exit code 0 = pass, 1 = a check failed, 2 = usage/internal error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Repo root = parent of scripts/. Resolve via this file so the gate works from
# a worktree or from the repo root identically.
REPO_ROOT = Path(__file__).resolve().parent.parent

ENVS = ("stg", "prod")

# Services whose Settings we load-test. Maps the env/services/<svc>.env name to
# the shim builder in env_validate_schemas.py.
SERVICES = ("isnad-graph", "user-service")

COMPOSE_FILE = REPO_ROOT / "compose" / "docker-compose.prod.yml"

# Vars compose requires (or interpolates) that are written by the workflow
# itself (the write-deploy-env composite / extra_image_tags), not pulled from
# the secret store and not declared in an env/ tier. Mirrors the WORKFLOW_WRITTEN
# set in compose-validate.yml's passlist-drift job so the two gates agree.
WORKFLOW_WRITTEN = frozenset(
    {
        "IMAGE_TAG",
        "BASE_DOMAIN",
        "USER_SERVICE_IMAGE_TAG",
        "LANDING_IMAGE_TAG",
        "PROM_CONFIG_FILE",
        "ALERTMANAGER_CONFIG_FILE",
        "SLACK_WEBHOOK_FILE",
    }
)


# ---------- dotenv parsing ----------

_DOTENV_LINE_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=(.*)$")


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a NAME=value dotenv file. Ignores blanks and # comments.

    Values are taken verbatim (no quote stripping beyond a single matching
    pair) — adequate for the gate's purposes (we only need keys + placeholder
    presence, not byte-exact value semantics).
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _DOTENV_LINE_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def assemble_env(env: str, service: str) -> dict[str, str]:
    """Assemble the non-secret tiers + secret PLACEHOLDERS for one (env, svc).

    Resolution order (last wins), per the design:
      base.env -> services/<svc>.env -> <env>.env -> secret placeholders
    Secret values are placeholders sourced from secrets/<env>.env.example so the
    gate never depends on a real secret.
    """
    merged: dict[str, str] = {}
    merged.update(parse_env_file(REPO_ROOT / "env" / "base.env"))
    merged.update(parse_env_file(REPO_ROOT / "env" / "services" / f"{service}.env"))
    merged.update(parse_env_file(REPO_ROOT / "env" / f"{env}.env"))
    merged.update(parse_env_file(REPO_ROOT / "secrets" / f"{env}.env.example"))
    return merged


# ---------- compose required-var extraction ----------

_COMPOSE_REQUIRED_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*):\?")
_COMPOSE_ANY_INTERP_RE = re.compile(r"\$\{?([A-Z_][A-Z0-9_]*)")


def compose_required_vars() -> set[str]:
    """${NAME:?...} — compose vars that fail-fast if unset/empty."""
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    return set(_COMPOSE_REQUIRED_RE.findall(text))


# ---------- passlist extraction (mirrors passlist-drift) ----------

_ENV_KEYS_RE = re.compile(r"^\s*env_keys:\s*([A-Z0-9_,]+)\s*$", re.M)


def deploy_passlist(env: str) -> set[str]:
    wf = REPO_ROOT / ".github" / "workflows" / f"deploy-{env}.yml"
    text = wf.read_text(encoding="utf-8")
    m = _ENV_KEYS_RE.search(text)
    if not m:
        raise RuntimeError(f"could not find `env_keys:` CSV line in {wf}")
    return {tok.strip() for tok in m.group(1).split(",") if tok.strip()}


# ---------- check 1: settings-load ----------


def cmd_settings_load() -> int:
    try:
        from env_validate_schemas import SERVICE_SCHEMAS
    except ImportError:
        # Allow invocation as `python3 scripts/env_validate.py` from repo root.
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from env_validate_schemas import SERVICE_SCHEMAS

    failed = False
    for env in ENVS:
        for svc in SERVICES:
            assembled = assemble_env(env, svc)
            schema = SERVICE_SCHEMAS.get(svc)
            if schema is None:
                print(f"::error::no Settings shim registered for service '{svc}'")
                failed = True
                continue
            try:
                schema(assembled, env)
            except Exception as exc:  # noqa: BLE001 — surface any validation failure
                failed = True
                print(
                    f"::error::[{env}/{svc}] Settings() failed to load from the "
                    f"assembled env tiers: {exc}"
                )
            else:
                print(f"OK  settings-load: {env}/{svc}")
    if failed:
        print("settings-load: FAILED")
        return 1
    print("settings-load: OK")
    return 0


# ---------- check 2: secret-keys + compose-guard completeness ----------


def cmd_secret_keys() -> int:
    failed = False

    # 2a: every tracked file under secrets/ must be a *.env.example catalogue.
    tracked = _git_tracked_under("secrets")
    for rel in tracked:
        name = Path(rel).name
        if name == ".gitignore":
            continue
        if not name.endswith(".env.example"):
            failed = True
            print(
                f"::error file={rel}::tracked file under secrets/ is not a "
                f"*.env.example catalogue — secret VALUES must never be committed"
            )

    required = compose_required_vars()

    for env in ENVS:
        catalogue = set(parse_env_file(REPO_ROOT / "secrets" / f"{env}.env.example"))
        passlist = deploy_passlist(env)

        # 2b: secret catalogue ⊆ deploy passlist (kills the 27-key hand-sync drift).
        missing_from_passlist = catalogue - passlist
        if missing_from_passlist:
            failed = True
            print(
                f"::error file=.github/workflows/deploy-{env}.yml::secrets/"
                f"{env}.env.example keys missing from deploy-{env} env_keys: "
                f"{sorted(missing_from_passlist)}"
            )

        # 2c: every compose ${VAR:?} guard is satisfied by SOME tier.
        non_secret = set(assemble_env(env, "isnad-graph")) | set(assemble_env(env, "user-service"))
        satisfied = non_secret | catalogue | WORKFLOW_WRITTEN
        unsatisfied = required - satisfied
        if unsatisfied:
            failed = True
            print(
                f"::error file=compose/docker-compose.prod.yml::[{env}] required "
                f"compose vars not satisfied by any tier (env/*, secrets catalogue, "
                f"or workflow-written): {sorted(unsatisfied)}"
            )

    if failed:
        print("secret-keys: FAILED")
        return 1
    print("secret-keys: OK")
    return 0


def _git_tracked_under(subdir: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", subdir],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Not a git checkout (or git absent) — fall back to walking the tree.
        base = REPO_ROOT / subdir
        return [
            str(p.relative_to(REPO_ROOT))
            for p in base.rglob("*")
            if p.is_file() and p.name != ".gitignore"
        ]
    return [line for line in out.stdout.splitlines() if line.strip()]


# ---------- check 3: os.environ lint over **/src/** ----------

_OS_ENVIRON_RE = re.compile(r"\bos\.(?:environ\b|getenv\b)")

# Path fragments where raw os.environ is allowed (decision #3): tests, the
# integration-tests harness, and one-off scripts. Scoped to **/src/** only.
_OS_ENVIRON_ALLOW_FRAGMENTS = (
    "/tests/",
    "/test/",
    "/integration-tests/",
    "/scripts/",
)


def cmd_os_environ() -> int:
    """Flag raw os.environ/os.getenv under any **/src/** tree.

    Decision #3: Pydantic Settings mandatory in service src/; raw os.environ
    allowed in tests/integration-tests/scripts. The deploy repo has no src/ of
    its own; this gate runs over whatever src/ trees are checked out. In the
    deploy repo's own CI that is typically none, so it is a no-op there but
    becomes load-bearing when wired into a repo that ships a src/ (or run over
    the org tree). It scopes strictly to **/src/** so it never touches the deploy
    repo's scripts/ or integration-tests/.
    """
    failed = False
    src_dirs = sorted(REPO_ROOT.rglob("src"))
    # Also consider sibling repos if the org tree happens to be present.
    org_root = REPO_ROOT.parent
    if org_root.is_dir():
        for sib in sorted(org_root.glob("noorinalabs-*")):
            if sib == REPO_ROOT:
                continue
            src_dirs.extend(sorted(sib.rglob("src")))

    seen_any_src = False
    for src in src_dirs:
        if not src.is_dir():
            continue
        # Skip vendored / worktree trees.
        s = "/" + str(src).replace("\\", "/") + "/"
        if any(f in s for f in ("/node_modules/", "/.venv/", "/worktrees/", "/dist/")):
            continue
        seen_any_src = True
        for py in sorted(src.rglob("*.py")):
            rel = "/" + str(py).replace("\\", "/")
            if any(frag in rel for frag in _OS_ENVIRON_ALLOW_FRAGMENTS):
                continue
            if any(f in rel for f in ("/node_modules/", "/.venv/", "/worktrees/")):
                continue
            for lineno, line in enumerate(
                py.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
            ):
                if _OS_ENVIRON_RE.search(line):
                    failed = True
                    try:
                        shown = py.relative_to(org_root)
                    except ValueError:
                        shown = py
                    print(
                        f"::error file={shown}::raw os.environ/os.getenv in src/ "
                        f"(line {lineno}) — config must flow through Pydantic "
                        f"Settings (deploy#332 decision #3). Move to a Settings "
                        f"field, or relocate to tests/scripts if it is test glue."
                    )
    if not seen_any_src:
        print("os-environ: no src/ trees checked out — gate is a no-op here.")
        return 0
    if failed:
        print("os-environ: FAILED")
        return 1
    print("os-environ: OK")
    return 0


# ---------- check 4: env-inventory staleness ----------


def cmd_inventory() -> int:
    """Re-run scripts/env-inventory.py and fail on a stale docs/env-inventory.*.

    env-inventory.py scans the 7 sibling repos under the org tree. The deploy
    repo's own CI checks out this repo alone, so the siblings are absent and the
    inventory would regenerate to a near-empty set — a false "stale". We detect
    that and SKIP rather than false-fail: the staleness gate is only meaningful
    when the full org tree is checked out (locally, or in a future org-tree CI
    job). When present, we run the inventory in-place and `git diff --exit-code`
    the two generated docs.
    """
    org_root = REPO_ROOT.parent
    required_siblings = ("noorinalabs-isnad-graph", "noorinalabs-user-service")
    have_org_tree = org_root.is_dir() and all((org_root / s).is_dir() for s in required_siblings)
    if not have_org_tree:
        print(
            "inventory: sibling org tree not checked out — skipping staleness "
            "check (only meaningful with the full 7-repo tree present)."
        )
        return 0

    script = REPO_ROOT / "scripts" / "env-inventory.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        print(f"::error::{proc.stderr.strip()}")
        return 1
    print("inventory: OK (docs/env-inventory.{csv,md} current)")
    return 0


# ---------- driver ----------

CHECKS = {
    "settings-load": cmd_settings_load,
    "secret-keys": cmd_secret_keys,
    "os-environ": cmd_os_environ,
    "inventory": cmd_inventory,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "check",
        choices=[*CHECKS.keys(), "all"],
        help="which check to run ('all' runs every check)",
    )
    args = parser.parse_args(argv)

    if args.check == "all":
        rc = 0
        for name, fn in CHECKS.items():
            print(f"--- {name} ---")
            rc |= fn()
            print()
        return 1 if rc else 0
    return CHECKS[args.check]()


if __name__ == "__main__":
    sys.exit(main())
