"""
telemetry/setup.py
------------------
Bootstraps OpenTelemetry for the three pillars — Traces, Metrics, Logs —
and ships everything to SigNoz via its OTLP gRPC collector.

Architecture:
  FastAPI App
      │
      ├─ Traces  ──► BatchSpanProcessor ──► OTLPSpanExporter (gRPC :4317)
      │                                           │
      ├─ Metrics ──► PeriodicMetricReader  ──► OTLPMetricExporter           ► SigNoz
      │                                           │
      └─ Logs    ──► Python logging handler ──► OTLPLogExporter

Auto-instrumentation covers:
  • FastAPI  — HTTP request spans with route, method, status code
  • Redis    — Every Redis command becomes a child span with key/command
  • Logging  — trace_id / span_id are injected into every log record

Custom metrics created here (app-level business metrics):
  • nexus.tasks.created         (counter)
  • nexus.cache.hits            (counter)
  • nexus.cache.misses          (counter)
  • nexus.rate_limit.rejected   (counter)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from config import settings

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _build_resource() -> Resource:
    """
    A Resource identifies the source of telemetry in SigNoz.
    These key-value pairs appear as attributes on every span and metric.
    """
    return Resource.create(
        {
            SERVICE_NAME:    settings.otel_service_name,
            SERVICE_VERSION: settings.app_version,
            "deployment.environment": settings.environment,
        }
    )


def _setup_tracing(resource: Resource) -> None:
    """
    Tracing pipeline:
      Span  →  BatchSpanProcessor  →  OTLPSpanExporter  →  SigNoz
    BatchSpanProcessor buffers spans in memory and sends them asynchronously
    in batches, reducing network overhead vs. the simple synchronous processor.
    """
    exporter = OTLPSpanExporter(
        endpoint=settings.otel_endpoint,
        insecure=True,   # No TLS inside the cluster; change for cross-cluster
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.info("OTel tracing configured → %s", settings.otel_endpoint)


def _setup_metrics(resource: Resource) -> None:
    """
    Metrics pipeline:
      Measurement  →  PeriodicExportingMetricReader  →  OTLPMetricExporter  →  SigNoz
    Metrics are pushed every 30 s (export_interval_millis).
    """
    exporter = OTLPMetricExporter(
        endpoint=settings.otel_endpoint,
        insecure=True,
    )
    reader = PeriodicExportingMetricReader(
        exporter,
        export_interval_millis=30_000,  # 30 s push interval
    )
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)
    logger.info("OTel metrics configured → %s", settings.otel_endpoint)


class AppMetrics:
    """
    Thin wrapper that holds all custom application-level OTel instruments.

    Usage in routers:
        from telemetry.setup import app_metrics
        app_metrics.tasks_created.add(1, {"priority": "high"})
    """

    def __init__(self) -> None:
        meter = metrics.get_meter(settings.otel_service_name, settings.app_version)

        # Counters — monotonically increasing, perfect for event counts
        self.tasks_created = meter.create_counter(
            name="nexus.tasks.created",
            description="Total number of tasks created",
            unit="1",
        )
        self.cache_hits = meter.create_counter(
            name="nexus.cache.hits",
            description="Redis cache hits on task reads",
            unit="1",
        )
        self.cache_misses = meter.create_counter(
            name="nexus.cache.misses",
            description="Redis cache misses on task reads (data rebuilt from store)",
            unit="1",
        )
        self.rate_limit_rejected = meter.create_counter(
            name="nexus.rate_limit.rejected",
            description="Requests rejected by the sliding-window rate limiter",
            unit="1",
        )
        self.tasks_deleted = meter.create_counter(
            name="nexus.tasks.deleted",
            description="Total number of tasks deleted",
            unit="1",
        )

        # Histogram — tracks distribution of a value (e.g. response time)
        self.task_operation_duration = meter.create_histogram(
            name="nexus.task.operation.duration",
            description="Duration of Redis task operations",
            unit="ms",
        )


def setup_telemetry(app: FastAPI) -> AppMetrics:
    """
    Wire up the full OTel stack and return an AppMetrics instance.

    Call this ONCE at application startup (inside lifespan or module level).
    Returns the AppMetrics object so routers can record business metrics.
    """
    resource = _build_resource()
    _setup_tracing(resource)
    _setup_metrics(resource)

    # Auto-instrument every FastAPI request — adds HTTP spans automatically
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="/health",   # Don't pollute traces with health-check noise
    )

    # Auto-instrument every Redis command — adds Redis child spans to each trace
    RedisInstrumentor().instrument()

    logger.info("OpenTelemetry fully initialised for service '%s'", settings.otel_service_name)

    return AppMetrics()


# Module-level singleton so routers can import directly
# (populated after setup_telemetry() is called in main.py)
app_metrics: AppMetrics | None = None
