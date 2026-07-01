#!/usr/bin/env python3
"""Deterministic, TTL-driven secret-rotation engine (deploy#513).

This is the UNIT-MECHANIC core of the rotation system the owner scoped in
deploy#513 and ADR 0007 (stay on GH Environment secrets; build rotation
discipline + automation *on top of* the store, no new secrets manager). It reads
the machine-readable inventory (``scripts/secret_rotation_inventory.yaml``, the
structured counterpart of ``docs/secrets-inventory.md``) and computes — purely,
for an injected clock — which secrets are rotation-due, what the per-class
refresh method is, and when a completed rotation should next fire. It NEVER
executes a live rotation and NEVER prints a secret value.

What lives here (PR scope — pure, testable, no side effects):

  * Inventory load + schema validation (deterministic, name-sorted).
  * TTL / cadence model — cadence → TTL days; ``next_due`` = last_rotated + TTL;
    a task is scheduled at ``next_due − lead_time`` (the "TTL − ~1 day" of the
    issue). Secrets with no calendar cadence (on_demand / on_deploy / provider /
    per_run) are never due by the clock — only by an on-demand trigger.
  * Rotation-due status for a given ``now`` (OK / DUE / OVERDUE / NO_CADENCE /
    UNKNOWN-when-never-rotated).
  * Self-rescheduling: ``next_rotation_after(completed_at)`` — the next fire time
    a completed task reschedules itself to.
  * Per-credential-class refresh registry: ``generate_and_replace`` mints a value
    (CSPRNG by default; a seeded RNG is injectable for tests/preview ONLY), and
    the mint→verify→store/apply plan generalizes ``scripts/rotate_db_password.sh``
    + the fix_b2_plan_key mint→verify→store pattern from #510. ``provider_reroll``
    / ``provider_managed`` are console/provider steps flagged for a human.
  * A rotation *plan* — an ordered, value-free description of how one secret is
    refreshed and applied — that NEVER contains a secret value.
  * The owner-notify classification (§ "notify at inflection points"): a due
    secret needs a human iff its refresh or apply crosses an inflection point
    (``requires_owner_gate``). Fully-programmatic due secrets are listed apart.
  * The session-start surface (``rotation_surface``): overdue + near-due secrets,
    the payload a session-start check would render.

What is DEFERRED to a scoped follow-up (runtime / operational, gated on prod
state or cross-repo wiring — out of PR-acceptance scope per the runtime-gate
convention): the live GitHub Actions self-rescheduling cron workflow that *runs*
these tasks; owner-notification delivery (Slack/email/issue); the session-start
hook wiring in noorinalabs-main that renders ``rotation_surface``; provider-
specific re-roll execution (CF/B2 console automation); and auto-generating the
inventory skeleton from ``gh secret list``. This module is the deterministic
decision engine those layers call — it is complete and fully tested on its own.

CLI (all read-only; ``--now`` makes output deterministic):

    python3 scripts/secret_rotation.py validate
    python3 scripts/secret_rotation.py status [--now YYYY-MM-DD] [--json]
    python3 scripts/secret_rotation.py due    [--now YYYY-MM-DD] [--json]
    python3 scripts/secret_rotation.py plan <SECRET_NAME> [--now YYYY-MM-DD] [--json]
    python3 scripts/secret_rotation.py next-due <SECRET_NAME> --completed-at YYYY-MM-DD
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
import string
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

# ── Constants ──────────────────────────────────────────────────────────────

# URL-safe alphabet — matches scripts/rotate_db_password.sh's [A-Za-z0-9_-] so a
# minted value can never corrupt compose PG_DSN/REDIS_URL interpolation (the
# us#65 / ig#956 url-unsafe-password class of bug) and needs no shell/SQL quoting.
URL_SAFE_ALPHABET = string.ascii_letters + string.digits + "-_"

DEFAULT_LEAD_TIME_DAYS = 1
DEFAULT_GENERATED_LENGTH = 40

INVENTORY_PATH = Path(__file__).resolve().parent / "secret_rotation_inventory.yaml"

# Refresh methods (§2 of deploy#513).
GENERATE_AND_REPLACE = "generate_and_replace"
PROVIDER_REROLL = "provider_reroll"
PROVIDER_MANAGED = "provider_managed"
REFRESH_METHODS = frozenset({GENERATE_AND_REPLACE, PROVIDER_REROLL, PROVIDER_MANAGED})

# Cadence → TTL (days). None means "no calendar TTL" — never due by the clock.
CADENCE_TTL_DAYS: dict[str, int | None] = {
    "annual": 365,
    "semiannual": 182,
    "quarterly": 90,
    "on_demand": None,
    "on_deploy": None,
    "provider": None,
    "per_run": None,
}

SCOPES = frozenset({"repo", "environment"})

# Rotation-due status values.
STATUS_OK = "OK"  # not yet within the lead window
STATUS_DUE = "DUE"  # inside [next_due − lead, next_due): schedule now
STATUS_OVERDUE = "OVERDUE"  # past next_due
STATUS_NO_CADENCE = "NO_CADENCE"  # no calendar TTL (on-demand/provider/etc.)
STATUS_UNKNOWN = "UNKNOWN"  # calendar cadence but no last_rotated recorded

# Statuses that put a secret on the session-start surface / the `due` list.
ACTIONABLE_STATUSES = frozenset({STATUS_DUE, STATUS_OVERDUE, STATUS_UNKNOWN})


class InventoryError(ValueError):
    """Raised when the inventory file is malformed or fails schema validation."""


# ── Model ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Secret:
    """One rotatable (or explicitly non-rotatable) credential from the inventory."""

    name: str
    scope: str
    secret_class: str
    owner_role: str
    env_separated: bool
    refresh_method: str
    cadence: str
    apply_process: str
    requires_owner_gate: bool
    last_rotated: date | None
    runbook: str | None
    rotatable: bool
    notes: str | None

    @property
    def ttl_days(self) -> int | None:
        """Calendar TTL in days, or None for non-calendar cadences."""
        return CADENCE_TTL_DAYS[self.cadence]

    def next_due(self) -> date | None:
        """Date the secret is next due (last_rotated + TTL), or None."""
        ttl = self.ttl_days
        if ttl is None or self.last_rotated is None:
            return None
        return self.last_rotated + timedelta(days=ttl)

    def scheduled_at(self, lead_time_days: int) -> date | None:
        """Date a rotation task should fire: next_due − lead_time."""
        due = self.next_due()
        if due is None:
            return None
        return due - timedelta(days=lead_time_days)

    def status(self, now: date, lead_time_days: int) -> str:
        """Rotation-due status for ``now`` (see STATUS_* constants)."""
        if not self.rotatable:
            return STATUS_NO_CADENCE
        if self.ttl_days is None:
            return STATUS_NO_CADENCE
        if self.last_rotated is None:
            # Calendar cadence declared but no baseline date — cannot be timed;
            # surfaced as UNKNOWN so it is not silently ignored.
            return STATUS_UNKNOWN
        due = self.next_due()
        assert due is not None  # ttl + last_rotated both present here
        if now >= due:
            return STATUS_OVERDUE
        if now >= due - timedelta(days=lead_time_days):
            return STATUS_DUE
        return STATUS_OK


# ── Inventory load + validation ────────────────────────────────────────────


def _require(mapping: dict[str, Any], key: str, name: str) -> Any:
    if key not in mapping:
        raise InventoryError(f"secret {name!r}: missing required field {key!r}")
    return mapping[key]


def _parse_date(value: Any, name: str, field_name: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise InventoryError(
                f"secret {name!r}: {field_name} {value!r} is not an ISO date"
            ) from exc
    raise InventoryError(f"secret {name!r}: {field_name} must be an ISO date or null")


def _parse_secret(raw: dict[str, Any]) -> Secret:
    if not isinstance(raw, dict):
        raise InventoryError(f"each secret must be a mapping, got {type(raw).__name__}")
    name = _require(raw, "name", "<unnamed>")
    if not isinstance(name, str) or not name:
        raise InventoryError(f"invalid secret name: {name!r}")

    scope = _require(raw, "scope", name)
    if scope not in SCOPES:
        raise InventoryError(f"secret {name!r}: scope {scope!r} not in {sorted(SCOPES)}")

    refresh_method = _require(raw, "refresh_method", name)
    if refresh_method not in REFRESH_METHODS:
        raise InventoryError(
            f"secret {name!r}: refresh_method {refresh_method!r} not in {sorted(REFRESH_METHODS)}"
        )

    cadence = _require(raw, "cadence", name)
    if cadence not in CADENCE_TTL_DAYS:
        raise InventoryError(
            f"secret {name!r}: cadence {cadence!r} not in {sorted(CADENCE_TTL_DAYS)}"
        )

    return Secret(
        name=name,
        scope=scope,
        secret_class=str(_require(raw, "secret_class", name)),
        owner_role=str(_require(raw, "owner_role", name)),
        env_separated=bool(_require(raw, "env_separated", name)),
        refresh_method=refresh_method,
        cadence=cadence,
        apply_process=str(_require(raw, "apply_process", name)),
        requires_owner_gate=bool(_require(raw, "requires_owner_gate", name)),
        last_rotated=_parse_date(raw.get("last_rotated"), name, "last_rotated"),
        runbook=(str(raw["runbook"]) if raw.get("runbook") else None),
        rotatable=bool(raw.get("rotatable", True)),
        notes=(str(raw["notes"]) if raw.get("notes") else None),
    )


def load_inventory(path: Path | None = None) -> list[Secret]:
    """Load, validate, and return the inventory sorted by secret name.

    Sorting makes every downstream computation and CLI rendering deterministic.
    """
    path = path or INVENTORY_PATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InventoryError(f"inventory not found: {path}") from exc

    if not isinstance(data, dict) or "secrets" not in data:
        raise InventoryError("inventory must be a mapping with a top-level 'secrets' list")
    raw_secrets = data["secrets"]
    if not isinstance(raw_secrets, list) or not raw_secrets:
        raise InventoryError("'secrets' must be a non-empty list")

    secrets_list = [_parse_secret(raw) for raw in raw_secrets]

    names = [s.name for s in secrets_list]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise InventoryError(f"duplicate secret names in inventory: {dupes}")

    return sorted(secrets_list, key=lambda s: s.name)


def lead_time_days(path: Path | None = None) -> int:
    """Read the configured default lead time (days), falling back to the constant."""
    path = path or INVENTORY_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    defaults = data.get("defaults", {}) if isinstance(data, dict) else {}
    value = defaults.get("lead_time_days", DEFAULT_LEAD_TIME_DAYS)
    return int(value)


# ── Value generation + redaction (never leak a value) ──────────────────────


def generate_value(
    length: int = DEFAULT_GENERATED_LENGTH,
    *,
    rng: random.Random | None = None,
) -> str:
    """Mint a URL-safe secret value.

    Default source is a CSPRNG (``secrets.choice``) — the real, cryptographically
    secure path. A ``random.Random`` may be injected for DETERMINISTIC tests /
    previews ONLY; a seeded PRNG is NOT cryptographically secure and must never
    mint a value that is actually stored. Callers that apply for real must use
    the default (rng=None).
    """
    if length < 1:
        raise ValueError("length must be >= 1")
    if rng is None:
        return "".join(secrets.choice(URL_SAFE_ALPHABET) for _ in range(length))
    return "".join(rng.choice(URL_SAFE_ALPHABET) for _ in range(length))


def redact(value: str) -> str:
    """Return a leak-safe descriptor of a value — never the value itself.

    Reveals only the length, so logs/plans can reference "a value was produced"
    without exposing it. Used anywhere a value might otherwise reach output.
    """
    return f"<redacted:len={len(value)}>"


# ── Refresh plan (value-free, ordered steps) ───────────────────────────────

# Generic mint→verify→store/apply template per refresh method. Generalizes
# scripts/rotate_db_password.sh (health-gated apply + rollback) and the
# fix_b2_plan_key mint→verify→store pattern from #510: no manual copy-paste
# between minting a value and storing it.
_REFRESH_STEPS: dict[str, list[str]] = {
    GENERATE_AND_REPLACE: [
        "mint: generate a new URL-safe value (CSPRNG)",
        "verify: confirm the new value authorizes against the live target",
        "store: write the value atomically to the GH secret (mint→store, no manual copy)",
        "apply: effect the change per apply_process (health-gated, with rollback)",
    ],
    PROVIDER_REROLL: [
        "mint: re-roll the credential in the upstream provider console (HUMAN)",
        "capture: capture the new value at creation (write-only providers cannot re-read)",
        "verify: confirm the new value authorizes",
        "store: write the value atomically to the GH secret",
        "apply: effect the change per apply_process",
    ],
    PROVIDER_MANAGED: [
        "regenerate: rotate via the owning provider's lifecycle (HUMAN)",
        "store: update the GH secret with the provider-issued value",
        "apply: re-materialize on the next deploy per apply_process",
    ],
}


def build_plan(secret: Secret, now: date, lead_time: int) -> dict[str, Any]:
    """Build a value-free rotation plan for one secret.

    The returned mapping describes HOW the secret is refreshed + applied and
    WHETHER a human inflection point is involved — it never contains a secret
    value. ``fully_programmatic`` is true only when the whole path can run
    without a human (generate_and_replace AND no owner gate).
    """
    fully_programmatic = (
        secret.rotatable
        and secret.refresh_method == GENERATE_AND_REPLACE
        and not secret.requires_owner_gate
    )
    due = secret.next_due()
    scheduled = secret.scheduled_at(lead_time)
    return {
        "secret": secret.name,
        "scope": secret.scope,
        "refresh_method": secret.refresh_method,
        "cadence": secret.cadence,
        "ttl_days": secret.ttl_days,
        "status": secret.status(now, lead_time),
        "next_due": due.isoformat() if due else None,
        "scheduled_at": scheduled.isoformat() if scheduled else None,
        "requires_owner_gate": secret.requires_owner_gate,
        "fully_programmatic": fully_programmatic,
        "apply_process": secret.apply_process,
        "runbook": secret.runbook,
        "steps": list(_REFRESH_STEPS[secret.refresh_method]),
    }


# ── Scheduling / self-reschedule ───────────────────────────────────────────


def next_rotation_after(secret: Secret, completed_at: date, lead_time: int) -> dict[str, Any]:
    """Compute the next fire time a completed rotation reschedules itself to.

    A rotation task, on completion, reschedules to (completed_at + TTL − lead).
    Returns None fields for non-calendar cadences (nothing to self-schedule).
    """
    ttl = secret.ttl_days
    if ttl is None or not secret.rotatable:
        return {
            "secret": secret.name,
            "cadence": secret.cadence,
            "reschedules": False,
            "next_due": None,
            "next_scheduled_at": None,
        }
    next_due = completed_at + timedelta(days=ttl)
    return {
        "secret": secret.name,
        "cadence": secret.cadence,
        "reschedules": True,
        "next_due": next_due.isoformat(),
        "next_scheduled_at": (next_due - timedelta(days=lead_time)).isoformat(),
    }


# ── Surfacing + notification classification ────────────────────────────────


def rotation_surface(secrets_list: list[Secret], now: date, lead_time: int) -> list[dict[str, Any]]:
    """Session-start surface: the actionable (overdue/due/unknown) secrets.

    This is the payload a session-start staleness check would render — sorted by
    status severity then name, value-free. OK and NO_CADENCE secrets are omitted.
    """
    severity = {STATUS_OVERDUE: 0, STATUS_DUE: 1, STATUS_UNKNOWN: 2}
    rows = []
    for s in secrets_list:
        st = s.status(now, lead_time)
        if st not in ACTIONABLE_STATUSES:
            continue
        due = s.next_due()
        rows.append(
            {
                "secret": s.name,
                "status": st,
                "next_due": due.isoformat() if due else None,
                "requires_owner_gate": s.requires_owner_gate,
                "owner_role": s.owner_role,
            }
        )
    rows.sort(key=lambda r: (severity[str(r["status"])], str(r["secret"])))
    return rows


def classify_notifications(
    surface_rows: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Split a surface into owner-notify vs fully-programmatic actionable secrets.

    A due secret needs a human at an inflection point iff ``requires_owner_gate``
    (provider console re-roll, prod-gate approval, promote sign-off). The rest
    can be actioned by the automation without prompting.
    """
    notify = sorted(r["secret"] for r in surface_rows if r["requires_owner_gate"])
    programmatic = sorted(r["secret"] for r in surface_rows if not r["requires_owner_gate"])
    return {"owner_notify": notify, "programmatic": programmatic}


