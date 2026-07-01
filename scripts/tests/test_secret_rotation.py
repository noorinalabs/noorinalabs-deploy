"""Unit tests for scripts/secret_rotation.py (deploy#513).

Coverage targets the three properties the rotation engine must guarantee:

  * **Determinism** — a fixed clock (and, for value generation, a fixed seed)
    produces byte-identical output; no wall-clock or CSPRNG leaks into the
    scheduled/decision path.
  * **TTL-due logic** — cadence → TTL, ``next_due`` = last_rotated + TTL, the
    OK/DUE/OVERDUE boundaries around ``next_due − lead``, and the no-calendar
    (on_demand/provider/on_deploy) and never-rotated (UNKNOWN) cases.
  * **No secret leak** — values are only ever produced by the explicit generator
    (CSPRNG by default), redaction never reveals a value, and a rotation *plan*
    never carries one.

Plus inventory-schema validation and reconciliation against the human-curated
docs/secrets-inventory.md (the two inventories must not drift).
"""

from __future__ import annotations

import json
import random
import re
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import secret_rotation as sr  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _mk(
    name: str = "TEST_SECRET",
    *,
    refresh_method: str = sr.GENERATE_AND_REPLACE,
    cadence: str = "quarterly",
    last_rotated: date | None = date(2026, 1, 1),
    requires_owner_gate: bool = False,
    rotatable: bool = True,
    scope: str = "environment",
) -> sr.Secret:
    return sr.Secret(
        name=name,
        scope=scope,
        secret_class="test class",
        owner_role="Security Engineer",
        env_separated=True,
        refresh_method=refresh_method,
        cadence=cadence,
        apply_process="do the thing",
        requires_owner_gate=requires_owner_gate,
        last_rotated=last_rotated,
        runbook=None,
        rotatable=rotatable,
        notes=None,
    )


# ── Inventory load + schema validation ─────────────────────────────────────


def test_real_inventory_loads_and_is_sorted() -> None:
    secrets = sr.load_inventory()
    assert secrets, "inventory should be non-empty"
    names = [s.name for s in secrets]
    assert names == sorted(names), "load_inventory must return name-sorted secrets"
    assert len(names) == len(set(names)), "no duplicate names"


def test_real_inventory_lead_time() -> None:
    assert sr.lead_time_days() == 1


def test_invalid_scope_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "inv.yaml"
    bad.write_text(
        "secrets:\n"
        "  - name: X\n    scope: galaxy\n    secret_class: c\n    owner_role: r\n"
        "    env_separated: true\n    refresh_method: generate_and_replace\n"
        "    cadence: quarterly\n    apply_process: a\n    requires_owner_gate: false\n",
        encoding="utf-8",
    )
    with pytest.raises(sr.InventoryError, match="scope"):
        sr.load_inventory(bad)


def test_invalid_refresh_method_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "inv.yaml"
    bad.write_text(
        "secrets:\n"
        "  - name: X\n    scope: repo\n    secret_class: c\n    owner_role: r\n"
        "    env_separated: false\n    refresh_method: telepathy\n"
        "    cadence: quarterly\n    apply_process: a\n    requires_owner_gate: false\n",
        encoding="utf-8",
    )
    with pytest.raises(sr.InventoryError, match="refresh_method"):
        sr.load_inventory(bad)


def test_bad_date_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "inv.yaml"
    bad.write_text(
        "secrets:\n"
        "  - name: X\n    scope: repo\n    secret_class: c\n    owner_role: r\n"
        "    env_separated: false\n    refresh_method: generate_and_replace\n"
        "    cadence: quarterly\n    apply_process: a\n    requires_owner_gate: false\n"
        "    last_rotated: not-a-date\n",
        encoding="utf-8",
    )
    with pytest.raises(sr.InventoryError, match="ISO date"):
        sr.load_inventory(bad)


def test_duplicate_names_rejected(tmp_path: Path) -> None:
    body = (
        "  - name: DUP\n    scope: repo\n    secret_class: c\n    owner_role: r\n"
        "    env_separated: false\n    refresh_method: generate_and_replace\n"
        "    cadence: quarterly\n    apply_process: a\n    requires_owner_gate: false\n"
    )
    bad = tmp_path / "inv.yaml"
    bad.write_text("secrets:\n" + body + body, encoding="utf-8")
    with pytest.raises(sr.InventoryError, match="duplicate"):
        sr.load_inventory(bad)


