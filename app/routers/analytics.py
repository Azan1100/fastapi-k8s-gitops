"""
routers/analytics.py
--------------------
Real-time analytics derived from Redis counters.

All reads use the replica to keep the master free for writes.
Counters are maintained atomically by the task router (INCR / DECR) so
this endpoint is purely a read-only aggregation view — no heavy scanning.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from opentelemetry import trace

from models import AnalyticsSummary, Priority, Status
from redis_client import redis_manager

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get(
    "/summary",
    response_model=AnalyticsSummary,
    summary="Aggregated task and cache metrics",
)
def get_summary() -> AnalyticsSummary:
    """
    Return a dashboard-friendly summary of task counts and cache performance.

    Data sources (all reads from replica):
      analytics:created          — lifetime tasks created
      analytics:deleted          — lifetime tasks deleted
      analytics:hits             — Redis cache hits since startup
      analytics:misses           — Redis cache misses since startup
      analytics:status:{status}  — per-status counts
    """
    with tracer.start_as_current_span("analytics.summary"):
        replica = redis_manager.replica

        # Fetch all analytics keys in one pipeline to minimise round-trips
        pipe = replica.pipeline(transaction=False)  # Read-only, no MULTI/EXEC needed

        # Queue all GETs
        pipe.get("analytics:hits")
        pipe.get("analytics:misses")
        pipe.get("analytics:created")
        pipe.get("analytics:deleted")
        for s in Status:
            pipe.get(f"analytics:status:{s.value}")

        results = pipe.execute()

        hits    = int(results[0] or 0)
        misses  = int(results[1] or 0)
        created = int(results[2] or 0)
        deleted = int(results[3] or 0)

        by_status: dict[str, int] = {}
        for i, s in enumerate(Status):
            by_status[s.value] = int(results[4 + i] or 0)

        # Live task count = ever created minus ever deleted
        total_tasks = max(0, created - deleted)

        # Cache hit rate — avoid division by zero on a fresh deployment
        total_lookups = hits + misses
        hit_rate = round((hits / total_lookups) * 100, 1) if total_lookups > 0 else 0.0

        # Rate-limit rejections come from a separate key written by the middleware
        rate_rejected = int(replica.get("ratelimit:rejected_total") or 0)

        # Priority breakdown — scan the live index (small set, O(n) is fine)
        task_ids  = replica.smembers("tasks:index")
        by_priority: dict[str, int] = {p.value: 0 for p in Priority}

        if task_ids:
            pipe2 = replica.pipeline(transaction=False)
            for tid in task_ids:
                pipe2.hget(f"task:{tid}", "priority")
            priorities = pipe2.execute()
            for p in priorities:
                if p and p in by_priority:
                    by_priority[p] += 1

        return AnalyticsSummary(
            total_tasks            = total_tasks,
            by_status              = by_status,
            by_priority            = by_priority,
            cache_hits             = hits,
            cache_misses           = misses,
            cache_hit_rate_pct     = hit_rate,
            rate_limited_requests  = rate_rejected,
        )
