"""FastAPI application factory for the ohmo gateway HTTP API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ohmo.gateway.dependencies import get_runtime
from ohmo.gateway.routers import agents, chat, models, skills, tasks

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize shared runtime singletons on startup."""
    runtime = get_runtime()

    # Task manager
    try:
        from openharness.tasks.manager import BackgroundTaskManager

        runtime.task_manager = BackgroundTaskManager()
        logger.info("BackgroundTaskManager initialized")
    except Exception as exc:
        logger.warning("Could not initialize BackgroundTaskManager: %s", exc)

    # MCP manager
    try:
        from openharness.mcp.client import McpClientManager
        from openharness.mcp.config import load_mcp_server_configs
        from openharness.config import load_settings

        settings = load_settings()
        server_configs = load_mcp_server_configs(settings, [])
        runtime.mcp_manager = McpClientManager(server_configs)
        logger.info("McpClientManager initialized")
    except Exception as exc:
        logger.warning("Could not initialize McpClientManager: %s", exc)

    yield

    # Teardown
    if runtime.mcp_manager is not None:
        try:
            await runtime.mcp_manager.close()
        except Exception:
            pass


def create_app(
    *,
    cors_origins: list[str] | None = None,
    workspace: str | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    runtime = get_runtime()
    runtime.workspace = workspace

    app = FastAPI(
        title="OpenHarness Agent API",
        description="HTTP API for the openharness AI agent framework",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # CORS — allow the web front-end and common dev origins
    allowed_origins = cors_origins or [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:2415",
        "http://localhost:2440",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:2415",
        "http://127.0.0.1:2440",
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_origin_regex=None if cors_origins else r"https?://[^/]+:(2415|2440|5173|3000)",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount routers under /agent/api/v1
    prefix = "/agent/api/v1"
    app.include_router(chat.router, prefix=prefix)
    app.include_router(agents.router, prefix=prefix)
    app.include_router(skills.router, prefix=prefix)
    app.include_router(tasks.router, prefix=prefix)
    app.include_router(models.router, prefix=prefix)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app
