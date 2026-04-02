from fastapi import FastAPI
import logging
from rediscluster import RedisCluster

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

# -------- Redis Cluster --------
startup_nodes = [
    {"host": "redis-0.redis-headless.fastapi.svc.cluster.local", "port": 6379},
    {"host": "redis-1.redis-headless.fastapi.svc.cluster.local", "port": 6379},
    {"host": "redis-2.redis-headless.fastapi.svc.cluster.local", "port": 6379},
]

try:
    redis_cluster = RedisCluster(startup_nodes=startup_nodes, decode_responses=True, skip_full_coverage_check=True)
    logger.info("✅ Connected to Redis Cluster")
except Exception as e:
    logger.error(f"❌ Redis Cluster connection failed: {e}")
    redis_cluster = None

@app.get("/api")
def get_data():
    if not redis_cluster:
        return {"error": "Redis cluster unavailable"}, 503
    
    try:
        redis_cluster.incr("visits")
        redis_cluster.incr("redis_hits")

        visits_count = redis_cluster.get("visits")
        redis_hits_count = redis_cluster.get("redis_hits")

        return {
            "message": "🚀 Redis Cluster Demo",
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