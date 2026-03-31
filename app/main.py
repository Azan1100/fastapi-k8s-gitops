from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

app = FastAPI()

FastAPIInstrumentor.instrument_app(app)

@app.get("/")
def root():
    return {"message": "Hello from K8s + GitOps 🚀"}
