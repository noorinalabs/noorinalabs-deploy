"""Scenario 5: Subscription enforcement on premium features."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import (
    REMOTE_SHAPING_UNSUPPORTED_REASON,
    RUN_MODE,
    AuthSession,
    issue_token_for,
)


@pytest.mark.asyncio
async def test_subscription_status_reflected_in_jwt(
    seeded_user_factory, user_service: httpx.AsyncClient
) -> None:
    # Hermetic-only: needs a user shaped with an ACTIVE paid subscription,
    # which the fixed free-tier stg test-user cannot provide.
    if RUN_MODE != "hermetic":
        pytest.skip(REMOTE_SHAPING_UNSUPPORTED_REASON)
    _, auth_code = await seeded_user_factory(
        email="premium-user@example.com", subscription_status="active"
    )
    tokens = await issue_token_for(user_service, auth_code)

    r = await user_service.get(
        "/auth/token/validate",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["subscription_status"] == "active"


@pytest.mark.asyncio
async def test_free_tier_not_promoted_to_premium(
    auth_session: AuthSession, user_service: httpx.AsyncClient
) -> None:
    # The default test-user is free-tier in both modes, so the "free stays
    # free" assertion runs remotely too.
    r = await user_service.get(
        "/auth/token/validate",
        headers={"Authorization": f"Bearer {auth_session.access_token}"},
    )
    body = r.json()
    assert body["subscription_status"] == "free"


@pytest.mark.asyncio
async def test_trial_start_flow(seeded_user_factory, user_service: httpx.AsyncClient) -> None:
    # Hermetic-only: starting a trial mutates DURABLE subscription state on
    # the user. Running it against the shared, long-lived stg test-user would
    # consume that user's one-time trial and pollute subsequent runs (the
    # 409-already-used branch would mask real regressions). Keep it on a
    # throwaway hermetic user. seeded_user_factory also skips in remote, but
    # the explicit guard documents WHY this one is intentionally not promoted.
    if RUN_MODE != "hermetic":
        pytest.skip(REMOTE_SHAPING_UNSUPPORTED_REASON)
    _, auth_code = await seeded_user_factory(email="trial-user@example.com")
    tokens = await issue_token_for(user_service, auth_code)

    # TrialStartRequest is an empty Pydantic model but still required as body.
    r = await user_service.post(
        "/api/v1/subscriptions/trial",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={},
    )
    # 201 on success, 409 if trial already used — both are valid end-states.
    assert r.status_code in (201, 409)
