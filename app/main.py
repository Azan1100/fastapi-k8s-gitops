from fastapi import FastAPI

# OpenTelemetry imports
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

app = FastAPI()

# 1. Set tracer provider
trace.set_tracer_provider(TracerProvider())

# 2. Configure exporter (THIS SENDS DATA TO SIGNOZ)
otlp_exporter = OTLPSpanExporter(
    endpoint="http://signoz-otel-collector.signoz:4317",
    insecure=True
)

# 3. Add span processor
span_processor = BatchSpanProcessor(otlp_exporter)
trace.get_tracer_provider().add_span_processor(span_processor)

# 4. Instrument FastAPI
FastAPIInstrumentor.instrument_app(app)

@app.get("/")
def root():
    return {"message": "Tracing works 🚀"}
