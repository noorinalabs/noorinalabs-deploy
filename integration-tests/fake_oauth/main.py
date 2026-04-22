"""Fake OAuth provider for integration tests (noorinalabs-main#135).

Implements just enough of Google's OAuth 2.0 / OpenID Connect surface to
satisfy user-service's GoogleOAuthProvider when
OAUTH_PROVIDER_BASE_URL_OVERRIDE points all four providers' hosts here.
Only Google is exercised end-to-end; the other providers' tests still use
the pre-#135 Redis shim in conftest.py.
"""

from __future__ import annotations

import os
import secrets
import time

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

AUDIENCE = os.environ.get("FAKE_OAUTH_AUDIENCE", "fake-google-client-id")

app = FastAPI(title="fake_oauth", version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/o/oauth2/v2/auth")
def google_authorize(request: Request) -> RedirectResponse:
    """Google's authorize endpoint.

    Redirects 302 back to the caller's redirect_uri with a fake code + the
    incoming state. Tests typically skip this step and POST a fabricated
    code straight to user-service's callback; the endpoint exists for
    completeness.
    """
    params = request.query_params
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code = f"FAKE_CODE_{secrets.token_urlsafe(16)}"
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        url=f"{redirect_uri}{sep}code={code}&state={state}",
        status_code=302,
    )


@app.post("/token")
async def google_token(request: Request) -> JSONResponse:
    """Google's token endpoint.

    Accepts any authorization code (the fake issued above, or one crafted
    directly by a test) and returns canned tokens. id_token is a stub
    string — Google's flow never verifies it in user-service; only Apple
    does, and Apple isn't exercised here.
    """
    # Read form body defensively — user-service posts form-encoded data.
    try:
        form = await request.form()
        code = str(form.get("code", ""))
    except Exception:
        code = ""

    if not code:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "missing code"},
            status_code=400,
        )

    now = int(time.time())
    fake_suffix = secrets.token_urlsafe(8)
    return JSONResponse(
        {
            "access_token": f"fake-access-{fake_suffix}",
            "id_token": f"fake-id-token.{AUDIENCE}.{now}",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": f"fake-refresh-{fake_suffix}",
            "scope": "openid email profile",
        }
    )


@app.get("/oauth2/v3/userinfo")
def google_userinfo(request: Request) -> JSONResponse:
    """Google's userinfo endpoint.

    Returns a deterministic fake user keyed off a random suffix baked into
    the access_token on the fly. We derive the suffix to keep sub/email
    stable within a single test flow (one token → one userinfo lookup).
    """
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    suffix = token.removeprefix("fake-access-") if token.startswith("fake-access-") else "anon"

    return JSONResponse(
        {
            "sub": f"fake-user-{suffix}",
            "email": f"fake-{suffix}@example.com",
            "email_verified": True,
            "name": f"Fake User {suffix}",
            "picture": "https://example.invalid/avatar.png",
        }
    )
