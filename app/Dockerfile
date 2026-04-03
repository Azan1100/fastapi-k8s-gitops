# ────────────────────────────────────────────────────────────────────────────
# Multi-stage Dockerfile for Nexus FastAPI application
#
# Stage 1 (builder): install Python dependencies into a venv — this layer
#   is cached by Docker and only rebuilt when requirements.txt changes.
#
# Stage 2 (runtime): copy only the venv + app source into a slim image.
#   The final image has no pip, no build tools — smaller attack surface.
# ────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Dependency Builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies (needed for hiredis C-extension compilation)
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# Copy only requirements first — Docker layer cache: requirements rarely change
COPY requirements.txt .

# Install all dependencies into an isolated venv
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# ── Stage 2: Runtime Image ────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Non-root user for security — K8s should also set securityContext.runAsNonRoot
RUN useradd --create-home --shell /bin/bash nexus

WORKDIR /app

# Copy the pre-built venv from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy application source
COPY --chown=nexus:nexus . .

# Activate the venv for all subsequent commands
ENV PATH="/opt/venv/bin:$PATH"

# Unbuffered output — ensures logs appear in kubectl logs without delay
ENV PYTHONUNBUFFERED=1

# Don't write .pyc files — not needed in containers
ENV PYTHONDONTWRITEBYTECODE=1

USER nexus

EXPOSE 8000

# Kubernetes health check — uses httpx (already in requirements)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=3)"

# 4 Uvicorn workers — tune to (2 × CPU cores) + 1 in production
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
