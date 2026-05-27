"""Bearer token middleware for the Helios HTTP API.

Per HELIOS.md §7.1, every endpoint except ``/v1/health`` requires a bearer
token from ``[api].bearer_token`` in ``~/.aegis/capture.toml``. The token
is loaded once at daemon startup and stashed on ``app.state.config``.

The single public surface here is :func:`require_token`, used as a
FastAPI dependency. Mount it on individual routes or — preferred — on a
router via ``include_router(..., dependencies=[Depends(require_token)])``
so the auth invariant is impossible to forget per route.

Failure modes (per HELIOS.md §7.2 error envelope):

* ``bearer_token`` empty in config        → 503 ``auth_not_configured``
* ``Authorization`` header missing/malformed → 401 ``unauthorized``
* token mismatch                          → 401 ``unauthorized``

Comparisons are constant-time via :func:`hmac.compare_digest` to avoid
leaking length / prefix information through timing.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Header, HTTPException, Request, status

from helios.log import get_logger

_log = get_logger("api.auth")


async def require_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """FastAPI dependency that enforces the configured bearer token.

    Raises :class:`HTTPException` with the §7.2 error envelope on any
    failure. The flat ``{"error", "detail", "code"}`` shape is produced
    by the global ``HTTPException`` handler registered in
    :mod:`helios.api`.
    """
    configured = request.app.state.config.api.bearer_token
    if not configured:
        # Daemon misconfigured — fail closed rather than allowing bypass.
        _log.error("auth_token_not_configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "auth_not_configured",
                "detail": "bearer_token missing in capture.toml",
                "code": status.HTTP_503_SERVICE_UNAVAILABLE,
            },
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "unauthorized",
                "detail": "missing or malformed Authorization header",
                "code": status.HTTP_401_UNAUTHORIZED,
            },
        )

    presented = authorization[len("Bearer ") :].strip()
    if not hmac.compare_digest(presented.encode(), configured.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "unauthorized",
                "detail": "invalid token",
                "code": status.HTTP_401_UNAUTHORIZED,
            },
        )


__all__ = ["require_token"]
