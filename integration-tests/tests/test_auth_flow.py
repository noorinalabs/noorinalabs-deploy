"""Scenario 1: OAuth (shimmed) → JWT issuance → isnad-graph API access.
Scenario 2: Token refresh across the service boundary.
Scenario 3: Real OAuth callback against fake_oauth container (noorinalabs-main#135).
"""

from __future__ import annotations

import secrets

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


@pytest.mark.asyncio
async def test_oauth_callback_to_token_issue(
    user_service: httpx.AsyncClient,
    isnad_graph: httpx.AsyncClient,
    user_pg,
) -> None:
    """noorinalabs-main#135 — real OAuth callback against fake_oauth.

    Exercises the full Google flow:
      1. /auth/oauth/google/login — get a state + code_verifier
      2. /auth/oauth/google/callback — user-service hits fake_oauth's
         /token + /oauth2/v3/userinfo via OAUTH_PROVIDER_BASE_URL_OVERRIDE,
         creates the user, returns a one-time authorization_code
      3. /auth/token — exchange that authorization_code for a JWT pair
      4. isnad-graph /api/v1/narrators — verify the JWT is accepted
    """
    # 1. Bootstrap PKCE by hitting the real login endpoint. Returns a
    # state + code_verifier that user-service will happily accept back
    # on the callback — the current callback impl does not re-check
    # state/verifier against Redis, it just forwards the verifier to
    # the token endpoint.
    r = await user_service.get("/auth/oauth/google/login")
    assert r.status_code == 200, r.text
    login = r.json()
    assert login["state"]
    assert login["code_verifier"]

    # 2. POST the callback. fake_oauth accepts any `code`; the resulting
    # user's sub/email is derived from the access_token suffix, so the
    # email is unique per flow and collisions across runs are avoided.
    fake_code = f"FAKE_CODE_{secrets.token_urlsafe(12)}"
    r = await user_service.post(
        "/auth/oauth/google/callback",
        json={
            "code": fake_code,
            "state": login["state"],
            "code_verifier": login["code_verifier"],
        },
    )
    assert r.status_code == 200, (
        f"callback failed — fake_oauth reachable? override set? resp={r.text}"
    )
    callback = r.json()
    assert callback["provider"] == "google"
    assert callback["email"].startswith("fake-") and "@example.com" in callback["email"]
    authorization_code = callback["authorization_code"]
    assert authorization_code

    # 3. Exchange the authorization code for JWTs.
    tokens = await issue_token_for(user_service, authorization_code)
    assert tokens["access_token"]
    assert tokens["refresh_token"]

    # 4. Access a protected isnad-graph endpoint — same bar as the
    # shim-path test: 401 means JWT rejected, anything else is fine.
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    r = await isnad_graph.get(
        "/api/v1/narrators", headers=headers, params={"limit": 1}
    )
    assert r.status_code != 401, (
        f"isnad-graph rejected JWT minted via fake_oauth flow: {r.text}"
    )

    # Cleanup — find_or_create_oauth_user persisted a row; remove it to
    # keep the test DB tidy. Match by email (unique) to get the user_id.
    uid = await user_pg.fetchval(
        "SELECT id FROM users WHERE email = $1", callback["email"]
    )
    if uid is not None:
        await user_pg.execute("DELETE FROM sessions WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM user_roles WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM subscriptions WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM oauth_accounts WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM users WHERE id = $1", uid)
