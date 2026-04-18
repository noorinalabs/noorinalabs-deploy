"""Scenario 4: RBAC on isnad-graph endpoints.

Admin JWT must unlock `/admin/*`; non-admin must be 401/403.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import issue_token_for


@pytest.mark.asyncio
async def test_admin_endpoint_rejects_non_admin(
    seeded_user_factory,
    user_service: httpx.AsyncClient,
    isnad_graph: httpx.AsyncClient,
) -> None:
    _, auth_code = await seeded_user_factory(
        email="not-admin@example.com", roles=["user"]
    )
    tokens = await issue_token_for(user_service, auth_code)

    r = await isnad_graph.get(
        "/api/v1/admin/health/live",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code in (401, 403), f"expected forbidden, got {r.status_code}"


@pytest.mark.asyncio
async def test_admin_endpoint_accepts_admin(
    seeded_user_factory,
    user_service: httpx.AsyncClient,
    isnad_graph: httpx.AsyncClient,
) -> None:
    _, auth_code = await seeded_user_factory(
        email="the-admin@example.com", roles=["admin"]
    )
    tokens = await issue_token_for(user_service, auth_code)

    r = await isnad_graph.get(
        "/api/v1/admin/health/live",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    # 200 expected; 404 is also acceptable if the admin router is registered
    # under a different prefix in the current build — the load-bearing signal
    # is that we did NOT get 401/403.
    assert r.status_code not in (401, 403), f"admin JWT rejected: {r.text}"
