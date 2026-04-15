# Sentinel Failover Drill — Operational Runbook

**Purpose:** Prove that Redis HA works — deliberately kill the master and verify the app survives without a restart.  
**Time required:** ~15 minutes  
**Audience:** Anyone running the Nexus API locally or operating it in production

> **Background reading:** For architecture details, config reference, and the full command handbook, see [`REDIS_HA.md`](./REDIS_HA.md).

---

## Table of Contents

1. [What This Drill Proves](#1-what-this-drill-proves)
2. [How Failover Works](#2-how-failover-works)
3. [Prerequisites](#3-prerequisites)
4. [Pre-Drill Checklist](#4-pre-drill-checklist)
5. [Running the Drill](#5-running-the-drill)
6. [What You Should See](#6-what-you-should-see)
7. [Post-Drill Verification](#7-post-drill-verification)
8. [Pass / Fail Criteria](#8-pass--fail-criteria)
9. [Troubleshooting the Drill](#9-troubleshooting-the-drill)
10. [Key Config Values](#10-key-config-values)

---

## 1. What This Drill Proves

Without Redis HA, a single Redis pod dying means:

```
Your App  →  Redis (single pod)  ← dies → entire app goes down
```

With Sentinel HA, the same event triggers automatic recovery:

```
T=0s   Master pod dies
T=8s   New master elected, app already writing to it
T=30s  Old master restarts as a replica
       → App never went down. Zero manual action required.
```

This drill deliberately kills the master pod and verifies all four claims:

1. Sentinel detects the failure and elects a new master
2. The app reconnects automatically (no restart)
3. All existing data is still intact
4. New writes succeed on the new master

---

## 2. How Failover Works

```
┌─────────────────────────────────────────────────────────────────┐
│  3 Redis pods — each runs a Redis process AND a Sentinel sidecar │
│                                                                   │
│   redis-node-0          redis-node-1          redis-node-2        │
│   [redis:6379]          [redis:6379]          [redis:6379]        │
│   [sentinel:26379]      [sentinel:26379]      [sentinel:26379]    │
│        │                     │                      │             │
│        └─────────────────────┴──────────────────────┘             │
│                   "Is the master alive?"                          │
│                   All 3 ask this every second                     │
└─────────────────────────────────────────────────────────────────┘
```

**Quorum = 2:** Two of three Sentinels must agree the master is unreachable before triggering failover. This prevents a single node with a bad network hiccup from causing false elections.

**Failover sequence:**

```
T=0s    Master pod is deleted (you do this)

T=5s    Two sentinels can't reach master for 5 seconds
        (downAfterMilliseconds = 5000)

T=6s    Quorum reached — "master is objectively down"

T=7s    Sentinels elect a new master from remaining replicas

T=8s    New master is promoted and accepts writes
        App's Sentinel client gets new address → resumes automatically

T=30s   Old master pod restarts (Kubernetes StatefulSet)
        Rejoins as a replica — cluster is fully healthy again
```

---

## 3. Prerequisites

Before running the drill, make sure you have:

- [ ] `kubectl` installed and configured to talk to your cluster
- [ ] `minikube` running (for local testing)
- [ ] Nexus API deployed and all pods healthy
- [ ] Redis installed via Helm (`helm list -n fastapi` shows `redis`)
- [ ] 4 terminal windows ready (or use tmux/split panes)

**Install prerequisites on macOS:**

```bash
# kubectl
brew install kubectl

# minikube
brew install minikube

# helm
brew install helm

# Start minikube if not already running
minikube start --cpus=4 --memory=6144
```

---

## 4. Pre-Drill Checklist

Run these checks **before** starting. If any fail, fix them first — the drill depends on a healthy baseline.

### 4.1 Set the password variable

```bash
# Set once — used in all commands throughout the drill
export REDIS_PASS=$(kubectl get secret redis-secret -n fastapi \
  -o jsonpath='{.data.redis-password}' | base64 --decode)

echo "Password loaded: ${#REDIS_PASS} characters"
# Should print a non-zero length
```

### 4.2 Verify all pods are Running

```bash
kubectl get pods -n fastapi
```

Expected output — all pods must be `Running` with full container counts:

```
NAME                           READY   STATUS    RESTARTS   AGE
redis-node-0                   3/3     Running   0          10m
redis-node-1                   3/3     Running   0          10m
redis-node-2                   3/3     Running   0          10m
nexus-api-xxxx-yyyy            1/1     Running   0          5m
nexus-api-xxxx-zzzz            1/1     Running   0          5m
nexus-api-xxxx-aaaa            1/1     Running   0          5m
nexus-api-xxxx-bbbb            1/1     Running   0          5m
```

> `3/3` on Redis pods = redis container + sentinel container + metrics exporter.  
> If any pod shows `0/3` or `CrashLoopBackOff`, do not proceed — fix it first.

### 4.3 Confirm ArgoCD sees the app as healthy

```bash
kubectl get applications -n argocd
```

Expected:

```
NAME        SYNC STATUS   HEALTH STATUS
nexus-api   Synced        Healthy
```

### 4.4 Find the current master

> **Important:** The master changes after each drill. Never assume it is `redis-node-1`. Always check.

```bash
kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -p 26379 -a "$REDIS_PASS" sentinel get-master-addr-by-name mymaster
```

Example output:
```
10.244.0.15    ← IP of the current master pod
6379
```

To map the IP to a pod name:
```bash
kubectl get pods -n fastapi -o wide | grep redis-node
```

Match the IP column to the sentinel output. **Write down the pod name** — you will kill it in Step 5.

### 4.5 Confirm app health

```bash
curl -s http://$(minikube ip):31876/health/redis | python3 -m json.tool
```

Expected:
```json
{
    "status": "ok",
    "redis": {
        "master": "ok",
        "replica": "ok"
    }
}
```

If `master` or `replica` shows an error here, **stop and fix it** before running the drill.

### 4.6 Snapshot current task count (to verify data survives)

```bash
curl -s http://$(minikube ip):31876/api/v1/tasks | python3 -c \
  "import sys, json; d = json.load(sys.stdin); print(f'Tasks before drill: {len(d[\"data\"])}')"
```

Write down this number. You will compare it after the drill.

---

## 5. Running the Drill

You need **4 terminal windows** open simultaneously. Set up terminals 1–3 first (they are observers), then perform the kill in terminal 4.

---

### Terminal 1 — Watch pod lifecycle

```bash
kubectl get pods -n fastapi -w
```

**What to watch for:** The master pod transitions to `Terminating`, disappears briefly, then restarts as a new pod with `0` restarts. The other pods stay `Running` throughout.

---

### Terminal 2 — Watch Sentinel detect the new master

```bash
watch -n 2 "kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -p 26379 -a \"$REDIS_PASS\" sentinel get-master-addr-by-name mymaster 2>/dev/null"
```

**What to watch for:** The IP address changes within ~10 seconds of the kill. That moment is when failover completes and the new master is active.

---

### Terminal 3 — Watch app health recover

```bash
watch -n 2 "curl -s http://$(minikube ip):31876/health/redis"
```

**What to watch for:** The response briefly shows an error or `"unhealthy"`, then recovers to `"ok"` within 10–30 seconds. This proves the app reconnected automatically.

---

### Terminal 4 — Kill the master

Replace `<MASTER_POD>` with the pod name you identified in Step 4.4 (e.g. `redis-node-1`):

```bash
kubectl delete pod <MASTER_POD> -n fastapi
```

That's it. Now watch terminals 1, 2, and 3.

**One-liner to kill the master automatically** (no need to look up the pod name):

```bash
MASTER_IP=$(kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -p 26379 -a "$REDIS_PASS" sentinel get-master-addr-by-name mymaster 2>/dev/null | head -1)

MASTER_POD=$(kubectl get pods -n fastapi -o wide \
  | awk -v ip="$MASTER_IP" '$6 == ip {print $1}')

echo "Killing master: $MASTER_POD"
kubectl delete pod "$MASTER_POD" -n fastapi
```

---

## 6. What You Should See

| Terminal | Event | What it proves |
|---|---|---|
| **T1 — Pods** | Master pod goes `Terminating`, then a new pod starts | Kubernetes StatefulSet guarantees pod recovery |
| **T2 — Sentinel** | Master IP changes to a different node within ~10 seconds | Sentinel detected the failure and elected a new master |
| **T3 — App health** | Briefly shows error/unhealthy, recovers to `ok` in ~10–30s | App reconnected to new master without a restart |

**Timeline of what you'll see:**

```
0s    kubectl delete pod fires
      T1: master pod → Terminating
      T2: Sentinel still shows old master IP
      T3: App health → "error" (briefly)

~5s   Sentinel declares master ODOWN (objectively down)

~8s   Failover completes
      T2: Master IP changes  ← this is the key moment
      T3: App health → "ok"  ← app reconnected

~30s  T1: Old master restarts as a new pod
      It will rejoin as a replica, not master
```

---

## 7. Post-Drill Verification

Run these commands in terminal 4 after the health endpoint recovers.

### 7.1 Who is the new master?

```bash
kubectl exec redis-node-0 -n fastapi -c redis -- \
  redis-cli -p 26379 -a "$REDIS_PASS" sentinel get-master-addr-by-name mymaster
```

The IP should be different from the one you noted in Step 4.4.

### 7.2 Did the killed pod come back as a replica?

```bash
# Replace <KILLED_POD> with the pod you deleted
kubectl exec <KILLED_POD> -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" info replication | grep role
```

Expected: `role:slave` (not `role:master`)

### 7.3 Check all node roles at once

```bash
for node in redis-node-0 redis-node-1 redis-node-2; do
  role=$(kubectl exec $node -n fastapi -c redis -- \
    redis-cli -a "$REDIS_PASS" info replication 2>/dev/null \
    | grep -E "^role:" | tr -d '\r')
  echo "$node  →  $role"
done
```

Expected output (one master, two slaves — specific node may differ):
```
redis-node-0  →  role:slave
redis-node-1  →  role:slave
redis-node-2  →  role:master
```

### 7.4 Is all the data still there?

```bash
curl -s http://$(minikube ip):31876/api/v1/tasks | python3 -c \
  "import sys, json; d = json.load(sys.stdin); print(f'Tasks after drill: {len(d[\"data\"])}')"
```

The count must match the number from Step 4.6.

### 7.5 Can we write to the new master?

```bash
curl -s -X POST http://$(minikube ip):31876/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"title":"Post-Failover Test Task","priority":"critical","status":"todo"}' \
  | python3 -m json.tool
```

Expected: HTTP 201 response with the new task's ID.

### 7.6 Is ArgoCD still happy?

```bash
kubectl get applications -n argocd
```

Expected: `Synced` / `Healthy`

---

## 8. Pass / Fail Criteria

Check off each item. All 6 must pass for the drill to be considered successful.

- [ ] **Sentinel detected failover** — Terminal 2 showed the master IP change within ~10 seconds
- [ ] **App recovered automatically** — Terminal 3 returned to `"master":"ok"` without a pod restart
- [ ] **Killed pod came back as replica** — `role:slave` on the previously-killed pod
- [ ] **Cluster has exactly one master** — Only one pod shows `role:master` in the role check
- [ ] **Data survived** — Task count after drill matches task count before drill
- [ ] **New writes work** — POST /api/v1/tasks succeeded on the new master

If all 6 pass: **the HA setup is working correctly.**

If any fail: see [Section 9 — Troubleshooting the Drill](#9-troubleshooting-the-drill).

---

## 9. Troubleshooting the Drill

### App health never recovers (stays in error state)

```bash
# Check which container in the app pod is failing
kubectl describe pod -l app=nexus-api -n fastapi | grep -A5 "State:"

# Check app logs for the exact error
kubectl logs -l app=nexus-api -n fastapi --tail=50 | grep -i "redis\|sentinel\|error\|connect"
```

Common causes:
- Sentinel hasn't finished electing (wait another 10–15 seconds)
- All 3 pods went down at once (StatefulSet will recover them — just wait)
- Wrong password in the app's environment vs. the secret

### Terminal 2 shows the same IP after 30 seconds

```bash
# Check if the Sentinel on redis-node-0 is alive
kubectl exec redis-node-0 -n fastapi -c sentinel -- \
  redis-cli -p 26379 -a "$REDIS_PASS" ping
# Should return: PONG

# Check Sentinel's view of the master
kubectl exec redis-node-0 -n fastapi -c sentinel -- \
  redis-cli -p 26379 -a "$REDIS_PASS" sentinel master mymaster \
  | grep -E "name|ip|port|flags"
```

If `flags` shows `o_down` or `s_down`, Sentinel is still in the middle of failover — wait.

### Killed pod comes back as master (split-brain)

```bash
# Force the old master to become a replica of the new master
kubectl exec <OLD_MASTER_POD> -n fastapi -c redis -- \
  redis-cli -a "$REDIS_PASS" replicaof <NEW_MASTER_IP> 6379
```

This situation should not happen in a healthy Sentinel setup. If it does, check the Sentinel logs for election errors:

```bash
kubectl logs redis-node-0 -n fastapi -c sentinel --tail=100 | grep -i "elect\|failover\|error"
```

### Data count doesn't match after drill

Data loss after failover means the replica that was promoted had not yet received all writes from the master before it died. Check the AOF persistence and replication settings:

```bash
# Check replication offset gap at time of failover (in sentinel logs)
kubectl logs redis-node-0 -n fastapi -c sentinel --tail=100 | grep "replication"

# Verify AOF is enabled on all nodes
for node in redis-node-0 redis-node-1 redis-node-2; do
  echo -n "$node appendonly: "
  kubectl exec $node -n fastapi -c redis -- \
    redis-cli -a "$REDIS_PASS" config get appendonly 2>/dev/null | tail -1
done
```

### `watch` command not available on macOS

```bash
# Install watch on macOS
brew install watch

# Or use a bash loop as a substitute
while true; do
  clear
  kubectl exec redis-node-0 -n fastapi -c redis -- \
    redis-cli -p 26379 -a "$REDIS_PASS" sentinel get-master-addr-by-name mymaster 2>/dev/null
  sleep 2
done
```

### Port-forward for app health check (if NodePort is unreachable)

```bash
# Run in background
kubectl port-forward svc/nexus-api -n fastapi 8080:80 &
PF_PID=$!

# Now use localhost:8080 instead of minikube IP
watch -n 2 "curl -s http://localhost:8080/health/redis"

# Kill port-forward when done
kill $PF_PID
```

---

## 10. Key Config Values

| Setting | Value | Why it matters |
|---|---|---|
| `quorum` | `2` | 2 of 3 Sentinels must agree master is down — prevents false failovers |
| `downAfterMilliseconds` | `5000` | Wait 5 seconds before declaring master dead — avoids reacting to transient blips |
| `failoverTimeout` | `10000` | 10-second cooldown between failovers — prevents flapping |
| `replicaCount` | `2` | 2 replicas = 3 pods total — minimum viable HA |
| Redis password | from `redis-secret` K8s Secret | Never hardcoded in any file or values |

---

## Why This Matters

| Scenario | Without Sentinel | With Sentinel |
|---|---|---|
| Master pod crashes | App is down until manually fixed | Auto-failover in ~10 seconds |
| Node goes offline overnight | All data inaccessible | Replica promoted, app keeps running |
| Bad deploy OOMKills Redis | Manual rollback + downtime | Sentinel promotes a healthy replica |
| Data loss risk on master | All data gone | Replicas hold a copy — minimal or zero loss |

> After every infrastructure change or Redis upgrade, re-run this drill to confirm HA is still working.
