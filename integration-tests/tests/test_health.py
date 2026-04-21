"""All services expose `/health` and return 200."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_user_service_health(user_service: httpx.AsyncClient) -> None:
    r = await user_service.get("/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_isnad_graph_health(isnad_graph: httpx.AsyncClient) -> None:
    r = await isnad_graph.get("/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_user_service_jwks(user_service: httpx.AsyncClient) -> None:
    """JWKS must be publicly reachable — isnad-graph uses it to validate JWTs."""
    r = await user_service.get("/.well-known/jwks.json")
    assert r.status_code == 200
    body = r.json()
    assert "keys" in body
    assert len(body["keys"]) >= 1
