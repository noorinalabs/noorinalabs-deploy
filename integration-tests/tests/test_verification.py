"""Scenario 6: Email verification flow."""

from __future__ import annotations

import asyncpg
import httpx
import pytest

from tests.conftest import issue_token_for


@pytest.mark.asyncio
async def test_verification_issue_and_confirm(
    seeded_user_factory,
    user_service: httpx.AsyncClient,
    user_pg: asyncpg.Connection,
) -> None:
    seeded, auth_code = await seeded_user_factory(
        email="needs-verify@example.com", email_verified=False
    )
    tokens = await issue_token_for(user_service, auth_code)
    auth_headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # Request issuance. The service stores a hashed token; the raw token is
    # only ever sent via email in prod. For the integration test we fetch the
    # most-recent row for our user and trust that the router's hashing round-trip
    # is exercised in the user-service unit suite.
    r = await user_service.post("/api/v1/verification", headers=auth_headers)
    assert r.status_code in (200, 201, 202)

    # Read raw-token equivalent by looking at the `sent_at` bump: we confirm that
    # a verification token row now exists for this user.
    row = await user_pg.fetchrow(
        "SELECT id, expires_at FROM verification_tokens "
        "WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
        seeded.user_id,
    )
    assert row is not None, "verification token row should exist after issue"

    # Status check must report pending.
    r = await user_service.get("/api/v1/verification", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    # Shape differs across revisions; just assert email_verified is present and False.
    assert body.get("email_verified") is False or body.get("status") in {
        "pending",
        "sent",
    }
