"""
models.py
---------
Pydantic v2 models for the Nexus Task Management API.

Covers request bodies, response envelopes, and internal data shapes.
Redis stores tasks as hash maps; these models handle serialisation /
deserialisation to and from flat string dictionaries.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


# ── Enumerations ──────────────────────────────────────────────────────────────

class Priority(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class Status(str, Enum):
    TODO        = "todo"
    IN_PROGRESS = "in_progress"
    REVIEW      = "review"
    DONE        = "done"


# ── Core Task Models ──────────────────────────────────────────────────────────

class TaskCreate(BaseModel):
    """Payload required to create a new task."""
    title:       str            = Field(..., min_length=1, max_length=200,
                                        description="Short, human-readable task title")
    description: Optional[str] = Field(None, max_length=2000)
    priority:    Priority       = Priority.MEDIUM
    status:      Status         = Status.TODO
    tags:        list[str]      = Field(default_factory=list,
                                        description="Free-form labels for filtering")
    assignee:    Optional[str]  = Field(None, max_length=100)

    @field_validator("tags", mode="before")
    @classmethod
    def deduplicate_tags(cls, v: list[str]) -> list[str]:
        """Silently remove duplicate tags while preserving insertion order."""
        seen: set[str] = set()
        return [t for t in v if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]


class TaskUpdate(BaseModel):
    """All fields optional — only supplied fields are updated (PATCH semantics)."""
    title:       Optional[str]      = Field(None, min_length=1, max_length=200)
    description: Optional[str]      = Field(None, max_length=2000)
    priority:    Optional[Priority] = None
    status:      Optional[Status]   = None
    tags:        Optional[list[str]]= None
    assignee:    Optional[str]      = Field(None, max_length=100)


class Task(BaseModel):
    """Full task representation returned to the client."""
    id:          str
    title:       str
    description: Optional[str]
    priority:    Priority
    status:      Status
    tags:        list[str]
    assignee:    Optional[str]
    created_at:  datetime
    updated_at:  datetime

    # ── Redis serialisation helpers ───────────────────────────────────────────

    def to_redis_hash(self) -> dict[str, str]:
        """Flatten to a string-valued dict suitable for Redis HSET."""
        return {
            "id":          self.id,
            "title":       self.title,
            "description": self.description or "",
            "priority":    self.priority.value,
            "status":      self.status.value,
            "tags":        json.dumps(self.tags),
            "assignee":    self.assignee or "",
            "created_at":  self.created_at.isoformat(),
            "updated_at":  self.updated_at.isoformat(),
        }

    @classmethod
    def from_redis_hash(cls, data: dict[str, Any]) -> Task:
        """Re-hydrate a Task from the flat string dict stored in Redis."""
        return cls(
            id          = data["id"],
            title       = data["title"],
            description = data.get("description") or None,
            priority    = Priority(data["priority"]),
            status      = Status(data["status"]),
            tags        = json.loads(data.get("tags", "[]")),
            assignee    = data.get("assignee") or None,
            created_at  = datetime.fromisoformat(data["created_at"]),
            updated_at  = datetime.fromisoformat(data["updated_at"]),
        )

    @classmethod
    def new(cls, payload: TaskCreate) -> Task:
        """Construct a brand-new Task from a creation payload."""
        now = datetime.now(timezone.utc)
        return cls(
            id          = str(uuid4()),
            title       = payload.title,
            description = payload.description,
            priority    = payload.priority,
            status      = payload.status,
            tags        = payload.tags,
            assignee    = payload.assignee,
            created_at  = now,
            updated_at  = now,
        )


# ── Response Envelopes ────────────────────────────────────────────────────────

class TaskResponse(BaseModel):
    """Single-task response; `cached` indicates whether data came from Redis cache."""
    data:   Task
    cached: bool = False


class TaskListResponse(BaseModel):
    """Paginated task list response."""
    data:   list[Task]
    total:  int
    cached: bool = False


class AnalyticsSummary(BaseModel):
    """Aggregated stats surfaced by the /analytics/summary endpoint."""
    total_tasks:       int
    by_status:         dict[str, int]
    by_priority:       dict[str, int]
    cache_hits:        int
    cache_misses:      int
    cache_hit_rate_pct: float
    rate_limited_requests: int


class HealthResponse(BaseModel):
    """Health-check envelope."""
    status:    str               # "ok" | "degraded" | "unhealthy"
    redis:     dict[str, str]    # {"master": "ok", "replica": "ok"} or {"error": "..."}
    version:   str
    timestamp: datetime
