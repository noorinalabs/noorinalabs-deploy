"""Performance baseline: auth latency < 100ms median over 20 samples.

In-container LAN only, so this excludes the client-side internet latency.
The threshold is intentionally loose (100ms) so flaky CI runners don't trip it.
"""

from __future__ import annotations

import statistics
import time

import httpx
import pytest

from tests.conftest import issue_token_for


@pytest.mark.performance
@pytest.mark.asyncio
async def test_token_issuance_latency_baseline(
    seeded_user_factory, user_service: httpx.AsyncClient
) -> None:
    samples_ms = []
    for _ in range(20):
        _, auth_code = await seeded_user_factory()
        t0 = time.perf_counter()
        await issue_token_for(user_service, auth_code)
        samples_ms.append((time.perf_counter() - t0) * 1000)

    median = statistics.median(samples_ms)
    p95 = sorted(samples_ms)[int(len(samples_ms) * 0.95) - 1]

    # Emit for CI logs.
    print(f"auth latency — median={median:.1f}ms p95={p95:.1f}ms n={len(samples_ms)}")

    assert median < 100, f"median auth latency {median:.1f}ms exceeds 100ms baseline"


@pytest.mark.performance
@pytest.mark.asyncio
async def test_token_validate_latency_baseline(
    seeded_user_factory, user_service: httpx.AsyncClient
) -> None:
    _, auth_code = await seeded_user_factory()
    tokens = await issue_token_for(user_service, auth_code)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    samples_ms = []
    for _ in range(20):
        t0 = time.perf_counter()
        r = await user_service.get("/auth/token/validate", headers=headers)
        assert r.status_code == 200
        samples_ms.append((time.perf_counter() - t0) * 1000)

    median = statistics.median(samples_ms)
    print(f"token validate — median={median:.1f}ms n={len(samples_ms)}")
    assert median < 100, f"validate latency {median:.1f}ms exceeds 100ms baseline"
