"""Shared fixtures for the cross-repo integration suite.

These fixtures talk to the *running* services over the docker Compose network.
Each fixture that creates state also cleans it up.

Why the auth-code shim? The OAuth provider exchange calls real Google/GitHub
URLs that are hardcoded in `src/app/services/oauth.py`. Rather than adding a
provider URL override (follow-up), we short-circuit to the *post-callback* point
in the flow: seed a user in user-postgres and an auth code in user-redis, then
exercise `/auth/token` through the rest of the stack end-to-end.
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


@pytest.fixture(autouse=True, scope="session")
def _warm_cross_service_jwks() -> None:
    """Warm isnad-graph's JWKS cache by issuing a throwaway JWT and calling
    a protected endpoint repeatedly at the start of the session. Without this,
    the first couple of cross-service JWT validations can 503 because
    isnad-graph's fetch_jwks() has no httpx-level retry and user-service's
    first post-startup /.well-known/jwks.json response occasionally arrives
    out-of-window.
    """
    import asyncio
    import time

    async def _warm() -> None:
        deadline = time.monotonic() + 30.0
        # Create a throwaway user + auth code so we can issue a real JWT.
        pg = await asyncpg.connect(USER_POSTGRES_DSN)
        redis_cli = aioredis.from_url(USER_REDIS_URL, decode_responses=True)
        user_id = uuid.uuid4()
        email = f"warmup-{secrets.token_hex(4)}@example.com"
        try:
            await pg.execute(
                "INSERT INTO users (id, email, email_verified, display_name, is_active, "
                "created_at, updated_at) VALUES ($1, $2, TRUE, 'warmup', TRUE, NOW(), NOW())",
                user_id, email,
            )
            code = secrets.token_urlsafe(48)
            await redis_cli.setex(
                f"auth_code:{code}", 300,
                json.dumps({"user_id": str(user_id), "email": email,
                            "roles": [], "subscription_status": "free"}),
            )
            async with httpx.AsyncClient(base_url=USER_SERVICE_URL, timeout=10) as us:
                t = await us.post("/auth/token", json={"authorization_code": code})
                if t.status_code != 200:
                    return
                tokens = t.json()
            async with httpx.AsyncClient(base_url=ISNAD_GRAPH_URL, timeout=10) as ig:
                headers = {"Authorization": f"Bearer {tokens['access_token']}"}
                while time.monotonic() < deadline:
                    r = await ig.get("/api/v1/narrators", headers=headers,
                                     params={"limit": 1})
                    if r.status_code not in (502, 503, 504):
                        return
                    await asyncio.sleep(1.0)
        finally:
            await pg.execute("DELETE FROM users WHERE id = $1", user_id)
            await pg.close()
            await redis_cli.aclose()

    asyncio.run(_warm())

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
