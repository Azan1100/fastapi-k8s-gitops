"""
config.py
---------
Centralised configuration via environment variables using Pydantic Settings.
All values have sensible defaults for local dev; production values are injected
via Kubernetes Secrets / ConfigMaps in deployment.yaml.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Application ──────────────────────────────────────────────────────────
    app_name: str = "Nexus API"
    app_version: str = "2.0.0"
    environment: str = "production"
    debug: bool = False

    # ── Redis Sentinel ────────────────────────────────────────────────────────
    # Comma-separated list of sentinel host:port pairs.
    # Bitnami chart creates a headless service so we can address each pod's
    # sentinel sidecar individually — giving the Sentinel client true
    # multi-sentinel awareness.
    redis_sentinel_hosts: str = "redis.fastapi.svc.cluster.local:26379"

    # The sentinel "master group" name — must match sentinel.masterSet in values.yaml
    redis_master_set: str = "mymaster"

    # Password for both Redis nodes AND Sentinel (Bitnami sets the same password)
    redis_password: str = ""

    # Redis logical database index (0-15)
    redis_db: int = 0

    # Seconds before a Redis socket operation times out
    redis_socket_timeout: float = 1.0

    # ── Cache ─────────────────────────────────────────────────────────────────
    # How long task data lives in the Redis cache (seconds)
    cache_ttl_seconds: int = 300  # 5 minutes

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    # Sliding window rate limit: max requests per window per client IP
    rate_limit_max_requests: int = 100
    rate_limit_window_seconds: int = 60

    # ── OpenTelemetry / SigNoz ────────────────────────────────────────────────
    # gRPC endpoint of the SigNoz OTel collector (insecure/no TLS inside cluster)
    otel_endpoint: str = "http://signoz-otel-collector.signoz:4317"
    otel_service_name: str = "nexus-api"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


# Module-level singleton — import this everywhere instead of re-instantiating
settings = Settings()
