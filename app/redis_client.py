"""
redis_client.py
---------------
Sentinel-aware Redis connection manager.

Why Sentinel instead of a direct connection?
  A direct `Redis(host="redis-master", port=6379)` breaks the moment the
  master pod dies — your app gets connection errors until you manually update
  the host.  With Sentinel the client asks "who is the current master?" on
  every connection checkout, so failover is transparent to the application.

Read / Write split:
  - Writes  → master_for()  : always the elected master
  - Reads   → slave_for()   : any healthy replica (reduces master load)

The Bitnami Redis Helm chart with sentinel.enabled=true runs a Sentinel
sidecar on port 26379 inside every Redis pod.  The headless K8s service
gives us stable DNS names for each pod so we can register all three sentinels
with the library.
"""

from __future__ import annotations

import logging
from functools import cached_property

from redis import Redis
from redis.sentinel import Sentinel

from config import settings

logger = logging.getLogger(__name__)


def _parse_sentinel_hosts(raw: str) -> list[tuple[str, int]]:
    """
    Parse a comma-separated string of host:port pairs.

    Example input:  "redis-node-0.redis-headless.fastapi:26379,redis-node-1..."
    Returns:        [("redis-node-0.redis-headless.fastapi", 26379), ...]
    """
    hosts: list[tuple[str, int]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        host, _, port_str = entry.rpartition(":")
        hosts.append((host, int(port_str)))
    return hosts


class RedisManager:
    """
    Singleton wrapper around redis-py's Sentinel client.

    Usage (in route handlers):
        from redis_client import redis_manager

        # Write
        redis_manager.master.set("key", "value")

        # Read (load-balanced across replicas)
        value = redis_manager.replica.get("key")
    """

    def __init__(self) -> None:
        self._sentinel: Sentinel | None = None

    def _get_sentinel(self) -> Sentinel:
        """
        Lazily initialise the Sentinel client.

        Lazy init means the app starts even if Redis is temporarily unavailable
        during a rolling deploy — connections are only attempted on first use.
        """
        if self._sentinel is None:
            hosts = _parse_sentinel_hosts(settings.redis_sentinel_hosts)
            logger.info("Connecting to Redis Sentinels: %s", hosts)

            self._sentinel = Sentinel(
                sentinels=hosts,
                socket_timeout=settings.redis_socket_timeout,
                # Auth for the Sentinel process itself (Bitnami sets the same
                # password on both Redis and Sentinel when auth.enabled=true)
                sentinel_kwargs={"password": settings.redis_password},
            )
        return self._sentinel

    @property
    def master(self) -> Redis:
        """
        Return a Redis client pointing at the current master node.

        Use this for all WRITE operations (SET, INCR, DEL, HSET, …).
        Sentinel re-resolves the master on every call, so failover is seamless.
        """
        return self._get_sentinel().master_for(
            settings.redis_master_set,
            socket_timeout=settings.redis_socket_timeout,
            password=settings.redis_password,
            db=settings.redis_db,
            decode_responses=True,   # Return str, not bytes
        )

    @property
    def replica(self) -> Redis:
        """
        Return a Redis client pointing at a healthy replica node.

        Use this for READ operations (GET, HGETALL, SMEMBERS, …) to offload
        the master and improve read throughput.  Falls back to master if no
        replica is available.
        """
        return self._get_sentinel().slave_for(
            settings.redis_master_set,
            socket_timeout=settings.redis_socket_timeout,
            password=settings.redis_password,
            db=settings.redis_db,
            decode_responses=True,
        )

    def health_check(self) -> dict[str, str]:
        """
        Ping both master and one replica; return a status dict.

        Called by GET /health so the load balancer / ArgoCD can confirm
        Redis connectivity independently of app logic.
        """
        result: dict[str, str] = {}
        try:
            self.master.ping()
            result["master"] = "ok"
        except Exception as exc:
            logger.error("Redis master health check failed: %s", exc)
            result["master"] = f"error: {exc}"

        try:
            self.replica.ping()
            result["replica"] = "ok"
        except Exception as exc:
            # Replica failure is degraded, not fatal — reads can fall back
            logger.warning("Redis replica health check failed: %s", exc)
            result["replica"] = f"error: {exc}"

        return result


# Module-level singleton — import this in routers and middleware
redis_manager = RedisManager()