# ── CLI ────────────────────────────────────────────────────────────────────


def _parse_now(value: str | None) -> date:
    if value is None:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"--now {value!r} is not an ISO date (YYYY-MM-DD)") from exc


def _cmd_validate(_args: argparse.Namespace) -> int:
    secrets_list = load_inventory()
    rotatable = [s for s in secrets_list if s.rotatable]
    print(f"OK: {len(secrets_list)} secrets ({len(rotatable)} rotatable) — schema valid")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    now = _parse_now(args.now)
    lead = lead_time_days()
    secrets_list = load_inventory()
    rows = []
    for s in secrets_list:
        due = s.next_due()
        rows.append(
            {
                "secret": s.name,
                "scope": s.scope,
                "refresh_method": s.refresh_method,
                "cadence": s.cadence,
                "status": s.status(now, lead),
                "next_due": due.isoformat() if due else None,
            }
        )
    if args.json:
        print(json.dumps({"now": now.isoformat(), "secrets": rows}, indent=2, sort_keys=True))
        return 0
    print(f"Secret rotation status @ {now.isoformat()} (lead {lead}d)")
    for r in rows:
        due_str = r["next_due"] or "-"
        print(f"  {r['status']:<10} {r['secret']:<28} {r['cadence']:<11} next_due={due_str}")
    return 0


