from fastapi import FastAPI
import redis

# OpenTelemetry
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.mount("/", StaticFiles(directory="static", html=True), name="static")

# -------- OpenTelemetry --------
trace.set_tracer_provider(TracerProvider())

otlp_exporter = OTLPSpanExporter(
    endpoint="http://signoz-otel-collector.signoz:4317",
    insecure=True
)

span_processor = BatchSpanProcessor(otlp_exporter)
trace.get_tracer_provider().add_span_processor(span_processor)

FastAPIInstrumentor.instrument_app(app)

# -------- Redis (Cluster entry point service) --------
redis_client = redis.Redis(
    host="redis-cluster",
    port=6379,
    decode_responses=True
)

@app.get("/api")
def get_data():
    # Count visits
    redis_client.incr("visits")
    redis_client.incr("redis_hits")

    visits_count = redis_client.get("visits")
    redis_hits_count = redis_client.get("redis_hits")

    return {
        "message": "🚀 Redis Cluster Demo",
        "visits": visits_count,
        "redis_hits": redis_hits_count
    }