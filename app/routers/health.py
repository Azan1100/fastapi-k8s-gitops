"""
routers/health.py
-----------------
Health and readiness endpoints.

Kubernetes uses two distinct probe types:
  • Liveness  (GET /health)       — "is the process alive?"
                                    Returns 200 even if Redis is down so K8s
                                    doesn't restart a pod that is simply waiting
                                    for Sentinel failover.

  • Readiness (GET /health/redis) — "is this pod ready to serve traffic?"
                                    Returns 503 if Redis is unreachable so the
                                    pod is temporarily removed from the Service
                                    endpoints until connectivity is restored.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Response

from config import settings
from models import HealthResponse
from redis_client import redis_manager

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Always returns 200 while the Python process is running.",
)
def liveness() -> HealthResponse:
    """Kubernetes liveness probe — fast, no external calls."""
    return HealthResponse(
        status    = "ok",
        redis     = {"note": "not checked on liveness probe"},
        version   = settings.app_version,
        timestamp = datetime.now(timezone.utc),
    )


@router.get(
    "/health/redis",
    response_model=HealthResponse,
    summary="Readiness probe — checks Redis connectivity",
    description="Returns 503 if the Redis master or replica is unreachable.",
)
def readiness(response: Response) -> HealthResponse:
    """Kubernetes readiness probe — verifies Redis master + replica are reachable."""
    redis_status = redis_manager.health_check()

    # If master is down the pod is not ready to serve (writes would fail)
    is_healthy = "error" not in redis_status.get("master", "error")
    overall    = "ok" if is_healthy else "unhealthy"

    if not is_healthy:
        response.status_code = 503   # Remove this pod from Service endpoints

    return HealthResponse(
        status    = overall,
        redis     = redis_status,
        version   = settings.app_version,
        timestamp = datetime.now(timezone.utc),
    )
