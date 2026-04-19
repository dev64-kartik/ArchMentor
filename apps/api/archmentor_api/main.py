"""FastAPI application entry point."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from archmentor_api import __version__
from archmentor_api.config import get_settings
from archmentor_api.routes import health, livekit_tokens, me, problems, reports, sessions


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="ArchMentor API",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(me.router)
    app.include_router(problems.router)
    app.include_router(sessions.router)
    app.include_router(reports.router)
    app.include_router(livekit_tokens.router)

    return app


app = create_app()
