"""FastAPI dependencies: auth, principals."""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel

from archmentor_api.config import Settings, get_settings


class Principal(BaseModel):
    """Authenticated caller derived from a verified Supabase JWT."""

    user_id: str
    email: str | None = None
    role: str = "authenticated"


def _verify_jwt(token: str, settings: Settings) -> dict[str, object]:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_user(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Extract and verify the Supabase JWT; return the Principal."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.split(" ", 1)[1].strip()
    claims = _verify_jwt(token, settings)

    subject = claims.get("sub")
    if not isinstance(subject, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject",
        )

    email = claims.get("email")
    role = claims.get("role", "authenticated")
    return Principal(
        user_id=subject,
        email=email if isinstance(email, str) else None,
        role=role if isinstance(role, str) else "authenticated",
    )


CurrentUser = Annotated[Principal, Depends(require_user)]
