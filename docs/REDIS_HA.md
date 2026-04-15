# Redis High Availability — Architecture & Operations Reference

**Project:** Nexus API — FastAPI task management service  
**Stack:** Kubernetes (Minikube local / production-ready) · Bitnami Redis Helm Chart · ArgoCD GitOps  
**Last updated:** 2026-04-15

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [How Sentinel Works](#3-how-sentinel-works)
4. [Components In Depth](#4-components-in-depth)
5. [Configuration Reference](#5-configuration-reference)
6. [Deployment Guide](#6-deployment-guide)
7. [Command Handbook — Health, Monitor & Debug](#7-command-handbook--health-monitor--debug)
8. [Failover Timeline](#8-failover-timeline)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Overview

Before this change, Redis ran as a **single pod**. If that pod died, the entire application went down — no reads, no writes, no recovery without manual intervention.

This document describes the fault-tolerant Redis setup that replaced it: a three-node cluster with automatic leader election, deployed on Kubernetes via the Bitnami Redis Helm chart, managed by ArgoCD.

**What you get:**

| Capability | Detail |
|---|---|
| Automatic failover | New master elected in ~4 seconds — no human action needed |
| Near-zero downtime | App retries with backoff during election; requests complete with brief latency instead of failing |
| Read/write split | Writes to master, reads from replicas (lower master load) |
| Data durability | Replicas hold a copy; AOF persistence on disk |
| GitOps deployment | ArgoCD — Git is the single source of truth |

---

## 2. Architecture

```
                        ┌───────────────────────┐
                        │      Nexus API         │
                        │      (4 pods)          │
                        │  redis_client.py       │
                        │  asks Sentinel:        │
                        │  "who is master?"      │
                        └──────────┬─────────────┘
                                   │
                   ┌───────────────┼───────────────┐
                   │ WRITES        │               │ READS
                   ▼               │               ▼
          ┌────────────────┐       │    ┌─────────────────────┐
          │  redis-node-X  │       │    │  redis-node-Y / Z   │
          │    MASTER      │───────┼───▶│    REPLICAS         │
          │  port 6379     │  rep- │    │  port 6379          │
          └────────────────┘  lic- │    └─────────────────────┘
                 │             ates│
          ┌──────┴──────┐         │
          │  Sentinel   │◄────────┘
          │  port 26379 │   sidecar on EVERY pod
          └─────────────┘   watches master 24/7
```

**3 Redis pods** — `redis-node-0`, `redis-node-1`, `redis-node-2`  
Each pod runs **two containers**:

| Container | Port | Role |
|---|---|---|
| `redis` | 6379 | The actual Redis data process |
| `sentinel` | 26379 | Watchdog — monitors master, triggers failover |

The **master** handles all writes. **Replicas** continuously stream changes from the master and serve read queries. When the master dies, Sentinel promotes one replica to master in ~10 seconds.

---

## 3. How Sentinel Works

Sentinel is a distributed consensus system built into Redis. Think of it as a **voting committee**:

```
  Sentinel-0        Sentinel-1        Sentinel-2
  (redis-node-0)    (redis-node-1)    (redis-node-2)
       │                  │                  │
       └──────────────────┴──────────────────┘
                  "Is the master alive?"
                  All 3 vote every second.

  If 2 of 3 say "NO" → quorum reached → failover begins
```

**Why quorum = 2?**  
With 3 sentinels, requiring 2 votes prevents a single node with a bad network connection from triggering a false failover. One sentinel acting alone cannot force an election.

**What happens during failover:**

```
T=0s   Master pod is deleted / crashes / OOMKilled

T=1.5s Two sentinels can't reach the master for 1.5 seconds
       (downAfterMilliseconds: 1500)

T=2s   Quorum reached — master is "objectively down"

T=3s   Sentinels elect a new master from the replicas
       (highest replication offset wins)

T=4s   New master is promoted; remaining replica re-syncs to it

T=4s   App's retry loop re-queries Sentinel, gets new master address
       Writes resume automatically — requests that hit during T=0–4s
       retry with exponential backoff and succeed once master is ready

T=30s  Old master pod restarts (Kubernetes StatefulSet)
       Rejoins as a REPLICA of the new master
```

Zero manual intervention. Zero app restart. Data preserved.

---

## 4. Components In Depth

### 4.1 Bitnami Redis Helm Chart

**Chart:** `bitnami/redis`  
**Values file:** `k8s/redis/values.yaml`

Why Bitnami:
- Deploys Redis in replication mode with Sentinel sidecars out of the box
- Manages the StatefulSet, headless service, and PersistentVolumeClaims
- Handles pod identity (stable DNS names like `redis-node-0.redis-headless.fastapi`)

### 4.2 FastAPI — Sentinel-Aware Redis Client

**File:** `app/redis_client.py`

The app uses `redis-py`'s `Sentinel` class. Instead of a static `Redis(host="redis-master")` connection, it asks Sentinel on every connection checkout: *"who is the current master?"*

```python
sentinel = Sentinel(sentinels=[
    ("redis-node-0.redis-headless.fastapi", 26379),
    ("redis-node-1.redis-headless.fastapi", 26379),
    ("redis-node-2.redis-headless.fastapi", 26379),
])

redis_manager.master   # → current elected master (use for all writes)
redis_manager.replica  # → any healthy replica   (use for reads)
```

This means after a failover, the app starts writing to the new master automatically — no code change, no pod restart.

### 4.3 ArgoCD — GitOps Deployment

ArgoCD watches this Git repository. Any `git push` to `main` is automatically reflected in the cluster.

```
git push → ArgoCD detects diff → kubectl apply
```

The FastAPI app (`k8s/deployment.yaml`) is managed by ArgoCD with `selfHeal: true`, meaning any manual cluster change is reverted to match Git within seconds.

Redis is managed by **Helm** separately. ArgoCD is configured not to touch Helm-managed resources.

---

## 5. Configuration Reference

### Sentinel Settings (`k8s/redis/values.yaml`)

| Parameter | Value | What it controls |
|---|---|---|
| `architecture` | `replication` | Master + replicas mode (not standalone) |
| `sentinel.enabled` | `true` | Runs Sentinel sidecar on every pod |
| `sentinel.masterSet` | `mymaster` | Logical name for the cluster — must match app config |
| `sentinel.quorum` | `2` | Minimum sentinel votes required to trigger failover |
| `sentinel.downAfterMilliseconds` | `1500` | Time (ms) master must be unreachable before it's declared down |
| `sentinel.failoverTimeout` | `10000` | Cooldown (ms) between consecutive failovers — prevents flapping |
| `replica.replicaCount` | `2` | Total pods = 3 (1 master + 2 replicas) |

### Redis Memory & Persistence Settings

| Parameter | Value | What it controls |
|---|---|---|
| `master.persistence.enabled` | `true` | PVC — data survives pod restarts |
| `master.persistence.size` | `8Gi` | Disk size per node |
| `maxmemory` | `400mb` | Redis evicts keys above this (80% of 512Mi limit) |
| `maxmemory-policy` | `allkeys-lru` | Evict least-recently-used keys when full |
| `appendonly` | `yes` | AOF persistence — every write fsynced to disk |
| `appendfsync` | `everysec` | Sync frequency — good balance of durability vs. speed |

### App Environment Variables

| Variable | Example Value | Purpose |
|---|---|---|
| `REDIS_SENTINEL_HOSTS` | `redis-node-0.redis-headless.fastapi:26379,...` | All 3 sentinel addresses (comma-separated) |
| `REDIS_MASTER_SET` | `mymaster` | Must match `sentinel.masterSet` in values.yaml |
| `REDIS_PASSWORD` | from `redis-secret` K8s Secret | Auth — never hardcoded |
| `REDIS_SOCKET_TIMEOUT` | `0.5` | Seconds before a connection attempt times out |

---

## 6. Deployment Guide

> Run these commands in order on a fresh cluster. Each step depends on the previous.

### Step 0 — Prerequisites

```bash
# Verify tools are available
kubectl version --client
helm version
minikube version   # local only
argocd version     # optional CLI

# Start minikube (local only)
minikube start --cpus=4 --memory=6144
```

### Step 1 — Namespace

```bash
kubectl apply -f k8s/namespace.yaml
kubectl get namespace fastapi   # should show Active
```

### Step 2 — Redis Password Secret

```bash
# Create the secret (do this BEFORE installing Redis)
kubectl create secret generic redis-secret \
  --from-literal=redis-password='<strong-password>' \
  -n fastapi

# Verify it was created
kubectl get secret redis-secret -n fastapi
```

> **Never** put the password in `values.yaml` or any file committed to Git.  
> For full GitOps secret management, see Sealed Secrets or External Secrets Operator.

### Step 3 — Install Redis

```bash
# Add the Bitnami repo (once per machine)
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update

# Install Redis with our values
helm install redis bitnami/redis \
  --namespace fastapi \
  --values k8s/redis/values.yaml

# Watch pods come up (takes ~60 seconds)
kubectl get pods -n fastapi -w
# Wait until all 3 redis-node-* pods show 3/3 Running
```

### Step 4 — Install ArgoCD

```bash
kubectl create namespace argocd

kubectl apply -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Wait for ArgoCD to be ready
kubectl wait --for=condition=available deployment/argocd-server \
  -n argocd --timeout=120s
```

### Step 5 — Deploy Nexus API via ArgoCD

```bash
# Apply the ArgoCD Application manifest
kubectl apply -f argocd/application.yaml

# Check sync status
kubectl get applications -n argocd
# Expected: SYNC STATUS=Synced  HEALTH STATUS=Healthy
```

### Step 6 — Verify End-to-End

```bash
# All pods running
kubectl get pods -n fastapi

# App health endpoint
curl -s http://$(minikube ip):31876/health/redis
# Expected: {"status":"ok","redis":{"master":"ok","replica":"ok"}}

# Who is the current master?
REDIS_PASS=$(kubectl get secret redis-secret -n fastapi \
  -o jsonpath='{.data.redis-password}' | base64 --decode)

kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -p 26379 -a "$REDIS_PASS" sentinel get-master-addr-by-name mymaster
```

---

## 7. Command Handbook — Health, Monitor & Debug

> Copy-paste reference for day-to-day operations. Set the password variable once at the top of your terminal session.

```bash
# Set once — used in all commands below
REDIS_PASS=$(kubectl get secret redis-secret -n fastapi \
  -o jsonpath='{.data.redis-password}' | base64 --decode)
```

---

### 7.1 Cluster Health

```bash
# All pod statuses at a glance
kubectl get pods -n fastapi

# Detailed status with restart counts and ages
kubectl get pods -n fastapi -o wide

# Show which node each pod is running on (useful for anti-affinity checks)
kubectl get pods -n fastapi -o wide --show-labels

# Watch pods update in real time
kubectl get pods -n fastapi -w
```

---

### 7.2 Identify the Current Master

```bash
# Ask Sentinel who the current master is (returns IP and port)
kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -p 26379 -a "$REDIS_PASS" sentinel get-master-addr-by-name mymaster

# Show full Sentinel info for the master group
kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -p 26379 -a "$REDIS_PASS" sentinel master mymaster

# Show all known replicas from Sentinel's perspective
kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -p 26379 -a "$REDIS_PASS" sentinel replicas mymaster

# Show all sentinels Sentinel knows about
kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -p 26379 -a "$REDIS_PASS" sentinel sentinels mymaster
```

---

### 7.3 Check Replication Status on Each Node

```bash
# Check role and replication lag on a specific node
kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" info replication

# Quick role check across all nodes at once
for node in redis-node-0 redis-node-1 redis-node-2; do
  role=$(kubectl exec $node -n fastapi -c redis -- \
    redis-cli -a "$REDIS_PASS" info replication 2>/dev/null \
    | grep -E "^role:" | tr -d '\r')
  echo "$node  →  $role"
done
```

Expected healthy output:
```
redis-node-0  →  role:slave
redis-node-1  →  role:master
redis-node-2  →  role:slave
```

```bash
# Check replication lag (master_repl_offset vs replica offset)
kubectl exec redis-node-1 -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" info replication \
  | grep -E "role|connected_slaves|slave[0-9]"
```

---

### 7.4 Application Health

```bash
# Redis health via the app's health endpoint
curl -s http://$(minikube ip):31876/health/redis | python3 -m json.tool

# Full app health
curl -s http://$(minikube ip):31876/health | python3 -m json.tool

# If NodePort is unreachable (VPN/network), use port-forward instead
kubectl port-forward svc/nexus-api -n fastapi 8080:80 &
curl -s http://localhost:8080/health/redis | python3 -m json.tool
```

---

### 7.5 Monitor Redis in Real Time

```bash
# Live command stream — shows every Redis command being executed
kubectl exec redis-node-1 -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" monitor

# Live stats (throughput, memory, connections) — refreshes every second
kubectl exec redis-node-1 -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" --stat

# Memory usage breakdown
kubectl exec redis-node-1 -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" info memory | grep -E "used_memory_human|maxmemory_human|mem_fragmentation"

# Connected clients
kubectl exec redis-node-1 -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" info clients
```

---

### 7.6 Inspect Data

```bash
# Count all keys in the database
kubectl exec redis-node-1 -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" dbsize

# List keys matching a pattern (use carefully on large datasets)
kubectl exec redis-node-1 -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" keys "task:*"

# Get a specific key's value
kubectl exec redis-node-1 -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" get "task:some-uuid"

# Get all fields of a hash
kubectl exec redis-node-1 -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" hgetall "task:some-uuid"

# Check TTL on a key (-1 = no expiry, -2 = key does not exist)
kubectl exec redis-node-1 -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" ttl "task:some-uuid"
```

---

### 7.7 View Logs

```bash
# Sentinel container logs (shows failover events, elections, config changes)
kubectl logs redis-node-0 -n fastapi -c sentinel --tail=50
kubectl logs redis-node-1 -n fastapi -c sentinel --tail=50

# Redis data container logs
kubectl logs redis-node-0 -n fastapi -c redis --tail=50

# Stream sentinel logs in real time
kubectl logs redis-node-0 -n fastapi -c sentinel -f

# App logs (shows Sentinel reconnection events after failover)
kubectl logs -l app=nexus-api -n fastapi --tail=100

# Stream app logs in real time
kubectl logs -l app=nexus-api -n fastapi -f
```

---

### 7.8 ArgoCD Status

```bash
# Quick sync/health status
kubectl get applications -n argocd

# Detailed application status
kubectl describe application nexus-api -n argocd

# Get ArgoCD admin password
kubectl get secret argocd-initial-admin-secret -n argocd \
  -o jsonpath='{.data.password}' | base64 --decode && echo

# Open ArgoCD UI (port-forward required — it runs as ClusterIP)
kubectl port-forward svc/argocd-server -n argocd 8443:443
# Then open: https://localhost:8443  (username: admin)
```

---

### 7.9 Helm Chart Management

```bash
# Check installed release
helm list -n fastapi

# Show current values applied to the release
helm get values redis -n fastapi

# Upgrade Redis after editing values.yaml
helm upgrade redis bitnami/redis \
  --namespace fastapi \
  --values k8s/redis/values.yaml

# Roll back to the previous Helm release
helm rollback redis -n fastapi

# Uninstall (destructive — deletes all Redis pods and PVCs)
helm uninstall redis -n fastapi
```

---

## 8. Failover Timeline

```
T=0s    Master pod crashes / is deleted / is OOMKilled
        App starts seeing ConnectionError on Redis writes.
        redis-py Retry begins: 0.1s → 0.2s → 0.4s → 0.8s ... backoff.

T=1.5s  Two remaining Sentinels can't reach the master
        Threshold: downAfterMilliseconds = 1500ms

T=2s    Both Sentinels vote "master is down" — quorum of 2/3 reached
        Sentinel state: ODOWN (objectively down)

T=3s    Sentinels run leader election among themselves
        Winning Sentinel selects a replica to promote
        (selection criteria: lowest replication lag wins)

T=4s    Selected replica receives SLAVEOF NO ONE command
        It becomes the new master

T=4s    Remaining replica is told to replicate from new master

T=4s    App's next retry re-queries Sentinel on port 26379
        Gets new master address → write succeeds
        Request that started at T=0 completes with ~4s extra latency
        No app restart. No manual intervention. No 500 errors.

T=~30s  Kubernetes restarts the crashed pod (StatefulSet guarantees this)
        Pod rejoins cluster as a REPLICA of the new master
        Cluster is fully healthy again — different master, same topology
```

---

## 9. Troubleshooting

### Pods stuck in `Pending`

```bash
kubectl describe pod redis-node-0 -n fastapi
# Look for: "0/1 nodes are available" → insufficient CPU/memory
# Fix: minikube start --cpus=4 --memory=6144
```

### `WRONGPASS` authentication errors in logs

```bash
# Verify the secret exists and is correct
kubectl get secret redis-secret -n fastapi -o jsonpath='{.data.redis-password}' \
  | base64 --decode && echo

# Verify it matches what Helm is using
helm get values redis -n fastapi | grep -A2 auth
```

### App shows `"master":"error"` in health endpoint

```bash
# 1. Check which pod is master
kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -p 26379 -a "$REDIS_PASS" sentinel get-master-addr-by-name mymaster

# 2. Can the master pod be reached directly?
kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -h redis-node-1.redis-headless.fastapi -p 6379 -a "$REDIS_PASS" ping

# 3. Check app logs for connection error details
kubectl logs -l app=nexus-api -n fastapi --tail=50 | grep -i "redis\|sentinel\|error"
```

### Sentinel says `No such master with that name`

```bash
# The masterSet name in the app must match sentinel.masterSet in values.yaml
# Both must be "mymaster"
kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -p 26379 -a "$REDIS_PASS" sentinel masters
# Confirm "name" field = "mymaster"
```

### Replica not syncing (replication lag keeps growing)

```bash
# Check master's connected_slaves count
kubectl exec <MASTER_POD> -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" info replication | grep connected_slaves

# Check replica's master_link_status
kubectl exec <REPLICA_POD> -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" info replication | grep master_link_status
# Should be "up" — if "down", the replica lost its connection to master

# Restart the replica to force re-sync
kubectl delete pod <REPLICA_POD> -n fastapi
# StatefulSet will bring it back and it will re-sync automatically
```

### ArgoCD shows `OutOfSync`

```bash
# Force ArgoCD to re-sync
kubectl patch application nexus-api -n argocd \
  --type merge -p '{"operation":{"sync":{}}}'

# Or use the ArgoCD CLI
argocd app sync nexus-api
```

---

## Summary

| What | How |
|---|---|
| Redis HA | Bitnami Helm chart, replication + Sentinel mode |
| Automatic failover | Sentinel quorum of 2/3, elects new master in ~10 seconds |
| App auto-reconnects | `redis-py` Sentinel client resolves master on every connection |
| Read/write split | Writes → `redis_manager.master`, Reads → `redis_manager.replica` |
| Deployment | ArgoCD GitOps — Git is the source of truth |
| Secrets | K8s Secret (`redis-secret`) — never hardcoded in any file |
| Persistence | PVC with AOF — data survives pod restarts and rescheduling |

> **For the failover drill** (proving all of this works on your local machine), see [`SENTINEL_DRILL.md`](./SENTINEL_DRILL.md).