def _md_covers(name: str, md: str) -> bool:
    """True if ``name`` is documented in the markdown inventory.

    The markdown groups some keys behind a ``PREFIX_*`` glob cell
    (e.g. ``USER_POSTGRES_*``), so a verbatim substring check under-counts.
    Accept either a verbatim occurrence or a ``PREFIX_*`` token whose prefix the
    name starts with.
    """
    if name in md:
        return True
    for token in re.findall(r"[A-Z][A-Z0-9_]*\*", md):
        prefix = token[:-1]  # strip trailing '*'
        if name.startswith(prefix):
            return True
    return False


def test_inventory_reconciles_with_markdown() -> None:
    """Every machine-inventory secret name must be documented in docs/secrets-inventory.md.

    Guards the two inventories against drift (the markdown is the human companion
    the YAML is derived from). One direction only (YAML ⊆ markdown): the markdown
    groups some paired/wildcarded keys in one cell, honored via ``_md_covers``.
    """
    md = (REPO_ROOT / "docs" / "secrets-inventory.md").read_text(encoding="utf-8")
    missing = [s.name for s in sr.load_inventory() if not _md_covers(s.name, md)]
    assert not missing, f"YAML secrets absent from docs/secrets-inventory.md: {missing}"


# ── TTL / cadence model ────────────────────────────────────────────────────


def test_cadence_ttl_mapping() -> None:
    assert _mk(cadence="annual").ttl_days == 365
    assert _mk(cadence="semiannual").ttl_days == 182
    assert _mk(cadence="quarterly").ttl_days == 90
    assert _mk(cadence="on_demand").ttl_days is None
    assert _mk(cadence="on_deploy").ttl_days is None
    assert _mk(cadence="provider").ttl_days is None
    assert _mk(cadence="per_run").ttl_days is None


def test_next_due_and_scheduled_at() -> None:
    s = _mk(cadence="quarterly", last_rotated=date(2026, 1, 1))
    assert s.next_due() == date(2026, 4, 1)  # +90d
    assert s.scheduled_at(1) == date(2026, 3, 31)  # next_due − 1d


def test_next_due_none_without_ttl_or_last_rotated() -> None:
    assert _mk(cadence="on_demand").next_due() is None
    assert _mk(cadence="quarterly", last_rotated=None).next_due() is None


# ── Status boundaries (the load-bearing due logic) ─────────────────────────


def test_status_ok_before_lead_window() -> None:
    s = _mk(cadence="quarterly", last_rotated=date(2026, 1, 1))  # due 2026-04-01
    assert s.status(date(2026, 3, 30), 1) == sr.STATUS_OK


def test_status_due_inside_lead_window() -> None:
    s = _mk(cadence="quarterly", last_rotated=date(2026, 1, 1))  # due 2026-04-01
    # lead=1 → window opens 2026-03-31
    assert s.status(date(2026, 3, 31), 1) == sr.STATUS_DUE


def test_status_overdue_on_and_after_due_date() -> None:
    s = _mk(cadence="quarterly", last_rotated=date(2026, 1, 1))  # due 2026-04-01
    assert s.status(date(2026, 4, 1), 1) == sr.STATUS_OVERDUE
    assert s.status(date(2026, 5, 1), 1) == sr.STATUS_OVERDUE


def test_status_no_cadence_for_on_demand() -> None:
    assert _mk(cadence="on_demand").status(date(2026, 6, 1), 1) == sr.STATUS_NO_CADENCE


def test_status_no_cadence_for_non_rotatable() -> None:
    s = _mk(cadence="per_run", rotatable=False)
    assert s.status(date(2026, 6, 1), 1) == sr.STATUS_NO_CADENCE


def test_status_unknown_when_calendar_but_never_rotated() -> None:
    s = _mk(cadence="quarterly", last_rotated=None)
    assert s.status(date(2026, 6, 1), 1) == sr.STATUS_UNKNOWN


# ── Self-reschedule ────────────────────────────────────────────────────────


def test_next_rotation_after_reschedules_calendar_secret() -> None:
    s = _mk(cadence="quarterly")
    result = sr.next_rotation_after(s, date(2026, 6, 1), 1)
    assert result["reschedules"] is True
    assert result["next_due"] == "2026-08-30"  # +90d
    assert result["next_scheduled_at"] == "2026-08-29"


def test_next_rotation_after_noop_for_no_cadence() -> None:
    s = _mk(cadence="on_demand")
    result = sr.next_rotation_after(s, date(2026, 6, 1), 1)
    assert result["reschedules"] is False
    assert result["next_due"] is None


# ── Value generation: determinism + no leak ────────────────────────────────


def test_generate_value_deterministic_with_seed() -> None:
    v1 = sr.generate_value(40, rng=random.Random(1234))
    v2 = sr.generate_value(40, rng=random.Random(1234))
    assert v1 == v2, "same seed must produce the same value (determinism)"


