"""Shared fixtures for the cross-repo integration suite.

These fixtures talk to the *running* services over the docker Compose network.
Each fixture that creates state also cleans it up.

Two auth-flow paths are exercised against the same stack:
  * Real-callback path — `test_oauth_callback_to_token_issue` exercises
    `/auth/oauth/google/callback` end-to-end against the `fake_oauth`
    container, reaching user-service via OAUTH_PROVIDER_BASE_URL_OVERRIDE
    (user-service#77/#78, deploy #135).
  * Shim path — the factory below seeds a user in user-postgres and an auth
    code in user-redis, short-circuiting to `/auth/token`. Predates the
    override; retained for regression coverage and for other providers that
    do not yet have fake_oauth support (GitHub / Apple / Facebook).
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import asyncpg
import httpx
import pytest
import redis.asyncio as aioredis

USER_SERVICE_URL = os.environ["USER_SERVICE_URL"]
ISNAD_GRAPH_URL = os.environ["ISNAD_GRAPH_URL"]



USER_POSTGRES_DSN = (
    f"postgresql://{os.environ['USER_POSTGRES_USER']}"
    f":{os.environ['USER_POSTGRES_PASSWORD']}"
    f"@{os.environ['USER_POSTGRES_HOST']}:{os.environ['USER_POSTGRES_PORT']}"
    f"/{os.environ['USER_POSTGRES_DB']}"
)
USER_REDIS_URL = os.environ["USER_REDIS_URL"]


@dataclass
class SeededUser:
    user_id: uuid.UUID
    email: str
    roles: list[str]
    subscription_status: str


@pytest.fixture
async def user_pg() -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(USER_POSTGRES_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def user_redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(USER_REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def user_service() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=USER_SERVICE_URL, timeout=10) as c:
        yield c


@pytest.fixture
async def isnad_graph() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=ISNAD_GRAPH_URL, timeout=10) as c:
        yield c


async def _seed_user(
    pg: asyncpg.Connection,
    *,
    email: str,
    roles: list[str],
    subscription_status: str = "free",
    email_verified: bool = True,
) -> uuid.UUID:
    user_id = uuid.uuid4()
    await pg.execute(
        """
        INSERT INTO users (id, email, email_verified, display_name, is_active,
                           created_at, updated_at)
        VALUES ($1, $2, $3, $4, TRUE, NOW(), NOW())
        """,
        user_id,
        email,
        email_verified,
        f"Test {email.split('@')[0]}",
    )
    for role in roles:
        role_id = await pg.fetchval("SELECT id FROM roles WHERE name = $1", role)
        if role_id is None:
            role_id = uuid.uuid4()
            await pg.execute(
                "INSERT INTO roles (id, name, created_at) VALUES ($1, $2, NOW())",
                role_id,
                role,
            )
        # user_roles columns: user_id, role_id, granted_at (not assigned_at).
        await pg.execute(
            "INSERT INTO user_roles (user_id, role_id, granted_at) VALUES ($1, $2, NOW())",
            user_id,
            role_id,
        )
    if subscription_status != "free":
        # subscriptions columns: id, user_id, plan (enum), status (enum),
        # starts_at (NOT NULL). Plans: free|trial|researcher|institutional.
        # Statuses: active|expired|cancelled|suspended.
        await pg.execute(
            """
            INSERT INTO subscriptions
                (id, user_id, plan, status, starts_at, created_at, updated_at)
            VALUES ($1, $2, 'researcher', $3, NOW(), NOW(), NOW())
            """,
            uuid.uuid4(),
            user_id,
            subscription_status,
        )
    return user_id


@pytest.fixture
async def seeded_user_factory(
    user_pg: asyncpg.Connection,
    user_redis: aioredis.Redis,
):
    """Factory that creates a test user + (optional) one-time auth code.

    Returns a callable `make(email=..., roles=..., subscription_status=...)`.
    Cleans up everything it created on teardown.
    """
    created_user_ids: list[uuid.UUID] = []
    created_auth_codes: list[str] = []

    async def make(
        *,
        email: str | None = None,
        roles: list[str] | None = None,
        subscription_status: str = "free",
        email_verified: bool = True,
    ) -> tuple[SeededUser, str]:
        email = email or f"test-{secrets.token_hex(4)}@example.com"
        roles = roles or []
        user_id = await _seed_user(
            user_pg,
            email=email,
            roles=roles,
            subscription_status=subscription_status,
            email_verified=email_verified,
        )
        created_user_ids.append(user_id)

        auth_code = secrets.token_urlsafe(48)
        payload = json.dumps(
            {
                "user_id": str(user_id),
                "email": email,
                "roles": roles,
                "subscription_status": subscription_status,
            }
        )
        await user_redis.setex(f"auth_code:{auth_code}", 60, payload)
        created_auth_codes.append(auth_code)

        return (
            SeededUser(
                user_id=user_id,
                email=email,
                roles=roles,
                subscription_status=subscription_status,
            ),
            auth_code,
        )

    yield make

    # Cleanup (order matters — FK from user_roles / subscriptions / sessions)
    for code in created_auth_codes:
        await user_redis.delete(f"auth_code:{code}")
    for uid in created_user_ids:
        await user_pg.execute("DELETE FROM sessions WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM user_roles WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM subscriptions WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM oauth_accounts WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM verification_tokens WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM totp_secrets WHERE user_id = $1", uid)
        await user_pg.execute("DELETE FROM users WHERE id = $1", uid)


async def issue_token_for(
    user_service: httpx.AsyncClient, authorization_code: str
) -> dict[str, str | int]:
    r = await user_service.post(
        "/auth/token", json={"authorization_code": authorization_code}
    )
    assert r.status_code == 200, f"token issuance failed: {r.status_code} {r.text}"
    return r.json()
