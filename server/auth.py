"""API-key authentication for the server.

Every request must carry the shared secret in the ``X-API-Key`` header. The
comparison uses :func:`secrets.compare_digest` to avoid leaking the key through
timing differences.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from server.config import load_config

_CONFIG = load_config()


def key_matches(given: str) -> bool:
    """True if ``given`` is the configured API key.

    Both sides are compared as UTF-8 bytes: ``compare_digest`` raises on a str
    holding non-ASCII characters, which would turn a wrong key into a 500.
    """
    return secrets.compare_digest(given.encode("utf-8"), _CONFIG.api_key.encode("utf-8"))


def require_api_key(x_api_key: str = Header(default="")) -> None:
    """FastAPI dependency that rejects requests with a missing/wrong key."""
    if not key_matches(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key",
        )
