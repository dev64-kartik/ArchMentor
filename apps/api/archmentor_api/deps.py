"""FastAPI dependencies: auth, principals."""

from __future__ import annotations

import hmac
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
    # Algorithm is pinned to HS256 (GoTrue default) to prevent algorithm
    # confusion attacks via a misconfigured env var.
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
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


def require_agent(
    settings: Annotated[Settings, Depends(get_settings)],
    x_agent_token: Annotated[str | None, Header(alias="X-Agent-Token")] = None,
) -> None:
    """Authenticate the LiveKit agent worker via shared secret.

    The agent is not a user and has no Supabase JWT; it presents the
    static `AGENT_INGEST_TOKEN` on backend-to-backend calls. Constant-time
    compare to avoid timing side channels.

    A missing header is 401 (prompting the caller to authenticate); a
    present-but-wrong token is 403 (the caller authenticated but is not
    authorized). This distinction lets the agent's ledger client treat
    "misconfigured token" as a hard failure while still retrying
    genuine transient issues.
    """
    if not x_agent_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing agent credentials",
            headers={"WWW-Authenticate": 'AgentToken realm="archmentor"'},
        )
    if not hmac.compare_digest(x_agent_token, settings.agent_ingest_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid agent credentials",
        )


AgentAuth = Annotated[None, Depends(require_agent)]
