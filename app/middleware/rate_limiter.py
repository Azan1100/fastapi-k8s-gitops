"""
middleware/rate_limiter.py
--------------------------
Redis-backed sliding-window rate limiter implemented as a Starlette middleware.

Algorithm — Fixed Window Counter (simple & efficient for most APIs):
  1. Key: "ratelimit:{client_ip}"
  2. INCR the key on every request
  3. On first increment (result == 1), set an expiry equal to the window size
  4. If the counter exceeds the limit, return 429 before the request reaches
     any route handler

Why Redis for rate limiting?
  - State is shared across all 4 FastAPI pod replicas — a simple in-process
    dict would let each pod allow `limit` requests, giving `limit × replicas`
    effective throughput
  - After a Redis failover, Sentinel re-routes to the new master in < 30s;
    the brief window reset is an acceptable trade-off vs. running stateless

Why write to master?
  INCR is a write operation; it must go to the master node.  Replicas are
  read-only by design.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from config import settings
from redis_client import redis_manager

logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Starlette BaseHTTPMiddleware subclass — runs before every route handler.

    Skips health-check endpoints so Kubernetes liveness/readiness probes
    are never rate-limited (they're not client traffic).
    """

    # Endpoints exempt from rate limiting
    EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/health/redis"})

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)

        client_ip = self._get_client_ip(request)
        key = f"ratelimit:{client_ip}"

        try:
            master = redis_manager.master

            # INCR is atomic — safe under concurrent replicas
            count = master.incr(key)

            if count == 1:
                # First request in this window: arm the expiry
                master.expire(key, settings.rate_limit_window_seconds)

            remaining = max(0, settings.rate_limit_max_requests - count)

            if count > settings.rate_limit_max_requests:
                # Record the rejection in OTel metrics (import lazily to avoid
                # circular import at module load time)
                try:
                    from telemetry.setup import app_metrics
                    if app_metrics:
                        app_metrics.rate_limit_rejected.add(1, {"client_ip": client_ip})
                except ImportError:
                    pass

                logger.warning("Rate limit exceeded for IP %s (count=%d)", client_ip, count)
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "Too Many Requests",
                        "detail": f"Rate limit of {settings.rate_limit_max_requests} "
                                  f"requests per {settings.rate_limit_window_seconds}s exceeded.",
                        "retry_after": settings.rate_limit_window_seconds,
                    },
                    headers={
                        "Retry-After":               str(settings.rate_limit_window_seconds),
                        "X-RateLimit-Limit":         str(settings.rate_limit_max_requests),
                        "X-RateLimit-Remaining":     "0",
                        "X-RateLimit-Reset-Seconds": str(settings.rate_limit_window_seconds),
                    },
                )

        except Exception as exc:
            # Redis unavailable — fail open (allow request) rather than
            # denying all traffic during a brief Sentinel failover
            logger.error("Rate limiter Redis error (failing open): %s", exc)

        response = await call_next(request)

        # Attach rate-limit headers to every successful response
        try:
            response.headers["X-RateLimit-Limit"]     = str(settings.rate_limit_max_requests)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
        except Exception:
            pass   # Header mutation can fail on streaming responses

        return response

    @staticmethod
    def _get_client_ip(request: Request) -> str:
        """
        Extract the real client IP, respecting X-Forwarded-For from the
        load balancer / ingress controller.
        """
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
