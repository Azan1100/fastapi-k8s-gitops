from fastapi import FastAPI
import logging
import redis

# OpenTelemetry
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from fastapi.staticfiles import StaticFiles

app = FastAPI()
logger = logging.getLogger(__name__)

# -------- OpenTelemetry --------
trace.set_tracer_provider(TracerProvider())

otlp_exporter = OTLPSpanExporter(
    endpoint="http://signoz-otel-collector.signoz:4317",
    insecure=True
)

span_processor = BatchSpanProcessor(otlp_exporter)
trace.get_tracer_provider().add_span_processor(span_processor)

FastAPIInstrumentor.instrument_app(app)

# -------- Redis Setup --------
try:
    redis_client = redis.Redis(host="redis-master.fastapi.svc.cluster.local", port=6379, decode_responses=True)
    # Test connection
    redis_client.ping()
    logger.info("✅ Connected to Redis Master")
except Exception as e:
    logger.error(f"❌ Redis connection failed: {e}")
    redis_client = None

@app.get("/api")
def get_data():
    if not redis_client:
        return {"error": "Redis unavailable"}, 503
    
    try:
        redis_client.incr("visits")
        redis_client.incr("redis_hits")

        visits_count = redis_client.get("visits")
        redis_hits_count = redis_client.get("redis_hits")

        return {
            "message": "🚀 Redis Simple HA Demo",
            "visits": visits_count,
            "redis_hits": redis_hits_count
        }
    except Exception as e:
        logger.error(f"Redis error: {e}")
        return {"error": "Redis operation failed"}, 500

@app.get("/health")
def health():
    return {"status": "ok"}

# Mount static files AFTER API routes
app.mount("/", StaticFiles(directory="static", html=True), name="static")