#!/usr/bin/env bash
# Add the vLLM Semantic Router to all three stacks, then remove it with
# --delete.
#
# The router is an Envoy external processor. The same workload and the same
# routing rules go into every cluster; what differs -- and what the comparison
# measures -- is the API each stack offers for attaching it: an Istio
# EnvoyFilter for the OpenShift profile, an EnvoyExtensionPolicy for Envoy AI
# Gateway, and one field of an AgentgatewayPolicy for agentgateway.
#
# Like the feature policies, this layer is opt-in: `make up` measures routing
# on its own, and only a request whose model is "auto" is resolved here.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUADRANT_CTX=kind-ai-gw-kuadrant
ENVOY_CTX=kind-ai-gw-envoy
AGENT_CTX=kind-ai-gw-agent
CONFIG="$ROOT/semantic-router/config/router-config.yaml"
MANIFESTS="$ROOT/semantic-router/manifests"
DELETE=false
[[ "${1:-}" == "--delete" ]] && DELETE=true

# An attachment that was applied is not yet an attachment the proxy is using.
# Two of the three report a status; waiting on it also surfaces a policy the
# controller rejected, which would otherwise look like a working router that
# simply never routed anything.
wait_condition() {
  local ctx="$1" object="$2" condition="$3" deadline=$((SECONDS + 180))
  while ((SECONDS < deadline)); do
    if kubectl --context "$ctx" -n ai-demo get "$object" -o json 2>/dev/null |
      python3 -c '
import json, sys
want = sys.argv[1]
status = json.load(sys.stdin).get("status", {})
conditions = list(status.get("conditions", []))
for key in ("ancestors", "parents"):
    for ancestor in status.get(key, []):
        conditions.extend(ancestor.get("conditions", []))
sys.exit(0 if any(c.get("type") == want and c.get("status") == "True"
                  for c in conditions) else 1)' "$condition"; then
      echo "$object $condition"
      return 0
    fi
    sleep 3
  done
  echo "warning: $object did not report $condition in $ctx" >&2
  return 0
}

attachment_for() {
  case "$1" in
    "$KUADRANT_CTX") echo "$MANIFESTS/kuadrant-extproc.yaml" ;;
    "$ENVOY_CTX") echo "$MANIFESTS/envoy-extproc.yaml" ;;
    "$AGENT_CTX") echo "$MANIFESTS/agentgateway-extproc.yaml" ;;
  esac
}

if $DELETE; then
  for ctx in "$KUADRANT_CTX" "$ENVOY_CTX" "$AGENT_CTX"; do
    echo "==> removing the semantic router from $ctx"
    # The attachment goes first: a gateway still calling an ext_proc service
    # that no longer exists would fail open on every request until the filter
    # is withdrawn, which reads like a routing bug rather than a teardown.
    kubectl --context "$ctx" delete -f "$(attachment_for "$ctx")" --ignore-not-found
    kubectl --context "$ctx" delete -f "$MANIFESTS/semantic-router.yaml" --ignore-not-found
    kubectl --context "$ctx" -n ai-demo delete configmap semantic-router-config --ignore-not-found
  done
  echo "OK: semantic router removed; 'auto' is no longer a routable model"
  exit 0
fi

for ctx in "$KUADRANT_CTX" "$ENVOY_CTX" "$AGENT_CTX"; do
  echo "==> semantic router workload in $ctx"
  kubectl --context "$ctx" -n ai-demo create configmap semantic-router-config \
    --from-file=router-config.yaml="$CONFIG" \
    --dry-run=client -o yaml | kubectl --context "$ctx" apply -f -
  kubectl --context "$ctx" apply -f "$MANIFESTS/semantic-router.yaml"
  # A subPath ConfigMap mount is never refreshed in place, so a rules change
  # only reaches the router through a new pod.
  kubectl --context "$ctx" -n ai-demo rollout restart deployment/semantic-router
  kubectl --context "$ctx" -n ai-demo rollout status \
    deployment/semantic-router --timeout=5m

  echo "==> ext_proc attachment in $ctx"
  kubectl --context "$ctx" apply -f "$(attachment_for "$ctx")"
  case "$ctx" in
    "$ENVOY_CTX") wait_condition "$ctx" envoyextensionpolicy/semantic-router Accepted ;;
    "$AGENT_CTX") wait_condition "$ctx" agentgatewaypolicy/kserve-mock-semantic-router Accepted ;;
    # An EnvoyFilter has no status to wait on. `make compare` waits for the
    # data plane itself before it measures anything, which is the only check
    # that covers this stack.
    *) echo "EnvoyFilter applied; Istio reports no status for it" ;;
  esac
done

cat <<'EOF'

OK: the semantic router is attached to all three gateways.

A request naming a model is routed to that model, exactly as before. A request
asking for "auto" is resolved by the router from the prompt, and the answer
reports which model actually served it:

  BASE=http://localhost:8082
  curl "$BASE/v1/chat/completions" -H 'content-type: application/json' \
    -d '{"model":"auto","messages":[{"role":"user","content":"prove that sqrt 2 is irrational"}]}'

  # -> "model": "kimi-k3", "mock_tier": "big"

Run 'make compare' to measure the decision through all three gateways.
EOF
