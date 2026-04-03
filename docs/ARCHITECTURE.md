# Nexus API — Architecture & Operations Guide

> **Stack:** FastAPI · Redis Sentinel · Kubernetes · ArgoCD · OpenTelemetry · SigNoz

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Why Redis Sentinel?](#2-why-redis-sentinel)
3. [Redis Sentinel Deep Dive](#3-redis-sentinel-deep-dive)
4. [Read / Write Split](#4-read--write-split)
5. [Why Helm?](#5-why-helm)
6. [OpenTelemetry & SigNoz](#6-opentelemetry--signoz)
7. [GitOps with ArgoCD](#7-gitops-with-argocd)
8. [Rate Limiting](#8-rate-limiting)
9. [Deployment Step-by-Step](#9-deployment-step-by-step)
10. [Monitoring Playbook](#10-monitoring-playbook)
11. [Runbook — Sentinel Failover](#11-runbook--sentinel-failover)
12. [Glossary](#12-glossary)

---

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          Kubernetes Cluster                             │
│  Namespace: fastapi                        Namespace: signoz            │
│                                                                         │
│  ┌──────────────────────────────┐          ┌──────────────────────────┐ │
│  │   LoadBalancer Service       │          │  SigNoz OTel Collector   │ │
│  │   nexus-api :80              │          │  :4317 (gRPC OTLP)       │ │
│  └────────────┬─────────────────┘          └──────────────────────────┘ │
│               │ routes to                          ▲                    │
│  ┌────────────▼──────────────────────────────────  │                    │
│  │           FastAPI Pods (4 replicas)             │ Traces             │
│  │                                                 │ Metrics            │
│  │  nexus-api-xxx-0   nexus-api-xxx-1   ...        │ Logs               │
│  │  ┌─────────────┐   ┌─────────────┐             │                    │
│  │  │  Uvicorn    │   │  Uvicorn    │ ────────────┘                    │
│  │  │  FastAPI    │   │  FastAPI    │                                   │
│  │  │  OTel SDK   │   │  OTel SDK   │                                   │
│  │  └──────┬──────┘   └──────┬──────┘                                   │
│  │         │ Sentinel query  │                                           │
│  └─────────┼─────────────────┼──────────────────────────────────────────┤
│            │                 │                                           │
│  ┌─────────▼─────────────────▼──────────────────────────────────────┐   │
│  │            Redis StatefulSet (Bitnami Helm Chart)                │   │
│  │                                                                  │   │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │   │
│  │  │  redis-node-0   │  │  redis-node-1   │  │  redis-node-2   │  │   │
│  │  │ ┌─────────────┐ │  │ ┌─────────────┐ │  │ ┌─────────────┐ │  │   │
│  │  │ │ Redis :6379 │ │  │ │ Redis :6379 │ │  │ │ Redis :6379 │ │  │   │
│  │  │ │  (MASTER)   │ │  │ │  (REPLICA)  │ │  │ │  (REPLICA)  │ │  │   │
│  │  │ ├─────────────┤ │  │ ├─────────────┤ │  │ ├─────────────┤ │  │   │
│  │  │ │Sentinel:26379│ │  │ │Sentinel:26379│ │  │ │Sentinel:26379│ │  │   │
│  │  │ └─────────────┘ │  │ └─────────────┘ │  │ └─────────────┘ │  │   │
│  │  │ redis_exp :9121 │  │ redis_exp :9121 │  │ redis_exp :9121 │  │   │
│  │  │ PVC: 8Gi        │  │ PVC: 8Gi        │  │ PVC: 8Gi        │  │   │
│  │  └─────────────────┘  └─────────────────┘  └─────────────────┘  │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  Kubernetes Services (created by Bitnami chart)                  │   │
│  │  redis          ClusterIP   :6379, :26379  (all sentinel pods)   │   │
│  │  redis-headless Headless    stable DNS per pod                   │   │
│  │  redis-metrics  ClusterIP   :9121           (redis_exporter)     │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘

GitHub ──push──► GitHub Actions ──docker push──► DockerHub
                                                      │
ArgoCD ──watches──► GitHub repo k8s/ ──applies──► Cluster
```

---

## 2. Why Redis Sentinel?

### The Problem With a Single Redis Pod

Your original deployment had one Redis pod. If that pod is evicted, crashes, or
its node goes down:

- All in-flight Redis operations fail immediately
- The FastAPI app's rate limiter, cache, and session data is inaccessible
- Your app returns 500 errors until Redis is rescheduled and reconnected
- A `Deployment` (not `StatefulSet`) gives the pod a new IP on restart, so the
  direct connection string `redis-master:6379` temporarily points nowhere

**Downtime window:** typically 30–120 seconds per Redis restart.

### Why Not Redis Cluster?

| Feature | Redis Sentinel | Redis Cluster |
|---|---|---|
| Min nodes | 3 (1M + 2R) | 6 (3M + 3R) |
| Client changes | Sentinel-aware client | Cluster-aware client (MOVED redirects) |
| Horizontal writes | No (1 master) | Yes (sharded) |
| Use case | HA for < 100 GB, single-region | Petabyte-scale, multi-region |
| Ops complexity | Low | High |

For a FastAPI app using Redis as a cache and rate-limiter, you don't need
horizontal write scaling. Sentinel gives you **automatic failover** with
minimal complexity.

### Why Not Active-Active (Redis Enterprise)?

Active-Active replication requires Redis Enterprise (commercial) or a managed
service. It's the right answer for multi-region writes, but overkill for a
single-cluster deployment.

---

## 3. Redis Sentinel Deep Dive

### What Sentinel Is

Redis Sentinel is a distributed supervisor process. It runs alongside Redis and
provides three services:

1. **Monitoring** — Sentinel pings the master and replicas periodically
2. **Notification** — Sentinel can alert via pub/sub when something changes
3. **Automatic failover** — Sentinel promotes a replica to master when the
   master is unreachable for longer than `down-after-milliseconds`

### How Failover Works (Step by Step)

```
Normal operation:
  node-0 [MASTER] ──replication──► node-1 [REPLICA]
                                ──replication──► node-2 [REPLICA]
  Sentinel-0 ──monitor──► node-0, node-1, node-2
  Sentinel-1 ──monitor──► node-0, node-1, node-2
  Sentinel-2 ──monitor──► node-0, node-1, node-2

Step 1: node-0 (master) crashes
  Sentinel-0: can't reach node-0 → marks as "subjectively down" (SDOWN)
  Sentinel-1: can't reach node-0 → marks as SDOWN
  Sentinel-2: can't reach node-0 → marks as SDOWN

Step 2: Quorum reached (2 of 3 agree)
  Sentinels communicate: "We all see node-0 as down"
  node-0 is declared "objectively down" (ODOWN)

Step 3: Leader election among Sentinels
  Sentinels elect a "Sentinel leader" via a Raft-like vote

Step 4: Promotion
  Sentinel leader picks the replica with the smallest replication lag
  (highest replication offset) → promotes node-1 to MASTER

Step 5: Reconfiguration
  node-2 is redirected: REPLICAOF node-1 6379
  Sentinels update their own config: new master is node-1

Step 6: Client reconnects
  FastAPI Sentinel client calls: sentinel.master_for("mymaster")
  Sentinel responds: "master is now redis-node-1.redis-headless:6379"
  App resumes writes → total downtime ≈ 5–15 seconds
```

### Quorum Math

| Sentinels | Quorum | Can lose | Notes |
|---|---|---|---|
| 3 | 2 | 1 sentinel | Our setup |
| 5 | 3 | 2 sentinels | Higher fault tolerance |
| 1 | 1 | 0 | Not HA — one point of failure |

With quorum=2 and 3 sentinels: if one sentinel pod is being rescheduled,
the remaining 2 can still agree on a failover. This is the minimum viable HA
configuration.

---

## 4. Read / Write Split

```
FastAPI App
    │
    ├── WRITE operations (POST, PUT, DELETE, INCR)
    │       │
    │       └──► redis_manager.master
    │                   │
    │                   └──► Sentinel asks: "who is master?"
    │                               │
    │                               └──► redis-node-0:6379 (MASTER)
    │
    └── READ operations (GET, HGETALL, SMEMBERS)
            │
            └──► redis_manager.replica
                        │
                        └──► Sentinel asks: "give me a slave"
                                    │
                                    └──► redis-node-1:6379 or
                                         redis-node-2:6379 (REPLICA)
```

**Why split reads to replicas?**

- Replicas handle reads, freeing the master for writes and replication
- If the app has bursty read traffic (e.g., many users loading the task list),
  the replica absorbs that load
- The master focuses on: receiving writes, streaming replication, and handling
  Sentinel heartbeats

**Trade-off: Replication Lag**

Replication is asynchronous. A write to the master arrives at the replica
within a few milliseconds under normal conditions. This means:

- A write immediately followed by a read may return stale data from the replica
- For the task API: `create_task()` reads from master after writing (confirmed
  data); subsequent `list_tasks()` reads from replica (may be 1 write behind)
- In practice this is invisible to users

---

## 5. Why Helm?

### The Alternative: Raw YAML

Deploying Redis with Sentinel manually requires:

```
• StatefulSet with headless service
• Init container to configure redis.conf and sentinel.conf per pod
• ConfigMap with templated configuration (different for master vs. replica)
• Multiple Services (headless, metrics, sentinel)
• PodDisruptionBudget
• Pod anti-affinity rules
• Secret volume mounts
• Readiness/liveness probe scripts
• redis_exporter sidecar container
```

That's 350+ lines of YAML that you maintain forever.  Every Redis version
upgrade means manually diffing changelogs and patching your YAML.

### Helm Advantages

| Concern | Manual YAML | Helm (Bitnami) |
|---|---|---|
| Initial setup | Write 350+ lines | One `values.yaml` + `helm install` |
| Version upgrade | Manual diff + test | `helm upgrade --set image.tag=x.y.z` |
| Rollback | `git revert` + `kubectl apply` | `helm rollback redis 1` |
| Community bugs | Your problem | Fixed upstream by Bitnami |
| Sentinel config | 100+ lines init script | `sentinel.enabled: true` |
| PVC provisioning | Manual | Automatic |
| Pod anti-affinity | Manual | `podAntiAffinityPreset: soft` |

### How Helm Works (30-second explanation)

```
helm install redis bitnami/redis --values k8s/redis/values.yaml -n fastapi
         │        │       │             │
         │        │       │             └── Your customisations
         │        │       └── Chart from Bitnami's repo
         │        └── Release name (used in resource names: redis-node-0, redis-headless, …)
         └── Helm CLI command
```

Helm renders the chart templates with your values and applies them as a
**release** — a named, versioned set of Kubernetes resources. You can
upgrade, rollback, and inspect the release history with simple commands.

---

## 6. OpenTelemetry & SigNoz

### The Three Pillars

```
┌─────────────────────────────────────────────────────────────────┐
│  FastAPI Pod                                                    │
│                                                                 │
│  HTTP Request → FastAPIInstrumentor                            │
│                     │                                          │
│                     ├── Span: "GET /api/v1/tasks"              │
│                     │     ├── Span: "task.list"                │
│                     │     │     ├── Span: "redis SMEMBERS"     │
│                     │     │     └── Span: "redis HGETALL x10"  │
│                     │     └── attributes: status=200, cached=true
│                     │                                          │
│                     ├── Metric: nexus.cache.hits += 1          │
│                     └── Log: "INFO | task.list | 10 tasks"     │
│                             (with trace_id injected)           │
│                                                                 │
│  BatchSpanProcessor ──────────────────────────────────────────► │
│  PeriodicMetricReader ─────────────────────────────────────────► SigNoz
│  LoggingHandler ───────────────────────────────────────────────► :4317
└─────────────────────────────────────────────────────────────────┘
```

### Traces

Every HTTP request creates a **trace** — a tree of spans showing exactly what
happened and how long each step took.

Example trace for `GET /api/v1/tasks`:
```
[0ms]  GET /api/v1/tasks                                   45ms total
 [1ms]   task.list                                         43ms
  [2ms]   redis SMEMBERS tasks:index                        3ms
  [5ms]   redis HGETALL task:abc-123                        1ms
  [6ms]   redis HGETALL task:def-456                        1ms
  ...
  [44ms]  redis SETEX tasks:cache:list                      1ms   (cache warm)
```

In SigNoz: **Traces → Search → service=nexus-api** shows all traces. Click any
trace to see the full span tree with Redis commands highlighted.

### Metrics

Custom application metrics pushed every 30 seconds to SigNoz:

| Metric | Type | What it measures |
|---|---|---|
| `nexus.tasks.created` | Counter | Tasks created (by priority) |
| `nexus.tasks.deleted` | Counter | Tasks deleted |
| `nexus.cache.hits` | Counter | Redis cache hits |
| `nexus.cache.misses` | Counter | Redis cache misses |
| `nexus.rate_limit.rejected` | Counter | Rate-limited requests |
| `nexus.task.operation.duration` | Histogram | Redis op latency (ms) |

In SigNoz: **Metrics → nexus.*** to build dashboards and set alerts.

### Logs

Python logs are emitted to stdout in structured format. The
`LoggingInstrumentor` injects `trace_id` and `span_id` into every log record,
so you can correlate a log line with its trace in SigNoz.

```
2024-01-15 10:23:45 | INFO     | routers.tasks | Task created id=abc trace_id=a1b2c3d4
```

In SigNoz: **Logs** → filter by `service.name = nexus-api` → click a log line
→ "View Trace" to jump directly to the trace.

### SigNoz OTel Collector — Redis Metrics Scraping

Add this to your SigNoz OTel Collector ConfigMap to scrape redis_exporter:

```yaml
# kubectl edit cm otel-collector-config -n signoz
receivers:
  prometheus:
    config:
      scrape_configs:
        - job_name: redis-sentinel
          scrape_interval: 30s
          static_configs:
            - targets:
                - redis-metrics.fastapi.svc.cluster.local:9121

exporters:
  otlp:
    endpoint: localhost:4317   # Self-reference within the collector pod
    tls:
      insecure: true

service:
  pipelines:
    metrics/redis:
      receivers:  [prometheus]
      exporters:  [otlp]
```

### Key Redis Metrics to Alert On (SigNoz Alerts)

| Metric | Alert condition | Meaning |
|---|---|---|
| `redis_up` | == 0 | Redis node is down |
| `redis_connected_clients` | > 500 | Connection pool pressure |
| `redis_memory_used_bytes` | > 80% of maxmemory | Near eviction threshold |
| `redis_keyspace_hits_total` / total | Hit rate < 70% | Cache is underperforming |
| `redis_replication_offset` (master vs replica) | Gap > 10000 bytes | Replication lag |
| `redis_sentinel_masters{status!="ok"}` | > 0 | Sentinel sees master as unhealthy |

---

## 7. GitOps with ArgoCD

```
Developer
    │
    │  git push origin main
    │
    ▼
GitHub
    │
    ├── GitHub Actions: build Docker image → push to DockerHub
    │
    └── (Git repo updated)
            │
            ▼ (ArgoCD polls every 3 minutes OR webhook)
        ArgoCD
            │
            ├── Compares k8s/ in Git with live cluster state
            ├── Detects: deployment.yaml image tag changed
            └── kubectl apply → Rolling update begins

Cluster (live state)
    ├── Old pod: nexus-api-xxx-0 (image: v1)
    ├── New pod: nexus-api-yyy-0 (image: v2) ← starts first
    ├── Old pod removed after new pod passes readiness probe
    └── Repeat for each replica (zero downtime)
```

**selfHeal: true** means if someone runs `kubectl edit deployment nexus-api`
to manually change a replica count, ArgoCD will revert it to match Git within
3 minutes. Git is the single source of truth.

---

## 8. Rate Limiting

```
Request arrives at FastAPI pod
    │
    ├── Path is /health* → skip rate limit → proceed
    │
    └── Extract client IP (X-Forwarded-For or remote address)
            │
            ▼
        Redis INCR "ratelimit:{ip}"    ← atomic, thread-safe
            │
            ├── Result == 1 → first request in window
            │       └── EXPIRE "ratelimit:{ip}" 60    ← arm the window
            │
            ├── Result ≤ 100 → allow request
            │       └── Add X-RateLimit-* headers to response
            │
            └── Result > 100 → return 429 Too Many Requests
                    └── Retry-After: 60
```

**Why Redis for rate limiting (not in-process)?**

With 4 FastAPI pod replicas, an in-process counter (Python dict) would give
each pod its own budget. A client could send 100 requests × 4 pods = 400
requests before being limited. Redis provides a shared, atomic counter across
all pods.

**Fail open:** If Redis is temporarily unavailable (e.g., Sentinel failover in
progress), the rate limiter logs a warning and allows the request through.
Brief Redis downtime does not block all user traffic.

---

## 9. Deployment Step-by-Step

### Prerequisites

```bash
# Verify tools
kubectl version
helm version        # >= 3.x
kubectl get nodes   # Cluster is reachable
kubectl get storageclass  # At least one StorageClass exists
```

### Step 1 — Create Namespace

```bash
kubectl apply -f k8s/namespace.yaml
```

### Step 2 — Create Redis Secret

```bash
# Generate a strong password
openssl rand -base64 32

kubectl create secret generic redis-secret \
  --from-literal=redis-password='<PASTE_STRONG_PASSWORD_HERE>' \
  -n fastapi

# Verify
kubectl get secret redis-secret -n fastapi
```

> **Production note:** For full GitOps (secrets in Git), use one of:
> - **Sealed Secrets** — encrypt secrets client-side, commit ciphertext to Git
> - **External Secrets Operator** — pull from AWS Secrets Manager / Vault at runtime

### Step 3 — Install Redis via Helm

```bash
# Add Bitnami chart repository
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update

# Install — name "redis" determines service names (redis-headless, redis-node-*)
helm install redis bitnami/redis \
  --namespace fastapi \
  --values k8s/redis/values.yaml \
  --version 20.3.0   # Pin version for reproducibility

# Watch pods come up
kubectl get pods -n fastapi -l app.kubernetes.io/name=redis -w
# Expected: redis-node-0 (Running), redis-node-1 (Running), redis-node-2 (Running)
```

### Step 4 — Verify Sentinel

```bash
# Exec into a Redis pod and query the sentinel
kubectl exec -it redis-node-0 -n fastapi -c sentinel -- \
  redis-cli -p 26379 -a '<YOUR_PASSWORD>' sentinel masters

# Should show: name=mymaster, status=ok, slaves=2, sentinels=3
```

### Step 5 — Deploy FastAPI via ArgoCD

```bash
# Install ArgoCD Application (ArgoCD must already be running in your cluster)
kubectl apply -f argocd/application.yaml

# Watch the sync
kubectl get application nexus-api -n argocd -w
# STATUS: Synced | HEALTH: Healthy
```

### Step 6 — Verify the App

```bash
# Get the LoadBalancer external IP
kubectl get svc nexus-api -n fastapi
# Wait for EXTERNAL-IP to be assigned (1-2 minutes on cloud)

# Test the API
curl http://<EXTERNAL-IP>/health
curl http://<EXTERNAL-IP>/health/redis
curl http://<EXTERNAL-IP>/api/v1/tasks
```

### Step 7 — Configure SigNoz Scraping

```bash
# Edit the OTel Collector config to add redis_exporter scraping
kubectl edit cm otel-collector-config -n signoz
# Add the prometheus receiver block from Section 6 above

# Restart the collector to pick up the change
kubectl rollout restart deployment otel-collector -n signoz
```

---

## 10. Monitoring Playbook

### SigNoz Dashboard Setup

1. Open SigNoz UI → **Dashboards** → **New Dashboard**
2. Create panels:

| Panel | Query | Type |
|---|---|---|
| Task Creation Rate | `rate(nexus.tasks.created[5m])` | Graph |
| Cache Hit Rate | `nexus.cache.hits / (nexus.cache.hits + nexus.cache.misses) * 100` | Gauge |
| Rate Limited Requests | `nexus.rate_limit.rejected` | Counter |
| Redis Memory Usage | `redis_memory_used_bytes / redis_memory_max_bytes * 100` | Gauge |
| Redis Connected Clients | `redis_connected_clients` | Graph |
| p99 API Latency | `histogram_quantile(0.99, http.server.duration)` | Graph |
| Replication Lag | `redis_replication_offset{role="master"} - redis_replication_offset{role="slave"}` | Graph |

### Alerts to Configure

```yaml
# High error rate
alert: NexusHighErrorRate
expr: rate(http_server_duration_count{status_code=~"5.."}[5m]) > 0.05
severity: critical

# Redis master down
alert: RedisMasterDown
expr: redis_up{role="master"} == 0
severity: critical
for: 1m

# Cache hit rate degraded
alert: LowCacheHitRate
expr: nexus.cache.hits / (nexus.cache.hits + nexus.cache.misses) < 0.6
severity: warning
for: 5m

# High memory usage
alert: RedisHighMemory
expr: redis_memory_used_bytes / redis_memory_max_bytes > 0.85
severity: warning
```

---

## 11. Runbook — Sentinel Failover

### Simulating a Failover (Test in Staging)

```bash
# Delete the master pod — Sentinel will elect a new master
kubectl delete pod redis-node-0 -n fastapi

# Watch the failover in Sentinel logs
kubectl logs redis-node-1 -n fastapi -c sentinel -f | grep -i "failover\|promoted\|master"

# Check the new master
kubectl exec -it redis-node-1 -n fastapi -c redis -- \
  redis-cli -a '<password>' info replication | grep role
# role:master  ← node-1 is now the master
```

### Expected Timeline

```
T+0s    redis-node-0 pod deleted
T+5s    Sentinels mark master as SDOWN (down-after-milliseconds: 5000)
T+5s    Quorum reached → ODOWN
T+6s    Sentinel leader elected
T+7s    Sentinel promotes node-1 to master
T+8s    node-2 repoints REPLICAOF to node-1
T+10s   FastAPI clients detect new master on next Redis call
T+30s   K8s reschedules redis-node-0 as a new replica
T+45s   redis-node-0 syncs from node-1 → full HA restored
```

### If Failover Does Not Happen

Check:
```bash
# Are 2+ sentinels running?
kubectl get pods -n fastapi -l app.kubernetes.io/name=redis

# Is quorum set correctly?
kubectl exec redis-node-1 -n fastapi -c sentinel -- \
  redis-cli -p 26379 -a '<pass>' sentinel masters | grep quorum

# Check sentinel logs for errors
kubectl logs redis-node-1 -n fastapi -c sentinel --tail=50
```

---

## 12. Glossary

| Term | Definition |
|---|---|
| **Sentinel** | Redis process that monitors masters/replicas and performs automatic failover |
| **Quorum** | Minimum number of Sentinels that must agree a master is down before failover |
| **SDOWN** | Subjectively Down — one Sentinel can't reach the master |
| **ODOWN** | Objectively Down — quorum of Sentinels agree master is unreachable |
| **Replication Offset** | Byte position in the replication stream; used to pick the "best" replica to promote |
| **StatefulSet** | K8s workload type for stateful apps; provides stable network identity and ordered pod names |
| **PVC** | PersistentVolumeClaim — reserved disk storage that outlives pod restarts |
| **Helm Release** | A named, versioned deployment of a Helm chart to a cluster |
| **Headless Service** | K8s Service with `clusterIP: None`; returns pod DNS entries instead of a VIP |
| **Read/Write Split** | Routing writes to master and reads to replicas to distribute load |
| **OTLP** | OpenTelemetry Protocol — wire format for sending telemetry data to a collector |
| **Span** | Single unit of work in a trace (e.g., "Redis HGETALL") with start time, duration, attributes |
| **Trace** | A tree of spans representing the full lifecycle of a request |
| **Meter** | OTel object for recording numeric measurements (counters, histograms, gauges) |
| **ArgoCD** | GitOps controller that keeps cluster state in sync with a Git repository |
| **GitOps** | Using Git as the single source of truth for infrastructure and application state |
| **Sliding Window** | Rate-limiting algorithm that resets the counter every N seconds per client |
| **Anti-affinity** | K8s scheduling rule that spreads pods across different nodes |
| **PodDisruptionBudget** | K8s policy that limits simultaneous voluntary disruptions (e.g., node drain) |
