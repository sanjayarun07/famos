from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from familyos.identity import Principal, authenticate

_bearer = HTTPBearer(auto_error=False)


async def _principal(creds: HTTPAuthorizationCredentials | None, *, allow_erasing: bool) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in required", headers={"WWW-Authenticate": "Bearer"})
    principal = await authenticate(creds.credentials, allow_erasing=allow_erasing)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked token", headers={"WWW-Authenticate": "Bearer"})
    return principal


async def current_member(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    return await _principal(creds, allow_erasing=False)


async def erasure_reader(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    """Read-only, and the only thing an erasing household still allows: a
    guardian watching the erasure they asked for. It reads `erasure_log`,
    which holds no personal data."""
    return await _principal(creds, allow_erasing=True)
