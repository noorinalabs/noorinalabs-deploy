"""Scenario 1: OAuth (shimmed) → JWT issuance → isnad-graph API access.
Scenario 2: Token refresh across the service boundary.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import issue_token_for


@pytest.mark.asyncio
async def test_auth_code_grants_jwt_that_isnad_graph_accepts(
    seeded_user_factory,
    user_service: httpx.AsyncClient,
    isnad_graph: httpx.AsyncClient,
) -> None:
    _, auth_code = await seeded_user_factory(email="alice@example.com")

    tokens = await issue_token_for(user_service, auth_code)
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert int(tokens["expires_in"]) > 0

    # Cross-service: access a protected isnad-graph endpoint with that JWT.
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    r = await isnad_graph.get("/api/v1/narrators", headers=headers, params={"limit": 1})
    # Accept 200 (data present) or 404/empty (no data seeded). Anything in the
    # 2xx / 4xx data-shape range is fine; 401 means JWT validation failed.
    assert r.status_code != 401, f"isnad-graph rejected user-service JWT: {r.text}"


@pytest.mark.asyncio
async def test_refresh_token_rotation(
    seeded_user_factory, user_service: httpx.AsyncClient
) -> None:
    _, auth_code = await seeded_user_factory(email="bob@example.com")
    t1 = await issue_token_for(user_service, auth_code)

    r = await user_service.post(
        "/auth/token/refresh", json={"refresh_token": t1["refresh_token"]}
    )
    assert r.status_code == 200, r.text
    t2 = r.json()

    # Two load-bearing properties of rotation:
    #   1. A new access token is issued (else `/token/refresh` is useless)
    #   2. The old refresh token is no longer accepted (else rotation is fake)
    # We don't directly compare the literal refresh token strings — they are
    # opaque tokens whose equality is not meaningfully observable from the
    # outside; what matters is revocation of the old one.
    assert t2["access_token"], "new access token must be present"
    assert t2["refresh_token"], "new refresh token must be present"

    # Old refresh must now be invalid (rotation revokes it).
    r2 = await user_service.post(
        "/auth/token/refresh", json={"refresh_token": t1["refresh_token"]}
    )
    assert r2.status_code == 401, (
        f"old refresh token still valid after rotation: {r2.status_code} {r2.text}"
    )


@pytest.mark.asyncio
async def test_token_validation_endpoint(
    seeded_user_factory, user_service: httpx.AsyncClient
) -> None:
    seeded, auth_code = await seeded_user_factory(
        email="carol@example.com", roles=["user"]
    )
    tokens = await issue_token_for(user_service, auth_code)

    r = await user_service.get(
        "/auth/token/validate",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["email"] == seeded.email
