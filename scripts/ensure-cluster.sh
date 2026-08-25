#!/usr/bin/env bash
# Create one comparison cluster, or start its retained Docker node if it
# already exists. No other cluster is touched.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/cluster-env.sh"
select_cluster "${1:?usage: ensure-cluster.sh <cluster>}"
mode="${2:-}"
[[ -z "$mode" || "$mode" == --existing ]] || { echo "unknown argument: $mode" >&2; exit 2; }

wait_updated_deployment() {
  local namespace="$1" deployment="$2" timeout="$3"
  local deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    if kubectl --context "$CONTEXT" --request-timeout=10s -n "$namespace" \
      get "deployment/$deployment" -o json 2>/dev/null |
      python3 -c '
import json, sys
try:
    obj = json.load(sys.stdin)
except (json.JSONDecodeError, OSError):
    raise SystemExit(1)
desired = obj.get("spec", {}).get("replicas", 1)
status = obj.get("status", {})
ok = (
    status.get("observedGeneration", 0) >= obj.get("metadata", {}).get("generation", 0)
    and status.get("updatedReplicas", 0) == desired
    and status.get("replicas", 0) == desired
    and status.get("readyReplicas", 0) == desired
    and status.get("availableReplicas", 0) == desired
    and status.get("unavailableReplicas", 0) == 0
)
raise SystemExit(0 if ok else 1)
'; then
      echo "deployment/$deployment completed rollout with no old replicas"
      return 0
    fi
    sleep 3
  done
  echo "deployment/$deployment did not complete its rollout in ${timeout}s" >&2
  return 1
}

wait_ready_pods() {
  local namespace="$1" selector="$2" minimum="$3" timeout="$4"
  local deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    if kubectl --context "$CONTEXT" --request-timeout=10s -n "$namespace" \
      get pods -l "$selector" -o json 2>/dev/null |
      python3 -c '
import json, sys
minimum = int(sys.argv[1])
try:
    items = json.load(sys.stdin).get("items", [])
except (json.JSONDecodeError, OSError):
    raise SystemExit(1)
ready = sum(
    any(c.get("type") == "Ready" and c.get("status") == "True"
        for c in pod.get("status", {}).get("conditions", []))
    for pod in items
    if not pod.get("metadata", {}).get("deletionTimestamp")
)
raise SystemExit(0 if ready >= minimum else 1)
' "$minimum"; then
      echo "at least $minimum pod(s) matching $selector are Ready"
      return 0
    fi
    sleep 3
  done
  echo "fewer than $minimum pod(s) matching $selector became Ready in ${timeout}s" >&2
  return 1
}

node="${CLUSTER}-control-plane"
api_timeout=180
if [[ "$mode" == --existing && "$STACK" == kuadrant ]]; then
  api_timeout=600
fi
if docker inspect "$node" >/dev/null 2>&1; then
  if [[ "$(docker inspect -f '{{.State.Running}}' "$node")" != true ]]; then
    docker start "$node" >/dev/null
  fi
elif [[ "$mode" == --existing ]]; then
  echo "$CLUSTER does not exist; run 'make up CLUSTER=$CLUSTER'" >&2
  exit 1
else
  kind create cluster --config "$ROOT/$KIND_CONFIG" --wait 180s
fi

# Every comparison cluster has one control-plane node. Leader election cannot
# provide failover here, while a brief API/etcd stall after Docker resume can
# make the scheduler and controller-manager surrender their leases and exit.
# Keep the single replicas alive so retained clusters can finish reconciling
# pods and rollouts without waiting through repeated static-pod backoff.
for manifest in kube-controller-manager.yaml kube-scheduler.yaml; do
  manifest_path="/etc/kubernetes/manifests/$manifest"
  if docker exec "$node" grep -q -- '--leader-elect=true' "$manifest_path"; then
    docker exec "$node" sed -i \
      's/--leader-elect=true/--leader-elect=false/' "$manifest_path"
  fi
done

