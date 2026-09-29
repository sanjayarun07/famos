from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from familyos.identity import Principal, authenticate

_bearer = HTTPBearer(auto_error=False)


async def current_member(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in required", headers={"WWW-Authenticate": "Bearer"})
    principal = await authenticate(creds.credentials)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked token", headers={"WWW-Authenticate": "Bearer"})
    return principal
