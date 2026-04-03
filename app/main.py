"""
main.py
-------
FastAPI application entry point for Nexus — a production-grade Task
Management API demonstrating Redis Sentinel HA on Kubernetes.

Startup order (important):
  1. FastAPI app is instantiated
  2. OpenTelemetry is wired up (must happen before any Redis calls so the
     Redis auto-instrumentation can wrap the client)
  3. Middleware is registered
  4. Routers are mounted
  5. Static files are served last (catch-all route)

The `lifespan` context manager handles graceful startup/shutdown logging
without blocking the event loop.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import settings

# ── Logging ───────────────────────────────────────────────────────────────────
# Configure structured logging early so every log line has a consistent format.
# OTel's LoggingInstrumentor will later inject trace_id / span_id automatically.
logging.basicConfig(
    level   = logging.DEBUG if settings.debug else logging.INFO,
    format  = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream  = sys.stdout,
)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Runs code before the server accepts requests (startup) and after it
    stops (shutdown).  This is the modern FastAPI alternative to
    @app.on_event("startup").
    """
    logger.info("═══ Nexus API v%s starting up ═══", settings.app_version)
    logger.info("Environment : %s", settings.environment)
    logger.info("OTel target : %s", settings.otel_endpoint)
    logger.info("Redis master set : %s", settings.redis_master_set)
    logger.info("Sentinel hosts   : %s", settings.redis_sentinel_hosts)

    yield   # ← Server is live and serving requests between here

    logger.info("═══ Nexus API shutting down ═══")


# ── Application ───────────────────────────────────────────────────────────────

app = FastAPI(
    title       = settings.app_name,
    version     = settings.app_version,
    description = (
        "**Nexus** is a production-grade Task Management API built with FastAPI, "
        "backed by Redis Sentinel for High Availability, and fully instrumented "
        "with OpenTelemetry (traces, metrics, logs) shipping to SigNoz.\n\n"
        "### Features\n"
        "- Redis Sentinel HA with read/write split\n"
        "- Sliding-window rate limiting via Redis\n"
        "- OpenTelemetry auto + custom instrumentation\n"
        "- GitOps deployment via ArgoCD\n"
    ),
    docs_url    = "/docs",
    redoc_url   = "/redoc",
    lifespan    = lifespan,
)

# ── OpenTelemetry ─────────────────────────────────────────────────────────────
# Import and initialise BEFORE registering middleware/routers so the Redis
# auto-instrumentation wraps the client before any connection is made.
import telemetry.setup as _tel_module
_metrics = _tel_module.setup_telemetry(app)
# Expose via the module-level variable so routers can `from telemetry.setup import app_metrics`
_tel_module.app_metrics = _metrics

# ── Middleware ────────────────────────────────────────────────────────────────
# Middleware is executed in LIFO order (last registered = first executed).
from middleware.rate_limiter import RateLimitMiddleware        # noqa: E402
app.add_middleware(RateLimitMiddleware)

# ── Routers ───────────────────────────────────────────────────────────────────
from routers import analytics, health, tasks                  # noqa: E402

app.include_router(health.router)                              # /health, /health/redis
app.include_router(tasks.router,     prefix="/api/v1")         # /api/v1/tasks
app.include_router(analytics.router, prefix="/api/v1")         # /api/v1/analytics

# ── Static Files ──────────────────────────────────────────────────────────────
# Mount LAST — StaticFiles acts as a catch-all and would intercept API routes
# if registered earlier.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
