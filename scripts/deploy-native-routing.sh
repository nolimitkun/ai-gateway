#!/usr/bin/env bash
# Attach native body-model routing to one gateway context, then remove it with
# --delete.
#
# Every stack in this repository routes `body.model` to an accelerator pool
# through the same external component: the Gateway API Inference Extension
# body-based router, which copies the model name into a header that ordinary
# Gateway API matching can see. Two of the three do not need it. This overlay
# expresses the same twelve model-to-pool mappings in each stack's own
# body-aware API instead:
#
#   Envoy AI Gateway   AIGatewayRoute, whose model name "is extracted from the
#                      request content before the routing decision"
#   agentgateway       AgentgatewayModel, matching `model` from the request
#                      and pointing a Custom provider at the InferencePool
#   Kuadrant           nothing -- Istio has no body-aware routing API, which
#                      is why that stack drives BBR from a raw EnvoyFilter
#
# The overlay is additive. Both stacks serve it on the native.local hostname
# and leave the BBR path untouched, so one cluster answers the same request
# either way and the comparison can scale BBR to zero to show which path
# actually depends on it.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/component-env.sh"
KUADRANT_CTX=kind-ai-gw-kuadrant
ENVOY_CTX=kind-ai-gw-envoy
AGENT_CTX=kind-ai-gw-agent
DELETE=false
SELECTED_CONTEXT=""
while (($#)); do
  case "$1" in
    --delete)
      DELETE=true
      shift
      ;;
    --context)
      [[ -n "${2:-}" ]] || { echo "--context requires a kubectl context" >&2; exit 2; }
      SELECTED_CONTEXT="$2"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

case "$SELECTED_CONTEXT" in
  "$ENVOY_CTX"|"$AGENT_CTX") ;;
  "$KUADRANT_CTX")
    cat >&2 <<'EOF'
The OpenShift profile has no native body-model routing to attach.

Istio exposes no Gateway API or provider CRD that reads the request body, which
is why this stack runs BBR from a raw EnvoyFilter with a per-route patch for
every model rule. That is a capability difference, not a missing manifest, and
the comparison records it as one.
EOF
    exit 2
    ;;
  *) echo "unsupported native-routing context: $SELECTED_CONTEXT" >&2; exit 2 ;;
esac
select_context_components "$SELECTED_CONTEXT"
MANIFESTS="$COMPONENT_ROOT/native-routing"

# The overlay routes to the accelerator pools by name. Without them every rule
# would reference an InferencePool that does not exist, and the failure would
# surface as a 500 at request time rather than at apply time.
if ! kubectl --context "$SELECTED_CONTEXT" -n ai-demo \
  get llminferenceservice kserve-b300 >/dev/null 2>&1; then
  echo "accelerator pools are not installed; run 'make pools CLUSTER=<name>' first" >&2
  exit 2
fi

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

if $DELETE; then
  echo "==> removing native routing from $SELECTED_CONTEXT"
  case "$SELECTED_CONTEXT" in
    "$ENVOY_CTX")
      # The SecurityPolicy first. Deleting the route first would briefly leave
      # a policy targeting a route that no longer exists, which Envoy Gateway
      # reports as a rejected policy and looks like a broken teardown.
      kubectl --context "$SELECTED_CONTEXT" delete -f "$MANIFESTS/security-policy.yaml" --ignore-not-found
      kubectl --context "$SELECTED_CONTEXT" delete -f "$MANIFESTS/ai-route.yaml" --ignore-not-found
      ;;
    "$AGENT_CTX")
      kubectl --context "$SELECTED_CONTEXT" delete -f "$MANIFESTS/models.yaml" --ignore-not-found
      kubectl --context "$SELECTED_CONTEXT" delete -f "$MANIFESTS/policies.yaml" --ignore-not-found
      # The listener goes last and is restored from the base manifest rather
      # than patched away, so the Gateway ends byte-identical to a cluster
      # that never had the overlay.
      kubectl --context "$SELECTED_CONTEXT" apply -f "$COMPONENT_ROOT/gateway/gateway.yaml"
      ;;
  esac
  echo "OK: native routing removed; native.local no longer resolves a model"
  exit 0
fi

case "$SELECTED_CONTEXT" in
  "$ENVOY_CTX")
    echo "==> AIGatewayRoute and SecurityPolicy in $SELECTED_CONTEXT"
    kubectl --context "$SELECTED_CONTEXT" apply -f "$MANIFESTS/ai-route.yaml"
    kubectl --context "$SELECTED_CONTEXT" apply -f "$MANIFESTS/security-policy.yaml"
    wait_condition "$SELECTED_CONTEXT" aigatewayroute/kserve-mock-native Accepted
    wait_condition "$SELECTED_CONTEXT" securitypolicy/kserve-mock-native Accepted
    ;;
  "$AGENT_CTX")
    echo "==> native listener in $SELECTED_CONTEXT"
    kubectl --context "$SELECTED_CONTEXT" apply -f "$MANIFESTS/gateway.yaml"
    kubectl --context "$SELECTED_CONTEXT" -n ai-demo wait --for=condition=Programmed \
      gateway/ai-gateway --timeout=5m
    echo "==> models and listener policies in $SELECTED_CONTEXT"
    kubectl --context "$SELECTED_CONTEXT" apply -f "$MANIFESTS/policies.yaml"
    kubectl --context "$SELECTED_CONTEXT" apply -f "$MANIFESTS/models.yaml"
    wait_condition "$SELECTED_CONTEXT" agentgatewaypolicy/kserve-mock-native-jwt Accepted
    wait_condition "$SELECTED_CONTEXT" agentgatewaypolicy/kserve-mock-native-small-tier Accepted
    ;;
esac

cat <<'EOF'

OK: native body-model routing is attached on the native.local hostname.

The same request reaches the same pool with no body-based router in the path:

  BASE=http://localhost:8080   # 8081 for agentgateway
  curl "$BASE/v1/chat/completions" -H 'host: native.local' \
    -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
    -d '{"model":"kimi-k3","messages":[{"role":"user","content":"hello"}]}'

One thing does not follow it there, and only on Envoy: `model: auto` is
resolved by the semantic router, which is attached to the BBR route on that
stack. On agentgateway the router is attached to the Gateway, so it runs on
this listener too and `auto` keeps working. The model catalog answers on both.

Run 'make compare CLUSTER=<name>' to record what the native path actually did.
EOF