def _cmd_due(args: argparse.Namespace) -> int:
    now = _parse_now(args.now)
    lead = lead_time_days()
    secrets_list = load_inventory()
    surface = rotation_surface(secrets_list, now, lead)
    buckets = classify_notifications(surface)
    if args.json:
        print(
            json.dumps(
                {"now": now.isoformat(), "surface": surface, "notifications": buckets},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not surface:
        print(f"No secrets due @ {now.isoformat()}.")
        return 0
    print(f"Rotation due @ {now.isoformat()} ({len(surface)} secret(s)):")
    for r in surface:
        gate = "owner-gate" if r["requires_owner_gate"] else "programmatic"
        print(f"  {r['status']:<8} {r['secret']:<28} next_due={r['next_due'] or '-'} [{gate}]")
    print(f"  owner-notify: {buckets['owner_notify']}")
    print(f"  programmatic: {buckets['programmatic']}")
    return 0


def _find(secrets_list: list[Secret], name: str) -> Secret:
    for s in secrets_list:
        if s.name == name:
            return s
    raise SystemExit(f"unknown secret: {name!r}")


def _cmd_plan(args: argparse.Namespace) -> int:
    now = _parse_now(args.now)
    lead = lead_time_days()
    secrets_list = load_inventory()
    plan = build_plan(_find(secrets_list, args.secret), now, lead)
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    print(f"Rotation plan: {plan['secret']} ({plan['refresh_method']})")
    print(
        f"  status={plan['status']} next_due={plan['next_due']} scheduled_at={plan['scheduled_at']}"
    )
    print(
        f"  fully_programmatic={plan['fully_programmatic']} "
        f"owner_gate={plan['requires_owner_gate']}"
    )
    print(f"  apply: {plan['apply_process']}")
    for i, step in enumerate(plan["steps"], 1):
        print(f"    {i}. {step}")
    return 0


def _cmd_next_due(args: argparse.Namespace) -> int:
    lead = lead_time_days()
    secrets_list = load_inventory()
    secret = _find(secrets_list, args.secret)
    completed = _parse_now(args.completed_at)
    result = next_rotation_after(secret, completed, lead)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secret_rotation.py",
        description="Deterministic, TTL-driven secret-rotation engine (deploy#513).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="validate the inventory schema")
    p_validate.set_defaults(func=_cmd_validate)

    p_status = sub.add_parser("status", help="rotation status for every secret")
    p_status.add_argument("--now", help="clock date (YYYY-MM-DD); default today")
    p_status.add_argument("--json", action="store_true", help="emit JSON")
    p_status.set_defaults(func=_cmd_status)

    p_due = sub.add_parser("due", help="secrets due/overdue (session-start surface)")
    p_due.add_argument("--now", help="clock date (YYYY-MM-DD); default today")
    p_due.add_argument("--json", action="store_true", help="emit JSON")
    p_due.set_defaults(func=_cmd_due)

    p_plan = sub.add_parser("plan", help="value-free refresh plan for one secret")
    p_plan.add_argument("secret", help="secret name")
    p_plan.add_argument("--now", help="clock date (YYYY-MM-DD); default today")
    p_plan.add_argument("--json", action="store_true", help="emit JSON")
    p_plan.set_defaults(func=_cmd_plan)

    p_next = sub.add_parser("next-due", help="next fire time after a completed rotation")
    p_next.add_argument("secret", help="secret name")
    p_next.add_argument("--completed-at", required=True, help="completion date (YYYY-MM-DD)")
    p_next.set_defaults(func=_cmd_next_due)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except InventoryError as exc:
        print(f"inventory error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