# A Docker container reaches Running before kube-apiserver has completed its
# startup and authorization chain. Retained clusters can briefly answer with
# connection errors or Forbidden during that interval, so wait for an
# authenticated node read before asking Kubernetes to evaluate Ready.
deadline=$((SECONDS + api_timeout))
while ((SECONDS < deadline)); do
  if kubectl --context "$CONTEXT" --request-timeout=10s \
    get "node/$node" >/dev/null 2>&1; then
    break
  fi
  sleep 3
done
if ! kubectl --context "$CONTEXT" --request-timeout=10s \
  get "node/$node" >/dev/null 2>&1; then
  echo "Kubernetes API in $CONTEXT did not authorize an admin node read within ${api_timeout}s" >&2
  exit 1
fi
kubectl --context "$CONTEXT" wait --for=condition=Ready "node/$node" --timeout=180s

if [[ "$mode" == --existing && "$STACK" == kuadrant ]] &&
   kubectl --context "$CONTEXT" -n "$GATEWAY_NAMESPACE" get gateway "$GATEWAY_NAME" >/dev/null 2>&1; then
  # Kuadrant serves its Wasm module from the operator pod. After a whole Kind
  # node restarts, Istio's proxy can attempt the fail-closed download before
  # that server is back, cache the failure, become Ready, and answer 503. Prove
  # the artifact is actually served, then recreate only the gateway proxy so
  # it downloads the module from a live endpoint. Restarting controllers here
  # adds avoidable work while the retained control plane is catching up.
  kubectl --context "$CONTEXT" -n kube-system wait pod \
    -l k8s-app=kube-proxy --for=condition=Ready --timeout=300s
  # CoreDNS starts before kube-proxy has necessarily restored the Service
  # network. Its Kubernetes watch resynchronises once that path is healthy,
  # but a retained, controller-heavy Kuadrant node can take several minutes to
  # drain the cold-start CPU backlog. Avoid adding another rollout while the
  # deployment controller is itself catching up; wait on the actual pods.
  wait_ready_pods kube-system k8s-app=kube-dns 1 600
  forward_log="$(mktemp)"
  wasm_timeout=600
  wasm_deadline=$((SECONDS + wasm_timeout))
  wasm_ready=false
  while ((SECONDS < wasm_deadline)); do
    : >"$forward_log"
    kubectl --context "$CONTEXT" -n kuadrant-system port-forward \
      service/kuadrant-operator-wasm 18082:8082 >"$forward_log" 2>&1 &
    forward_pid=$!
    for _ in 1 2 3 4 5; do
      sleep 1
      if curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:18082/plugin.wasm 2>/dev/null; then
        wasm_ready=true
        break
      fi
      kill -0 "$forward_pid" >/dev/null 2>&1 || break
    done
    kill "$forward_pid" >/dev/null 2>&1 || true
    wait "$forward_pid" >/dev/null 2>&1 || true
    [[ "$wasm_ready" == true ]] && break
    sleep 2
  done
  if [[ "$wasm_ready" != true ]]; then
    cat "$forward_log" >&2
    rm -f "$forward_log"
    echo "Kuadrant Wasm service did not serve plugin.wasm within ${wasm_timeout}s" >&2
    exit 1
  fi
  rm -f "$forward_log"
  gateway_deployment="$(kubectl --context "$CONTEXT" -n "$GATEWAY_NAMESPACE" get deployment \
    -l "gateway.networking.k8s.io/gateway-name=$GATEWAY_NAME" -o jsonpath='{.items[0].metadata.name}')"
  [[ -n "$gateway_deployment" ]] || { echo "gateway proxy Deployment not found in $CONTEXT" >&2; exit 1; }
  kubectl --context "$CONTEXT" -n "$GATEWAY_NAMESPACE" rollout restart "deployment/$gateway_deployment"
  wait_updated_deployment "$GATEWAY_NAMESPACE" "$gateway_deployment" 420
  wait_ready_pods "$GATEWAY_NAMESPACE" \
    "gateway.networking.k8s.io/gateway-name=$GATEWAY_NAME" 1 420
fi
echo "OK: $CLUSTER is running ($CONTEXT)"
