#!/usr/bin/env bash
# Add the gateway feature policies -- Keycloak authentication, group
# authorization, request rate limits, daily quotas, and token budgets -- to all
# three stacks, then remove them again with --delete.
#
# The policies are opt-in for a reason: `make up` deliberately leaves the data
# path unauthenticated so the KServe comparison measures routing alone. This
# script layers the security and traffic policies on top of that same path.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUADRANT_CTX=kind-ai-gw-kuadrant
ENVOY_CTX=kind-ai-gw-envoy
AGENT_CTX=kind-ai-gw-agent
EG_VERSION="${EG_VERSION:-v1.8.1}"
DELETE=false
[[ "${1:-}" == "--delete" ]] && DELETE=true

# Policy status conditions differ per implementation and per version. A policy
# that never reports Enforced is worth surfacing, but it must not abort the
# run: the comparison itself is the check that matters.
wait_condition() {
  local ctx="$1" object="$2" condition="$3" deadline=$((SECONDS + 180))
  while ((SECONDS < deadline)); do
    if kubectl --context "$ctx" -n ai-demo get "$object" -o json 2>/dev/null |
      python3 -c '
import json, sys
want = sys.argv[1]
obj = json.load(sys.stdin)
status = obj.get("status", {})
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
  echo "==> removing policies from $KUADRANT_CTX"
  kubectl --context "$KUADRANT_CTX" delete -k "$ROOT/kuadrant/policies" --ignore-not-found
  kubectl --context "$KUADRANT_CTX" apply -f "$ROOT/kuadrant/manifests/policy.yaml"

  echo "==> removing policies from $ENVOY_CTX"
  kubectl --context "$ENVOY_CTX" delete -f "$ROOT/envoy-ai-gateway/policies/rate-limit.yaml" --ignore-not-found
  kubectl --context "$ENVOY_CTX" delete -f "$ROOT/envoy-ai-gateway/policies/security-policy.yaml" --ignore-not-found
  kubectl --context "$ENVOY_CTX" delete -f "$ROOT/envoy-ai-gateway/policies/ai-route.yaml" --ignore-not-found
  helm --kube-context "$ENVOY_CTX" upgrade -i eg \
    "$ROOT/envoy-ai-gateway/charts/gateway-helm-${EG_VERSION}.tgz" \
    --namespace envoy-gateway-system \
    -f "$ROOT/envoy-ai-gateway/values/envoy-gateway.values.yaml" --wait --timeout 5m
  kubectl --context "$ENVOY_CTX" delete -f "$ROOT/envoy-ai-gateway/policies/redis.yaml" --ignore-not-found

  echo "==> removing policies from $AGENT_CTX"
  kubectl --context "$AGENT_CTX" delete -f "$ROOT/agentgateway/policies" --ignore-not-found

  for ctx in "$KUADRANT_CTX" "$ENVOY_CTX" "$AGENT_CTX"; do
    echo "==> removing Keycloak from $ctx"
    if [[ "$ctx" == "$KUADRANT_CTX" ]]; then
      kubectl --context "$ctx" delete -k "$ROOT/keycloak/overlays/openshift" --ignore-not-found
    else
      kubectl --context "$ctx" delete -k "$ROOT/keycloak" --ignore-not-found
    fi
    kubectl --context "$ctx" -n ai-demo delete configmap keycloak-realm --ignore-not-found
  done

  echo "OK: gateway feature policies removed; the KServe path is open again"
  exit 0
fi

bash "$ROOT/scripts/install-keycloak.sh"

echo "==> Kuadrant AuthPolicy, RateLimitPolicy, and TokenRateLimitPolicy"
kubectl --context "$KUADRANT_CTX" apply -k "$ROOT/kuadrant/policies"
wait_condition "$KUADRANT_CTX" authpolicy/kserve-mock Enforced
wait_condition "$KUADRANT_CTX" ratelimitpolicy/kserve-mock Enforced
wait_condition "$KUADRANT_CTX" tokenratelimitpolicy/kserve-mock Enforced

echo "==> Envoy Gateway rate limit backend"
kubectl --context "$ENVOY_CTX" apply -f "$ROOT/envoy-ai-gateway/policies/redis.yaml"
kubectl --context "$ENVOY_CTX" -n redis-system rollout status deployment/redis --timeout=5m
helm --kube-context "$ENVOY_CTX" upgrade -i eg \
  "$ROOT/envoy-ai-gateway/charts/gateway-helm-${EG_VERSION}.tgz" \
  --namespace envoy-gateway-system \
  -f "$ROOT/envoy-ai-gateway/values/envoy-gateway.values.yaml" \
  -f "$ROOT/envoy-ai-gateway/values/envoy-gateway.ratelimit.values.yaml" \
  --wait --timeout 5m
kubectl --context "$ENVOY_CTX" -n envoy-gateway-system rollout status \
  deployment/envoy-gateway --timeout=5m

echo "==> Envoy AI Gateway SecurityPolicy, AIGatewayRoute, and BackendTrafficPolicy"
kubectl --context "$ENVOY_CTX" apply -f "$ROOT/envoy-ai-gateway/policies/security-policy.yaml"
kubectl --context "$ENVOY_CTX" apply -f "$ROOT/envoy-ai-gateway/policies/ai-route.yaml"
kubectl --context "$ENVOY_CTX" apply -f "$ROOT/envoy-ai-gateway/policies/rate-limit.yaml"
wait_condition "$ENVOY_CTX" securitypolicy/kserve-mock Accepted
wait_condition "$ENVOY_CTX" backendtrafficpolicy/kserve-mock Accepted

echo "==> agentgateway JWT, authorization, rate limit, and CORS policies"
kubectl --context "$AGENT_CTX" apply -f "$ROOT/agentgateway/policies"
wait_condition "$AGENT_CTX" agentgatewaypolicy/kserve-mock-jwt Accepted

cat <<'EOF'

OK: gateway feature policies applied.

Every /v1 request now needs a Keycloak access token:

  BASE=http://localhost:8082
  TOKEN=$(curl -sS "$BASE/realms/ai-gateway/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=ai-gateway-cli \
    -d username=alice -d password=alice | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')
  curl "$BASE/v1/chat/completions" -H "authorization: Bearer $TOKEN" \
    -H 'content-type: application/json' \
    -d '{"model":"mock-kserve","messages":[{"role":"user","content":"hello"}]}'

Run 'make compare' to measure authentication, authorization, rate limiting,
quotas, and token budgets across all three gateways.
EOF
