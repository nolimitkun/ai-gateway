#!/usr/bin/env bash
# Install the shared Keycloak realm used by every gateway's auth policy.
#
# One Keycloak per cluster: the three stacks stay independent, and each
# gateway validates tokens against an issuer it can resolve in-cluster.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTEXTS=(kind-ai-gw-kuadrant kind-ai-gw-envoy kind-ai-gw-agent)

for ctx in "${CONTEXTS[@]}"; do
  echo "==> Keycloak in $ctx"
  kubectl --context "$ctx" create namespace ai-demo \
    --dry-run=client -o yaml | kubectl --context "$ctx" apply -f -
  kubectl --context "$ctx" -n ai-demo create configmap keycloak-realm \
    --from-file=ai-gateway-realm.json="$ROOT/keycloak/realm/ai-gateway-realm.json" \
    --dry-run=client -o yaml | kubectl --context "$ctx" apply -f -
  if [[ "$ctx" == "kind-ai-gw-kuadrant" ]]; then
    kubectl --context "$ctx" apply -k "$ROOT/keycloak/overlays/openshift"
  else
    kubectl --context "$ctx" apply -k "$ROOT/keycloak"
  fi
  kubectl --context "$ctx" -n ai-demo rollout status deployment/keycloak --timeout=5m
done

echo "OK: Keycloak realm 'ai-gateway' ready in all clusters"
