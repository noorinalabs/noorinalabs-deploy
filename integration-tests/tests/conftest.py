"""Shared fixtures for the cross-repo integration suite.

These fixtures talk to the *running* services. Two run modes are supported
(see `run-tests.sh` for the orchestration):

  * hermetic (default) — services run on the local docker-compose network
    built from sibling checkouts. DB+Redis are reachable from the runner
    container; `seeded_user_factory` writes directly into them.

  * remote — services run on stg; HTTP fixtures point at env-supplied URLs
    (USER_SERVICE_BASE_URL, ISNAD_BASE_URL). Direct DB/Redis access is NOT
    available from outside the VPS, so `user_pg`, `user_redis`, and
    `seeded_user_factory` auto-skip in remote mode. Tests that depend on
    them are therefore hermetic-only today; per-test refactors to use
    pre-provisioned stg test-user creds delivered via secrets are
    downstream follow-up work (see deploy#178 body).

Two auth-flow paths are exercised against the same stack (hermetic only):
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

# RUN_MODE is set by run-tests.sh; default to hermetic so direct invocation
# of pytest (e.g. `pytest tests/test_health.py`) inside the runner container
# still works the same way it always has.
RUN_MODE = os.environ.get("RUN_MODE", "hermetic")

# Defense-in-depth prod-environment guard. The shell entrypoint
# `integration-tests/run-tests.sh` already exits 3 on `RUN_MODE=remote
# ENVIRONMENT={prod,production}` before pytest starts, but a direct
# `pytest` invocation inside the runner image (interactive debug, future
# workflow that skips run-tests.sh, `python -m pytest`, etc.) would
# otherwise reach the auth-mutating fixtures and create real users or
# trip rate-limiters against prod. Re-check here at module load so the
# guard fires regardless of how pytest is invoked. See deploy#203 and
# the matching shell block in run-tests.sh — keep the two coupled.
_ENVIRONMENT = os.environ.get("ENVIRONMENT", "stg")
if RUN_MODE == "remote" and _ENVIRONMENT in ("prod", "production"):
    raise RuntimeError(
        f"Refusing to run integration suite in remote mode against "
        f"ENVIRONMENT={_ENVIRONMENT!r}. The suite mutates state via "
        f"/auth endpoints; running against prod could create real "
        f"users or trip rate-limiters that page oncall. Set "
        f"ENVIRONMENT=stg or unset (defaults to 'stg'). This is "
        f"enforced both by integration-tests/run-tests.sh AND here "
        f"at module load."
    )

HERMETIC_ONLY_REASON = (
    "hermetic-only fixture: direct DB/Redis access is not available in "
    "remote mode (stg DBs are not reachable from outside the VPS). Run "
    "this test with RUN_MODE=hermetic, or refactor it to drive the "
    "user-service via stg test-user credentials (deploy#178 follow-up)."
)

# --- Remote-mode test-user credentials (deploy#204) ---
#
# In hermetic mode, `seeded_user_factory` shapes an arbitrary user (roles,
# subscription, email_verified) directly in the local DB+Redis. Remote mode
# can't do that — stg DB/Redis are not reachable from outside the VPS, and
# user-service exposes NO password-login / test-seed / dev-bypass endpoint
# (audited deploy#204: the only token-minting paths are the Redis-seeded
# `/auth/token` one-time code and the real OAuth callback, neither of which
# a CI runner can drive against stg).
#
# The CI-safe, HTTP-only path we DO have is `/auth/token/refresh`: a
# long-lived refresh token for a pre-provisioned, stable stg test-user can
# be exchanged for a fresh access+refresh pair without any DB access. The
# refresh token is provisioned out-of-band (one real OAuth login by the
# test-user, captured into a GH Actions secret) and rotated per the runbook
# in README.md. See `remote_test_user` below.
#
# RUNTIME-GATED: the secret only exists in the integration-tests workflow's
# stg environment. When it is absent (local dev, PR-CI hermetic runs,
# secret not yet provisioned) the remote-capable tests skip cleanly rather
# than fail — exactly as the DB-direct fixtures skip in remote mode today.
STG_TEST_USER_REFRESH_TOKEN = os.environ.get("STG_TEST_USER_REFRESH_TOKEN", "")
STG_TEST_USER_EMAIL = os.environ.get("STG_TEST_USER_EMAIL", "")

REMOTE_CREDS_MISSING_REASON = (
    "remote-mode test-user credentials not provisioned: "
    "STG_TEST_USER_REFRESH_TOKEN is unset. This is expected outside the "
    "integration-tests workflow's stg environment (local dev, hermetic "
    "PR-CI). Provision the stg test-user refresh token per the runbook in "
    "integration-tests/README.md to exercise this test against stg."
)

# Roles/subscription/email-verified shaping that a single fixed stg
# test-user cannot satisfy stays hermetic-only. The test-user is created as
# a plain free-tier, non-admin, email-verified account; tests asserting
# admin RBAC, paid-subscription claims, fresh-2FA enrollment, or
# unverified-email flows need DB-direct shaping and remain hermetic-only.
REMOTE_SHAPING_UNSUPPORTED_REASON = (
    "hermetic-only: this test requires DB-direct user shaping (specific "
    "roles / subscription tier / email_verified / fresh-2FA state) that "
    "the single fixed stg test-user cannot provide. The stg test-user is a "
    "plain free-tier non-admin account. Run with RUN_MODE=hermetic."
)


# In remote mode, the *_BASE_URL env vars are the canonical stg targets;
# the legacy USER_SERVICE_URL / ISNAD_GRAPH_URL names are the hermetic
# compose-internal hostnames. run-tests.sh exports both shapes in remote
# mode so this lookup works either way.
USER_SERVICE_URL = os.environ.get("USER_SERVICE_URL", os.environ.get("USER_SERVICE_BASE_URL", ""))
ISNAD_GRAPH_URL = os.environ.get("ISNAD_GRAPH_URL", os.environ.get("ISNAD_BASE_URL", ""))
if not USER_SERVICE_URL or not ISNAD_GRAPH_URL:
    raise RuntimeError(
        "USER_SERVICE_URL/USER_SERVICE_BASE_URL and "
        "ISNAD_GRAPH_URL/ISNAD_BASE_URL are required. "
        "run-tests.sh sets these; if you're running pytest directly, "
        "export them yourself."
    )

# DB/Redis DSN construction is hermetic-only — these env vars are not
# defined in remote mode, and a plain `os.environ[...]` lookup would crash
# at import time before any pytest skip could fire. Guard the lookups.
if RUN_MODE == "hermetic":
    USER_POSTGRES_DSN = (
        f"postgresql://{os.environ['USER_POSTGRES_USER']}"
        f":{os.environ['USER_POSTGRES_PASSWORD']}"
        f"@{os.environ['USER_POSTGRES_HOST']}:{os.environ['USER_POSTGRES_PORT']}"
        f"/{os.environ['USER_POSTGRES_DB']}"
    )
    USER_REDIS_URL = os.environ["USER_REDIS_URL"]
else:
    USER_POSTGRES_DSN = ""
    USER_REDIS_URL = ""


@dataclass
class SeededUser:
    user_id: uuid.UUID
    email: str
    roles: list[str]
    subscription_status: str


@pytest.fixture
async def user_pg() -> AsyncIterator[asyncpg.Connection]:
    if RUN_MODE != "hermetic":
        pytest.skip(HERMETIC_ONLY_REASON)
    conn = await asyncpg.connect(USER_POSTGRES_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def user_redis() -> AsyncIterator[aioredis.Redis]:
    if RUN_MODE != "hermetic":
        pytest.skip(HERMETIC_ONLY_REASON)
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
    r = await user_service.post("/auth/token", json={"authorization_code": authorization_code})
    assert r.status_code == 200, f"token issuance failed: {r.status_code} {r.text}"
    return r.json()


@dataclass
class AuthSession:
    """An authenticated token pair, mode-agnostic.

    Lets a test obtain a usable access+refresh token without caring whether
    the underlying user was seeded DB-direct (hermetic) or is the
    pre-provisioned stg test-user reached via `/auth/token/refresh` (remote).
    `email` is the authenticated user's email when known (hermetic seeds it,
    remote reads it from STG_TEST_USER_EMAIL); it may be empty in remote mode
    if the secret was not supplied, in which case email-equality assertions
    should be guarded.
    """

    access_token: str
    refresh_token: str
    email: str


@pytest.fixture
async def remote_test_user(user_service: httpx.AsyncClient) -> AuthSession:
    """Mint a fresh token pair for the pre-provisioned stg test-user (remote mode).

    Exchanges the long-lived `STG_TEST_USER_REFRESH_TOKEN` via the real
    `/auth/token/refresh` endpoint — an HTTP-only path that needs no DB or
    Redis access, so it works from outside the VPS. The exchange ROTATES the
    refresh token (user-service revokes the old one on every refresh), so we
    return the freshly-issued pair; the long-lived secret is only the
    bootstrap seed for the first hop in this test process.

    Skips when the credential is absent (see REMOTE_CREDS_MISSING_REASON).
    In hermetic mode this fixture is a no-op sentinel: `auth_session`
    declares it as a parameter but only consumes it in the remote branch, so
    it must not skip (a skip would propagate and abort hermetic tests).
    """
    if RUN_MODE == "hermetic":
        # Never consumed in hermetic mode; return an empty sentinel so the
        # fixture resolves without contacting any service or skipping.
        return AuthSession(access_token="", refresh_token="", email="")
    if not STG_TEST_USER_REFRESH_TOKEN:
        pytest.skip(REMOTE_CREDS_MISSING_REASON)

    r = await user_service.post(
        "/auth/token/refresh",
        json={"refresh_token": STG_TEST_USER_REFRESH_TOKEN},
    )
    assert r.status_code == 200, (
        "stg test-user refresh-token exchange failed: "
        f"{r.status_code} {r.text}. The STG_TEST_USER_REFRESH_TOKEN secret "
        "may have expired or been rotated out — re-provision per the runbook "
        "in integration-tests/README.md."
    )
    body = r.json()
    return AuthSession(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        email=STG_TEST_USER_EMAIL,
    )


@pytest.fixture
async def _hermetic_seed() -> AsyncIterator:
    """Remote-SAFE hermetic-only seeding factory for `auth_session`.

    This is a parallel of `seeded_user_factory` that does NOT depend on the
    `user_pg` / `user_redis` fixtures. Those fixtures `pytest.skip()` in
    remote mode, and a skip propagates to every dependent — which would make
    `auth_session` (and thus every refactored test) skip in remote mode even
    though the remote branch never touches the DB. To keep `auth_session`
    usable via plain async-fixture parameter nesting (the only nesting
    pytest-asyncio 0.24 supports cleanly — `getfixturevalue` on an async
    fixture raises inside a running loop), this fixture opens its own
    connections internally and ONLY in hermetic mode. In remote mode it
    yields `None` and is never invoked, so no DB access happens.

    The `make` callable it yields mirrors `seeded_user_factory.make` but is
    intentionally minimal: it seeds a plain free-tier user (no role /
    subscription / verification shaping), which is all `auth_session`
    promises. Tests needing shaping use `seeded_user_factory` directly and
    stay hermetic-only.
    """
    if RUN_MODE != "hermetic":
        yield None
        return

    pg = await asyncpg.connect(USER_POSTGRES_DSN)
    redis = aioredis.from_url(USER_REDIS_URL, decode_responses=True)
    created_user_ids: list[uuid.UUID] = []
    created_auth_codes: list[str] = []

    async def make() -> tuple[SeededUser, str]:
        email = f"test-{secrets.token_hex(4)}@example.com"
        user_id = await _seed_user(pg, email=email, roles=[])
        created_user_ids.append(user_id)

        auth_code = secrets.token_urlsafe(48)
        payload = json.dumps(
            {
                "user_id": str(user_id),
                "email": email,
                "roles": [],
                "subscription_status": "free",
            }
        )
        await redis.setex(f"auth_code:{auth_code}", 60, payload)
        created_auth_codes.append(auth_code)
        return (
            SeededUser(user_id=user_id, email=email, roles=[], subscription_status="free"),
            auth_code,
        )

    try:
        yield make
    finally:
        for code in created_auth_codes:
            await redis.delete(f"auth_code:{code}")
        for uid in created_user_ids:
            await pg.execute("DELETE FROM sessions WHERE user_id = $1", uid)
            await pg.execute("DELETE FROM user_roles WHERE user_id = $1", uid)
            await pg.execute("DELETE FROM subscriptions WHERE user_id = $1", uid)
            await pg.execute("DELETE FROM oauth_accounts WHERE user_id = $1", uid)
            await pg.execute("DELETE FROM verification_tokens WHERE user_id = $1", uid)
            await pg.execute("DELETE FROM totp_secrets WHERE user_id = $1", uid)
            await pg.execute("DELETE FROM users WHERE id = $1", uid)
        await redis.aclose()
        await pg.close()


@pytest.fixture
async def auth_session(_hermetic_seed, remote_test_user, user_service) -> AuthSession:
    """Mode-aware authenticated session for the default (unshaped) test-user.

    * hermetic — seeds a fresh free-tier user DB-direct (via `_hermetic_seed`)
      and issues a token via the one-time-code path. Email is a unique
      per-run address.
    * remote — returns the pre-provisioned stg test-user's freshly-rotated
      token pair (via `remote_test_user`).

    Both underlying fixtures are declared as direct parameters (the nesting
    pytest-asyncio supports). The non-active one is a cheap no-op in the
    current mode: `_hermetic_seed` yields `None` in remote mode without
    touching the DB, and `remote_test_user` skips only when its own
    credential is needed — but `auth_session` reaches `remote_test_user`
    solely in the remote branch, so in hermetic mode `remote_test_user`'s
    skip is never triggered because the remote branch is not taken.

    Use this in tests that need *an* authenticated user but do NOT need a
    specific role / subscription tier / verification state. Tests that DO
    need such shaping must stay on `seeded_user_factory` (hermetic-only) and
    skip in remote mode with REMOTE_SHAPING_UNSUPPORTED_REASON.
    """
    if RUN_MODE == "hermetic":
        seeded, auth_code = await _hermetic_seed()
        tokens = await issue_token_for(user_service, auth_code)
        return AuthSession(
            access_token=str(tokens["access_token"]),
            refresh_token=str(tokens["refresh_token"]),
            email=seeded.email,
        )
    return remote_test_user
