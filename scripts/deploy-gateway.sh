#!/usr/bin/env bash
# Reconcile the Gateway API entry point for exactly one comparison cluster.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/cluster-env.sh"
select_cluster "${1:?usage: deploy-gateway.sh <cluster>}"

case "$STACK" in
  kuadrant)
    kubectl --context "$CONTEXT" apply -f "$ROOT/kuadrant/deploy/gateway/gateway.yaml"
    kubectl --context "$CONTEXT" apply -f "$ROOT/kuadrant/deploy/gateway/policy.yaml"
    ;;
  envoy)
    kubectl --context "$CONTEXT" apply -f "$ROOT/envoy-ai-gateway/deploy/gateway/gateway.yaml"
    ;;
  agent)
    kubectl --context "$CONTEXT" apply -f "$ROOT/agentgateway/deploy/gateway/gateway.yaml"
    ;;
esac

deadline=$((SECONDS + 180))
while ((SECONDS < deadline)); do
  programmed="$(kubectl --context "$CONTEXT" -n "$GATEWAY_NAMESPACE" get gateway "$GATEWAY_NAME" \
    -o 'jsonpath={.status.conditions[?(@.type=="Programmed")].status}' 2>/dev/null || true)"
  [[ "$programmed" == True ]] && break
  sleep 3
done
[[ "${programmed:-}" == True ]] || {
  kubectl --context "$CONTEXT" -n "$GATEWAY_NAMESPACE" get gateway "$GATEWAY_NAME" -o yaml >&2
  exit 1
}

# Envoy's Kind configuration publishes the Service directly. Kuadrant and
# agentgateway use the shared exposure helper to bind their fixed host ports.
if [[ "$STACK" != envoy ]]; then
  bash "$ROOT/scripts/expose-gateway.sh" "$CLUSTER" "$GATEWAY_NAMESPACE" "$GATEWAY_NAME"
fi
echo "OK: $STACK gateway ready at $BASE_URL in $CONTEXT"