def test_generate_value_uses_url_safe_alphabet_only() -> None:
    v = sr.generate_value(200, rng=random.Random(7))
    assert re.fullmatch(r"[A-Za-z0-9_-]+", v), "value must stay in the URL-safe alphabet"
    assert len(v) == 200


def test_generate_value_default_is_not_seeded_prng() -> None:
    # No rng → CSPRNG path; two draws must (overwhelmingly) differ.
    assert sr.generate_value(40) != sr.generate_value(40)


def test_generate_value_rejects_nonpositive_length() -> None:
    with pytest.raises(ValueError):
        sr.generate_value(0)


def test_redact_never_reveals_value() -> None:
    value = sr.generate_value(32, rng=random.Random(99))
    red = sr.redact(value)
    assert value not in red
    assert red == "<redacted:len=32>"


# ── Plan: value-free, correct programmatic classification ──────────────────


def test_plan_is_value_free_and_json_serializable() -> None:
    s = _mk(refresh_method=sr.GENERATE_AND_REPLACE)
    plan = sr.build_plan(s, date(2026, 3, 31), 1)
    blob = json.dumps(plan)
    # A freshly generated value must not be embedded anywhere in the plan.
    value = sr.generate_value(40, rng=random.Random(1))
    assert value not in blob
    assert "steps" in plan and plan["steps"]


def test_plan_fully_programmatic_only_for_generate_without_gate() -> None:
    prog = sr.build_plan(
        _mk(refresh_method=sr.GENERATE_AND_REPLACE, requires_owner_gate=False),
        date(2026, 3, 31),
        1,
    )
    assert prog["fully_programmatic"] is True

    gated = sr.build_plan(
        _mk(refresh_method=sr.GENERATE_AND_REPLACE, requires_owner_gate=True),
        date(2026, 3, 31),
        1,
    )
    assert gated["fully_programmatic"] is False

    provider = sr.build_plan(
        _mk(refresh_method=sr.PROVIDER_REROLL, requires_owner_gate=True),
        date(2026, 3, 31),
        1,
    )
    assert provider["fully_programmatic"] is False


def test_plan_steps_match_refresh_method() -> None:
    for method in (sr.GENERATE_AND_REPLACE, sr.PROVIDER_REROLL, sr.PROVIDER_MANAGED):
        plan = sr.build_plan(_mk(refresh_method=method), date(2026, 3, 31), 1)
        assert plan["steps"] == sr._REFRESH_STEPS[method]


# ── Surface + notification classification ──────────────────────────────────


def test_rotation_surface_orders_by_severity_then_name() -> None:
    secrets = [
        _mk("B_DUE", cadence="quarterly", last_rotated=date(2026, 1, 1)),  # due 04-01
        _mk("A_OVERDUE", cadence="quarterly", last_rotated=date(2025, 1, 1)),  # long overdue
        _mk("C_OK", cadence="quarterly", last_rotated=date(2026, 3, 1)),  # due 05-30
        _mk("D_NOCAL", cadence="on_demand"),
    ]
    surface = sr.rotation_surface(secrets, date(2026, 3, 31), 1)
    ordered = [r["secret"] for r in surface]
    assert ordered == ["A_OVERDUE", "B_DUE"]  # OK + on_demand omitted; overdue first


def test_rotation_surface_deterministic() -> None:
    secrets = sr.load_inventory()
    now = date(2026, 12, 31)
    lead = sr.lead_time_days()
    assert sr.rotation_surface(secrets, now, lead) == sr.rotation_surface(secrets, now, lead)


def test_classify_notifications_splits_on_owner_gate() -> None:
    surface = [
        {"secret": "GATED", "requires_owner_gate": True},
        {"secret": "AUTO", "requires_owner_gate": False},
    ]
    buckets = sr.classify_notifications(surface)
    assert buckets["owner_notify"] == ["GATED"]
    assert buckets["programmatic"] == ["AUTO"]


# ── CLI determinism ────────────────────────────────────────────────────────


def test_cli_status_json_deterministic(capsys: pytest.CaptureFixture[str]) -> None:
    assert sr.main(["status", "--now", "2026-06-30", "--json"]) == 0
    first = capsys.readouterr().out
    assert sr.main(["status", "--now", "2026-06-30", "--json"]) == 0
    second = capsys.readouterr().out
    assert first == second, "fixed --now must yield identical output"
    payload = json.loads(first)
    assert payload["now"] == "2026-06-30"
    assert payload["secrets"]


def test_cli_validate_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert sr.main(["validate"]) == 0
    assert "schema valid" in capsys.readouterr().out


def test_cli_plan_unknown_secret_exits() -> None:
    with pytest.raises(SystemExit):
        sr.main(["plan", "NOPE_NOT_A_SECRET"])
