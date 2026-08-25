#!/usr/bin/env bash
# Create one comparison cluster, or start its retained Docker node if it
# already exists. No other cluster is touched.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/cluster-env.sh"
select_cluster "${1:?usage: ensure-cluster.sh <cluster>}"
mode="${2:-}"
[[ -z "$mode" || "$mode" == --existing ]] || { echo "unknown argument: $mode" >&2; exit 2; }

node="${CLUSTER}-control-plane"
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

# A Docker container reaches Running before kube-apiserver has completed its
# startup and authorization chain. Retained clusters can briefly answer with
# connection errors or Forbidden during that interval, so wait for an
# authenticated node read before asking Kubernetes to evaluate Ready.
deadline=$((SECONDS + 180))
while ((SECONDS < deadline)); do
  if kubectl --context "$CONTEXT" get "node/$node" >/dev/null 2>&1; then
    break
  fi
  sleep 3
done
if ! kubectl --context "$CONTEXT" get "node/$node" >/dev/null 2>&1; then
  echo "Kubernetes API in $CONTEXT did not authorize an admin node read within 180s" >&2
  exit 1
fi
kubectl --context "$CONTEXT" wait --for=condition=Ready "node/$node" --timeout=180s

if [[ "$mode" == --existing && "$STACK" == kuadrant ]] &&
   kubectl --context "$CONTEXT" -n "$GATEWAY_NAMESPACE" get gateway "$GATEWAY_NAME" >/dev/null 2>&1; then
  # Kuadrant serves its Wasm module from the operator pod. After a whole Kind
  # node restarts, Istio's proxy can attempt the fail-closed download before
  # that server is back, cache the failure, become Ready, and answer 503. Wait
  # for the actual operator pod—not a stale Deployment status—then recreate
  # only this gateway proxy so it downloads the module from a live endpoint.
  kubectl --context "$CONTEXT" -n kuadrant-system wait pod \
    -l app=kuadrant,control-plane=controller-manager \
    --for=condition=Ready --timeout=420s
  kubectl --context "$CONTEXT" -n istio-system wait pod \
    -l istio=pilot --for=condition=Ready --timeout=300s
  forward_log="$(mktemp)"
  kubectl --context "$CONTEXT" -n kuadrant-system port-forward \
    service/kuadrant-operator-wasm 18082:8082 >"$forward_log" 2>&1 &
  forward_pid=$!
  wasm_deadline=$((SECONDS + 120))
  wasm_ready=false
  while ((SECONDS < wasm_deadline)); do
    if curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:18082/plugin.wasm 2>/dev/null; then
      wasm_ready=true
      break
    fi
    kill -0 "$forward_pid" >/dev/null 2>&1 || break
    sleep 2
  done
  kill "$forward_pid" >/dev/null 2>&1 || true
  wait "$forward_pid" >/dev/null 2>&1 || true
  if [[ "$wasm_ready" != true ]]; then
    cat "$forward_log" >&2
    rm -f "$forward_log"
    echo "Kuadrant Wasm service did not serve plugin.wasm within 120s" >&2
    exit 1
  fi
  rm -f "$forward_log"
  gateway_deployment="$(kubectl --context "$CONTEXT" -n "$GATEWAY_NAMESPACE" get deployment \
    -l "gateway.networking.k8s.io/gateway-name=$GATEWAY_NAME" -o jsonpath='{.items[0].metadata.name}')"
  [[ -n "$gateway_deployment" ]] || { echo "gateway proxy Deployment not found in $CONTEXT" >&2; exit 1; }
  kubectl --context "$CONTEXT" -n "$GATEWAY_NAMESPACE" rollout restart "deployment/$gateway_deployment"
  kubectl --context "$CONTEXT" -n "$GATEWAY_NAMESPACE" rollout status \
    "deployment/$gateway_deployment" --timeout=420s
fi
echo "OK: $CLUSTER is running ($CONTEXT)"
