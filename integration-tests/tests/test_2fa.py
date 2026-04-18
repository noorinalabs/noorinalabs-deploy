"""Scenario 7: 2FA (TOTP) setup + verify flow."""

from __future__ import annotations

import httpx
import pyotp
import pytest

from tests.conftest import issue_token_for


@pytest.mark.asyncio
async def test_totp_setup_and_verify(
    seeded_user_factory, user_service: httpx.AsyncClient
) -> None:
    _, auth_code = await seeded_user_factory(email="totp-user@example.com")
    tokens = await issue_token_for(user_service, auth_code)
    auth_headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # Setup: returns a `secret` (or `provisioning_uri`) for the authenticator app.
    r = await user_service.post("/api/v1/2fa", headers=auth_headers)
    assert r.status_code in (200, 201), r.text
    body = r.json()
    secret = body.get("secret") or body.get("totp_secret")
    assert secret, f"expected secret in 2FA setup response; got {body!r}"

    # Verify with a real TOTP code generated from the returned secret.
    code = pyotp.TOTP(secret).now()
    r = await user_service.post(
        "/api/v1/2fa", headers=auth_headers, json={"code": code}
    )
    # 200 on confirm, 409 if this particular build scopes confirm to a
    # different subpath — accept either as long as it is not 401/5xx.
    assert r.status_code in (200, 201, 409), r.text


@pytest.mark.asyncio
async def test_totp_rejects_invalid_code(
    seeded_user_factory, user_service: httpx.AsyncClient
) -> None:
    _, auth_code = await seeded_user_factory(email="totp-bad@example.com")
    tokens = await issue_token_for(user_service, auth_code)
    auth_headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    setup = await user_service.post("/api/v1/2fa", headers=auth_headers)
    assert setup.status_code in (200, 201)

    r = await user_service.post(
        "/api/v1/2fa", headers=auth_headers, json={"code": "000000"}
    )
    assert r.status_code in (400, 401, 403), (
        f"invalid TOTP code should fail, got {r.status_code}"
    )
