"""Scenario 1: OAuth (shimmed) → JWT issuance → isnad-graph API access.
Scenario 2: Token refresh across the service boundary.
Scenario 3: Real OAuth callback against fake_oauth container (noorinalabs-main#135).
"""

from __future__ import annotations

import os
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tests.conftest import issue_token_for

USER_SERVICE_URL = os.environ["USER_SERVICE_URL"]


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
    isnad_graph: httpx.AsyncClient,
    user_pg,
) -> None:
    """noorinalabs-main#135 — real OAuth callback against fake_oauth.

    The callback is GET + RedirectResponse (user-service #66/#67). The flow:
      1. GET /auth/oauth/google/login — stashes state + PKCE verifier in Redis
         and returns the authorization_url (already rewritten to fake_oauth
         via OAUTH_PROVIDER_BASE_URL_OVERRIDE).
      2. GET /auth/oauth/google/callback?code=...&state=... — user-service
         validates state against Redis, exchanges the code with fake_oauth's
         /token, fetches userinfo from fake_oauth's /oauth2/v3/userinfo,
         upserts the user, mints tokens, and 302s to
         AUTH_OAUTH_POST_LOGIN_URL/{provider}?token=...&is_new_user=0|1.
      3. Parse the access token out of the redirect Location and verify
         isnad-graph accepts it.

    We do not need to visit fake_oauth's /o/oauth2/v2/auth — fake_oauth's
    /token accepts any code, and state validation only checks Redis. This
    keeps the test focused on the user-service ↔ fake_oauth round-trip.
    """
    async with httpx.AsyncClient(
        base_url=USER_SERVICE_URL, timeout=10, follow_redirects=False
    ) as user_service:
        # 1. Bootstrap PKCE by hitting the login endpoint. This side-effect
        # (stashing state → {provider, code_verifier} in Redis) is what makes
        # the direct callback hit below pass state validation.
        r = await user_service.get("/auth/oauth/google/login")
        assert r.status_code == 200, r.text
        login = r.json()
        state = login["state"]
        assert state
        # Sanity: login rewrote the authorize URL onto fake_oauth. If this
        # regresses, callback will also fail with a surprising network error
        # rather than a clean 302 — catch it here.
        parsed_auth_url = urlparse(login["authorization_url"])
        assert parsed_auth_url.netloc == "fake_oauth:8080", (
            "login did not rewrite authorization_url onto fake_oauth — "
            f"OAUTH_PROVIDER_BASE_URL_OVERRIDE wiring regressed: {login['authorization_url']}"
        )

        # 2. GET the callback with a fabricated code + the real state. The
        # callback is a browser-facing endpoint that returns a RedirectResponse
        # (302) — we must disable redirect-following so httpx gives us the
        # Location header with the minted access token on it.
        fake_code = f"FAKE_CODE_{secrets.token_urlsafe(12)}"
        r = await user_service.get(
            "/auth/oauth/google/callback",
            params={"code": fake_code, "state": state},
        )
        assert r.status_code == 302, (
            "callback did not redirect — fake_oauth reachable? override set? "
            f"status={r.status_code} body={r.text[:500]}"
        )
        location = r.headers.get("location", "")
        assert location, "callback 302 missing Location header"
        parsed = urlparse(location)
        # AUTH_OAUTH_POST_LOGIN_URL default is /auth/callback; the handler
        # appends /{provider}. Error redirects take the same base but carry
        # ?error=... — fail loudly if we got an error redirect.
        assert parsed.path == "/auth/callback/google", (
            f"unexpected redirect path {parsed.path!r} — full Location={location}"
        )
        qs = parse_qs(parsed.query)
        assert "error" not in qs, (
            f"callback redirected with error={qs.get('error')} — check user-service "
            f"logs for the provider/exchange failure. Location={location}"
        )
        assert qs.get("token"), f"no token in callback redirect: {location}"
        access_token = qs["token"][0]
        assert qs.get("is_new_user") == ["1"], (
            f"expected a newly-created user on first callback; got is_new_user="
            f"{qs.get('is_new_user')}"
        )

        # 3. Cross-service: access a protected isnad-graph endpoint with the
        # token minted by the real OAuth callback. 401 means JWT validation
        # failed (issuer/audience/JWKS mismatch); anything else is fine here.
        headers = {"Authorization": f"Bearer {access_token}"}
        ig = await isnad_graph.get(
            "/api/v1/narrators", headers=headers, params={"limit": 1}
        )
        assert ig.status_code != 401, (
            f"isnad-graph rejected JWT minted via fake_oauth flow: {ig.text}"
        )

    # 4. Cleanup — find_or_create_oauth_user persisted a row. Its email is
    # derived from the fake access_token suffix, so we can't predict it; look
    # it up by OAuth provider + the account id we issued (sub=fake-user-<suffix>).
    # Simplest path: delete any fake-*@example.com users created during this
    # test window. These are only produced by this test path.
    rows = await user_pg.fetch(
        "SELECT id FROM users WHERE email LIKE 'fake-%@example.com'"
    )
    for row in rows:
        uid = row["id"]
        await user_pg.execute("DELETE FROM sessions WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM user_roles WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM subscriptions WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM oauth_accounts WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM users WHERE id = $1", uid)
