"""Angel AI Service — Enterprise AI layer powered by Claude Code CLI.

Provides AI chat capabilities to angel-kpi and future systems.
Uses Claude Max subscription via `claude -p` headless mode.
"""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from db.sqlite_db import init_db
from routers import admin, chat, health

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(str(__import__("pathlib").Path(__file__).parent / "logs" / "service.log"), encoding="utf-8")],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and validate config on startup."""
    if settings.SERVICE_TOKEN_SECRET == "change-me-in-production":
        logger.critical("SERVICE_TOKEN_SECRET is still default! Set it in .env before production use.")
    init_db()
    logger.info(f"AI Service started on port {settings.SERVICE_PORT}")
    yield


app = FastAPI(
    title=settings.SERVICE_NAME,
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow KPI frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-User-Context"],
)

# Register routers
app.include_router(chat.router)
app.include_router(health.router)
app.include_router(admin.router)
