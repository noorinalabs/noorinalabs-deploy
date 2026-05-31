"""Scenario 3: Session management through user-service."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import AuthSession


@pytest.mark.asyncio
async def test_session_lifecycle(
    auth_session: AuthSession, user_service: httpx.AsyncClient
) -> None:
    # Session create/list/revoke works for any authenticated user — no role
    # or subscription shaping needed — so this runs in both modes.
    headers = {"Authorization": f"Bearer {auth_session.access_token}"}

    # The token-issuance flow already writes a session row; it must be listable.
    r = await user_service.get("/api/v1/sessions", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "sessions" in body
    assert len(body["sessions"]) >= 1
    session_id = body["sessions"][0]["id"]

    # Revoke a single session.
    r = await user_service.delete(f"/api/v1/sessions/{session_id}", headers=headers)
    assert r.status_code == 204

    # Revoke-all.
    r = await user_service.delete("/api/v1/sessions", headers=headers)
    # 200 with a revoked count (or 401 if the previous delete already revoked
    # the current session — either is valid).
    assert r.status_code in (200, 401)
