"""Authenticated principal echo — M0 verify target."""

from __future__ import annotations

from fastapi import APIRouter

from archmentor_api.deps import CurrentUser, Principal

router = APIRouter(tags=["me"])


@router.get("/me", response_model=Principal)
def get_me(user: CurrentUser) -> Principal:
    return user
