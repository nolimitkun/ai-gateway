#!/usr/bin/env bash
# Show the complete desired-state summary for one comparison cluster.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/cluster-env.sh"
select_cluster "${1:?usage: cluster-status.sh <cluster>}"

echo "=== $CLUSTER ($STACK, $CONTEXT, $BASE_URL) ==="
kubectl --context "$CONTEXT" get gateway -n "$GATEWAY_NAMESPACE" "$GATEWAY_NAME"
kubectl --context "$CONTEXT" get httproute,llminferenceservice,inferencepool -n ai-demo
case "$STACK" in
  kuadrant) kubectl --context "$CONTEXT" get authpolicy,ratelimitpolicy,tokenratelimitpolicy -n ai-demo 2>/dev/null || true ;;
  envoy) kubectl --context "$CONTEXT" get securitypolicy,backendtrafficpolicy,aigatewayroute,envoyextensionpolicy -n ai-demo 2>/dev/null || true ;;
  agent) kubectl --context "$CONTEXT" get agentgatewaypolicy -n ai-demo 2>/dev/null || true ;;
esac
