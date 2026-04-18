"""Scenario 4: RBAC on isnad-graph endpoints.

Admin JWT must unlock `/admin/*`; non-admin must be 401/403.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.conftest import issue_token_for


async def _warm_jwks(isnad_graph: httpx.AsyncClient, access_token: str) -> None:
    """Prod isnad-graph once to warm its JWKS cache. isnad-graph's
    fetch_jwks() call has no retry on the httpx layer, so a cold-start
    connect failure surfaces as 503 from require_auth. A brief warmup
    with exponential backoff paves over that.
    """
    for delay_s in (0.1, 0.5, 1.5):
        r = await isnad_graph.get(
            "/api/v1/admin/health/live",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if r.status_code != 503:
            return
        await asyncio.sleep(delay_s)


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

    await _warm_jwks(isnad_graph, tokens["access_token"])

    r = await isnad_graph.get(
        "/api/v1/admin/health/live",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code in (401, 403), f"expected forbidden, got {r.status_code}: {r.text}"


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

    await _warm_jwks(isnad_graph, tokens["access_token"])

    r = await isnad_graph.get(
        "/api/v1/admin/health/live",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    # Load-bearing: admin JWT was NOT rejected for auth reasons (401/403).
    assert r.status_code not in (401, 403), f"admin JWT rejected: {r.text}"
