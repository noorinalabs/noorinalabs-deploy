"""Per-service Settings SHIMS for the env-validate settings-load gate.

The deploy repo's CI checks out only the deploy repo, so each service's real
Pydantic Settings class (in its own repo's src/) is not importable here. The
deploy#332 design explicitly blesses "a thin per-service shim" as the
alternative. Each shim below mirrors the SECURITY-RELEVANT constraints of the
real Settings class so the gate catches the failures that matter from the
assembled env tiers, while staying hermetic (no service-package install, no
sibling checkout).

Each shim is a callable `(assembled_env: dict[str, str], env: str) -> None`
that raises on any validation failure. `env` is the target environment name
("stg"/"prod") and is mapped to the service's ENVIRONMENT semantics where the
real class gates on it (user-service production/staging guards).

Keep these shims in sync with the real classes when a service adds a validated,
deploy-relevant field:
  - user-service: noorinalabs-user-service/src/app/config.py  (Settings)
  - isnad-graph:  noorinalabs-isnad-graph/src/config.py       (Settings + nested)
"""

from __future__ import annotations

from urllib.parse import urlparse

# Map the deploy env name to the ENVIRONMENT value services expect.
_ENV_TO_SERVICE_ENVIRONMENT = {"stg": "staging", "prod": "production"}

# user-service Settings._validate_environment allowlist.
_US_ALLOWED_ENVIRONMENTS = frozenset({"development", "test", "staging", "production"})
_US_PROD_LIKE = frozenset({"production", "staging"})
_US_OVERRIDE_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _validate_user_service(env_map: dict[str, str], env: str) -> None:
    """Mirror the deploy-relevant validators in user-service Settings.

    - ENVIRONMENT must be in the allowlist (we set it from the deploy env).
    - OAUTH_PROVIDER_BASE_URL_OVERRIDE must be unset in prod-like envs and, if
      set outside ENVIRONMENT=test, must be https — the real model_validator's
      prod-guard. The deploy tiers should NEVER set this in stg/prod, so the
      gate asserts the assembled env doesn't accidentally introduce it.
    """
    environment = _ENV_TO_SERVICE_ENVIRONMENT.get(env, env)
    if environment not in _US_ALLOWED_ENVIRONMENTS:
        raise ValueError(
            f"user-service ENVIRONMENT must be one of {sorted(_US_ALLOWED_ENVIRONMENTS)}, "
            f"got {environment!r}"
        )

    override = env_map.get("OAUTH_PROVIDER_BASE_URL_OVERRIDE", "").strip()
    if override:
        parsed = urlparse(override)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(
                f"OAUTH_PROVIDER_BASE_URL_OVERRIDE must include scheme and host, got {override!r}"
            )
        if parsed.scheme not in _US_OVERRIDE_ALLOWED_SCHEMES:
            raise ValueError(
                "OAUTH_PROVIDER_BASE_URL_OVERRIDE scheme must be one of "
                f"{sorted(_US_OVERRIDE_ALLOWED_SCHEMES)}, got {parsed.scheme!r}"
            )
        if environment in _US_PROD_LIKE:
            raise ValueError(
                "OAUTH_PROVIDER_BASE_URL_OVERRIDE must not be set in "
                "production/staging environments"
            )

    # RS256 keypair: prod-like envs must have both halves provisioned (as
    # placeholders here) — a missing key would break JWT issuance at runtime.
    if environment in _US_PROD_LIKE:
        for key in ("JWT_PRIVATE_KEY", "JWT_PUBLIC_KEY"):
            if not env_map.get(key, "").strip():
                raise ValueError(
                    f"user-service requires {key} to be provisioned in {env} "
                    f"(absent from the assembled tiers + secret catalogue)"
                )


def _validate_isnad_graph(env_map: dict[str, str], env: str) -> None:
    """Mirror the deploy-relevant constraints in isnad-graph Settings.

    isnad-graph's Settings fields are all-defaulted (nested prefixed models),
    so there is no hard ValidationError from missing vars. The deploy-relevant
    invariant the gate enforces: the DB/cache credentials compose marks as
    required for the api must be present in some tier so the container starts.
    """
    for key in ("NEO4J_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD"):
        if not env_map.get(key, "").strip():
            raise ValueError(
                f"isnad-graph requires {key} to be provisioned in {env} "
                f"(absent from the assembled tiers + secret catalogue)"
            )


SERVICE_SCHEMAS = {
    "user-service": _validate_user_service,
    "isnad-graph": _validate_isnad_graph,
}
