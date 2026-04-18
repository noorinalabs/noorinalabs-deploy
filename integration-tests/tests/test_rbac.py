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

    # Retry on 503 — the first admin call from isnad-graph back to user-service
    # for a JWKS fetch can race with user-service readiness; the JWKS is then
    # cached for subsequent calls. Two attempts is enough for the cache to warm.
    for attempt in range(3):
        r = await isnad_graph.get(
            "/api/v1/admin/health/live",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        if r.status_code != 503:
            break
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

    for attempt in range(3):
        r = await isnad_graph.get(
            "/api/v1/admin/health/live",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        if r.status_code != 503:
            break
    # Load-bearing: admin JWT was NOT rejected for auth reasons (401/403).
    assert r.status_code not in (401, 403), f"admin JWT rejected: {r.text}"
