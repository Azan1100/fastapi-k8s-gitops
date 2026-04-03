"""
routers/tasks.py
----------------
Task CRUD endpoints demonstrating the Redis Sentinel read/write split.

Redis key schema:
  task:{id}              — Hash     — full task fields
  tasks:index            — Set      — set of all task IDs
  tasks:status:{status}  — Set      — IDs grouped by status
  tasks:cache:list       — String   — serialised task list (cache layer)
  analytics:hits         — String   — counter for cache hits
  analytics:misses       — String   — counter for cache misses
  analytics:created      — String   — total tasks ever created
  analytics:deleted      — String   — total tasks ever deleted
  analytics:status:{s}   — String   — per-status counter

Read / Write split enforced throughout:
  ✦ GET requests  → redis_manager.replica (reads from a replica)
  ✦ POST/PUT/DEL  → redis_manager.master  (writes always to master)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from opentelemetry import trace

from config import settings
from models import Priority, Status, Task, TaskCreate, TaskListResponse, TaskResponse, TaskUpdate
from redis_client import redis_manager

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

router = APIRouter(prefix="/tasks", tags=["Tasks"])

# ── Internal helpers ──────────────────────────────────────────────────────────

def _task_key(task_id: str) -> str:
    return f"task:{task_id}"

def _status_index_key(status: Status) -> str:
    return f"tasks:status:{status.value}"

def _record_hit() -> None:
    """Atomically increment the cache-hit counter on the master."""
    try:
        redis_manager.master.incr("analytics:hits")
    except Exception:
        pass   # Non-critical — don't let a counter failure break a request

def _record_miss() -> None:
    try:
        redis_manager.master.incr("analytics:misses")
    except Exception:
        pass


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=TaskResponse,
    status_code=201,
    summary="Create a task",
)
def create_task(payload: TaskCreate) -> TaskResponse:
    """
    Persist a new task to Redis and return it.

    Steps:
      1. Build Task object with a new UUID and timestamps
      2. HSET task:{id} with all fields          → master
      3. SADD tasks:index {id}                   → master
      4. SADD tasks:status:{status} {id}         → master
      5. Invalidate the list cache               → master
      6. Increment analytics counters            → master
    """
    with tracer.start_as_current_span("task.create") as span:
        task = Task.new(payload)
        span.set_attribute("task.id",       task.id)
        span.set_attribute("task.priority", task.priority.value)
        span.set_attribute("task.status",   task.status.value)

        t0 = time.monotonic()
        master = redis_manager.master

        # Store task data
        master.hset(_task_key(task.id), mapping=task.to_redis_hash())

        # Maintain index sets for efficient listing and filtering
        master.sadd("tasks:index", task.id)
        master.sadd(_status_index_key(task.status), task.id)

        # Invalidate the cached task list so the next GET /tasks is fresh
        master.delete("tasks:cache:list")

        # Analytics
        master.incr("analytics:created")
        master.incr(f"analytics:status:{task.status.value}")

        duration_ms = (time.monotonic() - t0) * 1000

        # OTel business metrics
        try:
            from telemetry.setup import app_metrics
            if app_metrics:
                app_metrics.tasks_created.add(1, {"priority": task.priority.value})
                app_metrics.task_operation_duration.record(duration_ms, {"operation": "create"})
        except ImportError:
            pass

        logger.info("Task created id=%s priority=%s", task.id, task.priority.value)
        return TaskResponse(data=task, cached=False)


@router.get(
    "",
    response_model=TaskListResponse,
    summary="List all tasks",
    description="Returns all tasks, optionally filtered by status or priority. "
                "Result is cached in Redis for 5 minutes.",
)
def list_tasks(
    status:   Optional[Status]   = Query(None, description="Filter by status"),
    priority: Optional[Priority] = Query(None, description="Filter by priority"),
) -> TaskListResponse:
    """
    List tasks with optional filters.

    When no filters are specified the result is served from a Redis string
    cache (`tasks:cache:list`) to avoid scanning all task hashes.
    Filtered requests bypass the cache to keep logic simple.
    """
    with tracer.start_as_current_span("task.list") as span:
        span.set_attribute("filter.status",   status.value   if status   else "none")
        span.set_attribute("filter.priority", priority.value if priority else "none")

        # Use replica for all reads
        replica = redis_manager.replica

        # Fast path: serve from list cache when no filters
        if status is None and priority is None:
            cached_json = replica.get("tasks:cache:list")
            if cached_json:
                _record_hit()
                span.set_attribute("cache.hit", True)
                tasks = [Task(**t) for t in json.loads(cached_json)]
                return TaskListResponse(data=tasks, total=len(tasks), cached=True)

        _record_miss()
        span.set_attribute("cache.hit", False)

        # Determine which IDs to fetch
        if status is not None:
            task_ids = replica.smembers(_status_index_key(status))
        else:
            task_ids = replica.smembers("tasks:index")

        if not task_ids:
            return TaskListResponse(data=[], total=0, cached=False)

        tasks: list[Task] = []
        for task_id in task_ids:
            raw = replica.hgetall(_task_key(task_id))
            if not raw:
                continue   # Stale index entry — skip gracefully
            task = Task.from_redis_hash(raw)

            # Apply priority filter in Python (small sets, so no perf concern)
            if priority and task.priority != priority:
                continue

            tasks.append(task)

        # Sort by created_at descending (newest first)
        tasks.sort(key=lambda t: t.created_at, reverse=True)

        # Populate the list cache only on unfiltered requests
        if status is None and priority is None:
            redis_manager.master.setex(
                "tasks:cache:list",
                settings.cache_ttl_seconds,
                json.dumps([t.model_dump(mode="json") for t in tasks]),
            )

        return TaskListResponse(data=tasks, total=len(tasks), cached=False)


@router.get(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Get a single task",
)
def get_task(task_id: str) -> TaskResponse:
    """
    Fetch a task by ID.

    Cache key: `task:{id}` (the hash itself acts as the cache — no separate
    cache layer needed for single-task lookups).
    """
    with tracer.start_as_current_span("task.get") as span:
        span.set_attribute("task.id", task_id)

        raw = redis_manager.replica.hgetall(_task_key(task_id))
        if not raw:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")

        task = Task.from_redis_hash(raw)
        _record_hit()
        span.set_attribute("cache.hit", True)
        return TaskResponse(data=task, cached=True)


@router.put(
    "/{task_id}",
    response_model=TaskResponse,
    summary="Update a task (partial update)",
)
def update_task(task_id: str, payload: TaskUpdate) -> TaskResponse:
    """
    Partially update task fields (PATCH semantics via PUT).

    Steps:
      1. Load current task from master (ensure consistency post-failover)
      2. Apply non-None fields from payload
      3. Update task hash on master
      4. Update status index sets if status changed
      5. Invalidate list cache
    """
    with tracer.start_as_current_span("task.update") as span:
        span.set_attribute("task.id", task_id)

        master = redis_manager.master

        # Always read from master before a write to avoid stale data post-failover
        raw = master.hgetall(_task_key(task_id))
        if not raw:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")

        task = Task.from_redis_hash(raw)
        old_status = task.status

        # Apply partial updates
        update_data = payload.model_dump(exclude_none=True)
        for field, value in update_data.items():
            setattr(task, field, value)
        task.updated_at = datetime.now(timezone.utc)

        # Persist
        master.hset(_task_key(task_id), mapping=task.to_redis_hash())

        # Update status index sets if status changed
        if "status" in update_data and task.status != old_status:
            master.srem(_status_index_key(old_status), task_id)
            master.sadd(_status_index_key(task.status),  task_id)
            # Adjust per-status analytics counters
            master.decr(f"analytics:status:{old_status.value}")
            master.incr(f"analytics:status:{task.status.value}")

        # Invalidate list cache
        master.delete("tasks:cache:list")

        logger.info("Task updated id=%s", task_id)
        return TaskResponse(data=task, cached=False)


@router.delete(
    "/{task_id}",
    status_code=204,
    summary="Delete a task",
)
def delete_task(task_id: str) -> None:
    """
    Delete a task and clean up all associated index entries.

    Returns 204 No Content on success, 404 if the task does not exist.
    """
    with tracer.start_as_current_span("task.delete") as span:
        span.set_attribute("task.id", task_id)

        master = redis_manager.master

        raw = master.hgetall(_task_key(task_id))
        if not raw:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")

        task = Task.from_redis_hash(raw)

        # Remove data and all index references
        master.delete(_task_key(task_id))
        master.srem("tasks:index",             task_id)
        master.srem(_status_index_key(task.status), task_id)

        # Analytics
        master.incr("analytics:deleted")
        master.decr(f"analytics:status:{task.status.value}")

        # Invalidate list cache
        master.delete("tasks:cache:list")

        # OTel metric
        try:
            from telemetry.setup import app_metrics
            if app_metrics:
                app_metrics.tasks_deleted.add(1)
        except ImportError:
            pass

        logger.info("Task deleted id=%s", task_id)
        # 204 — no response body
