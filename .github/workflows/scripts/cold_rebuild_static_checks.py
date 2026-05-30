#!/usr/bin/env python3
"""Static workflow-shape guards for the cold-rebuild dryrun gate.

Each check codifies one of the five P3W2-emergency-thread bugs as an
inverted invariant: "the bug shape MUST NOT reappear, and the fix shape
MUST stay in place". Failure prints a per-check diagnosis pointing at the
canonical issue and the fix PR.

Inputs (env):
    USER_SERVICE_PYPROJECT — path to the user-service pyproject.toml,
        already checked out via the wave-aware sibling-ref step.

This script does NOT contact GHCR, Hetzner, or any external registry. It
reads the workflow YAML files, the terraform sources, and the user-service
pyproject and asserts shape. The dynamic complement (registry shape parity
for bug #4) is the buildx-shape-dryrun job in cold-rebuild-dryrun.yml.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

WORKFLOWS = Path(".github/workflows")
TF_MODULE = Path("terraform/hetzner/modules/hetzner-vps")


class Failure:
    """A single check failure with diagnostic context."""

    def __init__(self, check: str, bug: str, msg: str) -> None:
        self.check = check
        self.bug = bug
        self.msg = msg

    def render(self) -> str:
        return f"  - [{self.check}] ({self.bug}) {self.msg}"


def check_terraform_no_ephemeral_keypair() -> list[Failure]:
    """Bug #1 (#216, fixed #217): terraform.yml must NOT generate runner
    keypairs. The fix shape: canonical pubkeys are committed under
    modules/hetzner-vps/ and the env-root `*_ssh_public_key_path` defaults
    point at them.

    Updated for ADR 0006 (#164): the single `ssh_public_key_path` /
    `deploy.pub` pair is now a per-role split — a `deploy.pub` (CI deploy
    key) AND a `root.pub` (placeholder for the owner-workstation-only root
    key), with two env-root variables `deploy_ssh_public_key_path` and
    `root_ssh_public_key_path`. Both pubkeys must exist + look valid, and
    both env-root defaults must point at the checked-in canonical pubkeys
    (never an operator-local `~/.ssh/...` path — that recreates the #216
    lockout class).
    """
    failures: list[Failure] = []
    tf_yml = (WORKFLOWS / "terraform.yml").read_text(encoding="utf-8")

    # Negative invariant: the workflow must NOT call ssh-keygen.
    if re.search(r"\bssh-keygen\b", tf_yml):
        failures.append(
            Failure(
                "tf-no-ssh-keygen",
                "#216",
                "terraform.yml contains `ssh-keygen` — the runner-ephemeral keypair "
                "anti-pattern is back. Every VPS provisioned by the workflow will be "
                "authorized for a key whose private half is destroyed when the runner "
                "shuts down. Restore the canonical-pubkey approach from #217.",
            )
        )

    # Positive invariant: both canonical pubkeys must exist on disk and
    # look like SSH ed25519 lines. We do NOT validate the cryptographic
    # content (rotation is operator scope), only that each file isn't a
    # stub or a different format. Per ADR 0006 root.pub is a placeholder
    # (the real root key is owner-workstation-only); it still must be a
    # syntactically valid pubkey so cloud-init's templatefile renders and
    # `terraform validate` on the module passes with the local defaults.
    for role in ("deploy", "root"):
        pubkey = TF_MODULE / f"{role}.pub"
        if not pubkey.is_file():
            failures.append(
                Failure(
                    "tf-canonical-pubkey-present",
                    "#216",
                    f"{pubkey} is missing — the canonical {role} pubkey is gone. "
                    "Restore it. The deploy pubkey matches the env-scoped "
                    "DEPLOY_SSH_PRIVATE_KEY secret (ADR 0006); root.pub is the "
                    "checked-in placeholder for the owner-workstation-only root key.",
                )
            )
            continue
        line = pubkey.read_text(encoding="utf-8").strip()
        if not re.match(r"^ssh-(ed25519|rsa)\s+[A-Za-z0-9+/=]+(\s+\S+)?$", line):
            failures.append(
                Failure(
                    "tf-canonical-pubkey-shape",
                    "#216",
                    f"{pubkey} does not look like a valid SSH public key line. "
                    "Format must be `ssh-ed25519 <base64-key> [comment]`.",
                )
            )

    # Positive invariant: each env-root variables.tf default for the two
    # per-role pubkey-path variables must point at the canonical checked-in
    # pubkeys, not `~/.ssh/id_ed25519.pub` (the operator's local key — what
    # was there before #217 fixed it). The expected default is the
    # corresponding `../../modules/hetzner-vps/{role}.pub`.
    role_to_var = {
        "deploy": "deploy_ssh_public_key_path",
        "root": "root_ssh_public_key_path",
    }
    for env in ("stg", "prod"):
        var_tf = Path(f"terraform/hetzner/envs/{env}/variables.tf")
        if not var_tf.is_file():
            failures.append(
                Failure(
                    "tf-env-variables-present",
                    "#216",
                    f"{var_tf} missing — env-root structure broken.",
                )
            )
            continue
        contents = var_tf.read_text(encoding="utf-8")
        for role, var_name in role_to_var.items():
            m = re.search(
                rf'variable\s+"{re.escape(var_name)}"\s*\{{[^}}]*?default\s*=\s*"([^"]+)"',
                contents,
                re.DOTALL,
            )
            if not m:
                failures.append(
                    Failure(
                        "tf-ssh-key-default-present",
                        "#216",
                        f"{var_tf}: {var_name} variable missing or has no default. "
                        "ADR 0006 requires both deploy_ssh_public_key_path and "
                        "root_ssh_public_key_path with checked-in defaults.",
                    )
                )
                continue
            default = m.group(1)
            if "id_ed25519.pub" in default or default.startswith("~"):
                failures.append(
                    Failure(
                        "tf-ssh-key-default-canonical",
                        "#216",
                        f"{var_tf}: {var_name} default is `{default}` — must point "
                        f"at the canonical checked-in pubkey "
                        f"(`../../modules/hetzner-vps/{role}.pub`). Operator-local "
                        "paths like `~/.ssh/id_ed25519.pub` recreate the #216 "
                        "lockout class.",
                    )
                )

    return failures


def check_promote_token_scope() -> list[Failure]:
    """Bug #2 (retag-token mismatch): promote.yml's retag step writes
    prod-* tags onto packages owned by sibling repos, requiring write:packages
    scope that the default GITHUB_TOKEN lacks. The fix shape: the retag job's
    docker/login-action uses GH_PACKAGES_TOKEN (org secret), not GITHUB_TOKEN.
    """
    failures: list[Failure] = []
    pm = (WORKFLOWS / "promote.yml").read_text(encoding="utf-8")

    # Locate the `retag:` job. We need to know which password: line is
    # inside the retag job vs. inside the plan job (which only reads, so
    # GITHUB_TOKEN is fine there).
    retag_match = re.search(r"^  retag:\s*$", pm, re.MULTILINE)
    if not retag_match:
        failures.append(
            Failure(
                "promote-retag-job-present",
                "retag-token",
                "promote.yml has no `retag:` job — workflow shape changed. Check "
                "that the cold-rebuild dryrun's expectations match.",
            )
        )
        return failures

    # Take everything from `retag:` to either the next top-level job or EOF.
    retag_start = retag_match.start()
    next_job = re.search(r"^  [a-z][a-z0-9_-]*:\s*$", pm[retag_start + 1 :], re.MULTILINE)
    retag_end = retag_start + 1 + next_job.start() if next_job else len(pm)
    retag_block = pm[retag_start:retag_end]

    # The retag block's docker/login-action MUST use GH_PACKAGES_TOKEN.
    # We require at least one `password: ${{ secrets.GH_PACKAGES_TOKEN }}`
    # in the retag block, and zero `password: ${{ secrets.GITHUB_TOKEN }}`
    # uses in that block (the latter would mean the wrong token snuck back
    # onto a login step).
    has_packages_token = bool(
        re.search(r"password:\s*\$\{\{\s*secrets\.GH_PACKAGES_TOKEN\s*\}\}", retag_block)
    )
    bad_default_token = bool(
        re.search(r"password:\s*\$\{\{\s*secrets\.GITHUB_TOKEN\s*\}\}", retag_block)
    )

    if not has_packages_token:
        failures.append(
            Failure(
                "promote-retag-uses-packages-token",
                "retag-token",
                "promote.yml retag job does NOT authenticate with "
                "secrets.GH_PACKAGES_TOKEN. The default GITHUB_TOKEN lacks "
                "write:packages on noorinalabs/* container packages owned by "
                "sibling repos — this caused the 2026-05-02 emergency-restore "
                "403 permission_denied failures. Restore GH_PACKAGES_TOKEN at "
                "the retag step's docker/login-action.",
            )
        )
    if bad_default_token:
        failures.append(
            Failure(
                "promote-retag-no-default-token",
                "retag-token",
                "promote.yml retag job has a docker/login-action authenticating "
                "with secrets.GITHUB_TOKEN. That token cannot write packages "
                "owned by sibling repos. Switch every login step in the retag "
                "job to GH_PACKAGES_TOKEN.",
            )
        )

    return failures


def check_promote_no_floating_source() -> list[Failure]:
    """Bug #3 (#234, fixed #236): promote.yml retag must source from
    `sha-<short>` (digest-stable per Contract v6), never from the floating
    `stg-latest` tag. The TOCTOU race surfaced when a concurrent
    service-repo ghcr-publish bumped stg-latest mid-window.
    """
    failures: list[Failure] = []
    pm = (WORKFLOWS / "promote.yml").read_text(encoding="utf-8")

    # Locate "Resolve source tag" step inside the retag job.
    src_step_match = re.search(
        r"name:\s*Resolve source tag\s*\n.*?run:\s*\|.*?(?=\n      - name:|\Z)",
        pm,
        re.DOTALL,
    )
    if not src_step_match:
        failures.append(
            Failure(
                "promote-resolve-src-present",
                "#234",
                "promote.yml has no `Resolve source tag` step — workflow shape "
                "changed. Update the cold-rebuild dryrun's expectations.",
            )
        )
        return failures

    src_block = src_step_match.group(0)

    # The source_tag assignment must always be sha-${SHORT}. We accept
    # either bare `source_tag="sha-${SHORT}"` (the post-#236 shape) or any
    # branch that ALWAYS resolves to sha-<short>. The negative-invariant
    # we enforce: source_tag must NEVER be assigned to "stg-latest" or any
    # `<env>-latest` floating-tag form.
    if re.search(r'source_tag\s*=\s*"(stg|prod)-latest"', src_block):
        failures.append(
            Failure(
                "promote-no-floating-source",
                "#234",
                "promote.yml `Resolve source tag` step assigns source_tag to a "
                "floating tag (stg-latest or prod-latest). This re-introduces "
                "the #234 TOCTOU race: a concurrent service-repo ghcr-publish "
                "between this step and the parity check moves the floating "
                "tag's digest, breaking parity. Always source from sha-<short>.",
            )
        )

    # Positive invariant: the assignment to source_tag must reference
    # ${SHORT} (the plan-resolved digest-stable SHA).
    if not re.search(r'source_tag\s*=\s*"sha-\$\{?SHORT\}?"', src_block):
        failures.append(
            Failure(
                "promote-source-tag-uses-short",
                "#234",
                "promote.yml `Resolve source tag` step does not assign source_tag "
                'to `"sha-${SHORT}"`. The fix from #236 requires the source tag '
                "to be the digest-stable sha-<short> form.",
            )
        )

    return failures


def check_db_migrate_driver_aligned() -> list[Failure]:
    """Bug #5 (#235, fixed #236): db-migrate.yml's DATABASE_URL driver
    scheme must match a driver actually shipped in the user-service
    image's pyproject.toml. The original bug used `postgresql+psycopg://`
    while the image only had asyncpg.
    """
    failures: list[Failure] = []
    db_yml_text = (WORKFLOWS / "db-migrate.yml").read_text(encoding="utf-8")

    # Extract the driver scheme from any DATABASE_URL= assignment in
    # db-migrate.yml. We accept `postgresql+<driver>://...` or bare
    # `postgresql://` (which SQLAlchemy maps to the default driver).
    schemes: set[str] = set()
    for m in re.finditer(
        r'DATABASE_URL\s*=\s*"postgresql(?:\+([a-z][a-z0-9_]*))?://',
        db_yml_text,
    ):
        driver = m.group(1)
        # Bare postgresql:// in SQLAlchemy 2.x routes to psycopg2 by
        # default — record as `psycopg2` so the pyproject check applies.
        schemes.add(driver if driver else "psycopg2")

    if not schemes:
        failures.append(
            Failure(
                "db-migrate-database-url-present",
                "#235",
                "db-migrate.yml has no DATABASE_URL= assignment recognizable to "
                "this guard. If the workflow shape changed, update the regex.",
            )
        )
        return failures

    pyproject_path = os.environ.get("USER_SERVICE_PYPROJECT", "")
    if not pyproject_path or not Path(pyproject_path).is_file():
        failures.append(
            Failure(
                "db-migrate-pyproject-resolvable",
                "#235",
                f"USER_SERVICE_PYPROJECT={pyproject_path!r} does not resolve. The "
                "wave-aware sibling checkout for user-service likely failed; this "
                "guard cannot run without it.",
            )
        )
        return failures
    pyproject = Path(pyproject_path).read_text(encoding="utf-8")

    # Map known SQLAlchemy driver scheme suffixes to the package name we
    # expect in pyproject.toml. The set covers what's been used in this
    # codebase plus the likely-next options (psycopg2 / psycopg / asyncpg).
    SCHEME_TO_PKG = {
        "asyncpg": "asyncpg",
        "psycopg": "psycopg",  # psycopg 3
        "psycopg2": "psycopg2",
        "pg8000": "pg8000",
    }

    for scheme in sorted(schemes):
        pkg = SCHEME_TO_PKG.get(scheme)
        if pkg is None:
            failures.append(
                Failure(
                    "db-migrate-driver-known",
                    "#235",
                    f"db-migrate.yml uses unrecognized DATABASE_URL driver "
                    f"`postgresql+{scheme}` — extend SCHEME_TO_PKG in this "
                    "guard before merging the change.",
                )
            )
            continue
        # pyproject.toml should declare the package as a dependency. We
        # match it as a quoted string at start of a dep line — the same
        # pattern user-service uses (`"asyncpg>=0.30.0",`).
        if not re.search(rf'"\s*{re.escape(pkg)}\s*[<>=!~]', pyproject):
            failures.append(
                Failure(
                    "db-migrate-driver-shipped",
                    "#235",
                    f"db-migrate.yml's DATABASE_URL uses `postgresql+{scheme}://` "
                    f"but `{pkg}` is NOT declared in user-service/pyproject.toml. "
                    "Running alembic against the user-service image will hit "
                    "`ModuleNotFoundError` (the original #235 failure). Either "
                    "switch the URL to a driver the image ships, or add the "
                    "driver to user-service's pyproject in the same wave.",
                )
            )

    return failures


def check_stg_verify_artifact_v2_contract() -> list[Failure]:
    """Contract guard for the stg-verify artifact schema v2 (deploy#199).

    The v2 contract is split across two workflow files and must stay
    consistent on both sides:

    - verify-deploy.yml MUST emit `schema_version: 2` artifacts with a
      `service_digests` object covering all four service keys.
    - promote.yml gate MUST dispatch on schema_version, accept v2 strict
      per-service equality, fall back to v1 freshness-window, and
      compare the four service keys against plan-resolved digests.

    A drift in either file (e.g., bumping the verify schema to v3 without
    updating the gate, or adding a new service to the plan mapping
    without updating verify-deploy's digest loop) silently degrades the
    gate to v1 fallback for the new service — exactly the T1-window
    risk #199 closed.

    The four service keys mapping MUST match between verify-deploy.yml's
    digest-resolution loop and promote.yml's plan job mapping dict.
    """
    failures: list[Failure] = []
    vd = (WORKFLOWS / "verify-deploy.yml").read_text(encoding="utf-8")
    pm = (WORKFLOWS / "promote.yml").read_text(encoding="utf-8")

    expected_services = {"api", "frontend", "user-service", "landing"}

    # ── verify-deploy.yml side ──────────────────────────────────────
    if '"schema_version": 2' not in vd and '"schema_version":2' not in vd:
        failures.append(
            Failure(
                "vd-v2-schema-version",
                "#199",
                "verify-deploy.yml does not emit `schema_version: 2` in the "
                "stg-verify-result artifact. The promote.yml v2 gate (deploy#199) "
                "depends on this field to dispatch between strict per-service "
                "equality (v2) and freshness-window fallback (v1).",
            )
        )

    if "service_digests" not in vd:
        failures.append(
            Failure(
                "vd-v2-service-digests-field",
                "#199",
                "verify-deploy.yml does not include a `service_digests` field "
                "in the emitted artifact. The v2 gate requires this field to "
                "compare against plan-resolved digests.",
            )
        )

    # Each of the four service keys must appear in verify-deploy.yml's
    # digest resolution loop. Loose substring match — the entries are
    # `"<key>:noorinalabs/..."` shell-string literals.
    vd_missing = [s for s in sorted(expected_services) if f'"{s}:noorinalabs/' not in vd]
    if vd_missing:
        failures.append(
            Failure(
                "vd-v2-service-coverage",
                "#199",
                f"verify-deploy.yml's digest-resolution loop is missing the "
                f"following service keys: {vd_missing}. The v2 contract requires "
                f"all four services ({sorted(expected_services)}) to be covered "
                "on both sides; a missing service silently degrades the gate to "
                "v1 fallback for that service.",
            )
        )

    # ── promote.yml side ────────────────────────────────────────────
    # The plan job's mapping dict must cover the same four keys. We
    # look for the literal mapping shape.
    pm_missing = [
        s
        for s in sorted(expected_services)
        if f'"{s}":' not in pm  # mapping = { "api": "noorinalabs/...", ... }
    ]
    if pm_missing:
        failures.append(
            Failure(
                "pm-v2-plan-mapping-coverage",
                "#199",
                f"promote.yml's plan job mapping is missing the following "
                f"service keys: {pm_missing}. Must match verify-deploy.yml's "
                "digest-resolution loop on the same set.",
            )
        )

    # The gate must dispatch on schema_version. We look for a case-style
    # accept of both 1 and 2.
    if not re.search(r'case\s+"\$schema_version"\s+in\s*\n\s*1\|2\)', pm):
        failures.append(
            Failure(
                "pm-v2-schema-dispatch",
                "#199",
                "promote.yml gate does not have a `case` block accepting both "
                "schema_version 1 and 2. The dispatch is required so a v1 "
                "artifact (legacy) falls back to freshness-window while v2 "
                "enforces strict per-service digest equality.",
            )
        )

    # The gate must declare per-service digest outputs from plan AND
    # consume them as env vars in the gate step.
    for svc_env in ("API_DIGEST", "FRONTEND_DIGEST", "USER_SERVICE_DIGEST", "LANDING_DIGEST"):
        if svc_env not in pm:
            failures.append(
                Failure(
                    "pm-v2-digest-env-coverage",
                    "#199",
                    f"promote.yml gate step does not reference the `{svc_env}` "
                    "env var. The v2 gate compares plan-resolved digests against "
                    "the artifact's service_digests on a per-service basis; all "
                    "four service digest env vars must be plumbed through.",
                )
            )

    # The gate must have a v1-fallback warning annotation pointing at #199.
    if "ship deploy#199" not in pm.lower() and "ship #199" not in pm:
        failures.append(
            Failure(
                "pm-v2-fallback-warning",
                "#199",
                "promote.yml gate's v1 fallback path does not emit a warning "
                "annotation directing operators to ship #199 to all stg deploys. "
                "Without the warning, operators won't see the upgrade path while "
                "the rollout is still in progress.",
            )
        )

    return failures


def main() -> int:
    sections = [
        ("Terraform: no ephemeral keypair (bug #1, #216)", check_terraform_no_ephemeral_keypair),
        ("Promote: retag uses GH_PACKAGES_TOKEN (bug #2)", check_promote_token_scope),
        ("Promote: no floating-tag source (bug #3, #234)", check_promote_no_floating_source),
        ("DB-migrate: driver matches pyproject (bug #5, #235)", check_db_migrate_driver_aligned),
        ("Stg-verify artifact v2 contract (#199)", check_stg_verify_artifact_v2_contract),
    ]
    all_failures: list[Failure] = []
    for label, fn in sections:
        try:
            f = fn()
        except Exception as exc:
            print(f"[ERROR] {label}: check raised: {exc}", file=sys.stderr)
            return 2
        if f:
            print(f"[FAIL]  {label}")
            for failure in f:
                print(failure.render())
            all_failures.extend(f)
        else:
            print(f"[OK]    {label}")

    if all_failures:
        print(
            f"\nCold-rebuild static checks: {len(all_failures)} failure(s).",
            file=sys.stderr,
        )
        print(
            "See docs/runbooks/cold-rebuild-dry-run.md for triage steps per bug.",
            file=sys.stderr,
        )
        return 1
    print("\nCold-rebuild static checks: all guards green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
