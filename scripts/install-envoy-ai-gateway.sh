#!/usr/bin/env bash
# Install the Envoy AI Gateway stack (Envoy Gateway + AI Gateway) into kind-ai-gw-envoy.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTX="kind-ai-gw-envoy"
EG_VERSION="${EG_VERSION:-v1.8.1}"
AIEG_VERSION="${AIEG_VERSION:-v1.0.0}"
H="helm --kube-context $CTX"

echo "==> [1/4] Envoy Gateway $EG_VERSION"
$H upgrade -i eg "$ROOT/envoy-ai-gateway/charts/gateway-helm-${EG_VERSION}.tgz" \
  --namespace envoy-gateway-system --create-namespace \
  -f "$ROOT/envoy-ai-gateway/values/envoy-gateway.values.yaml" --wait --timeout 5m
kubectl --context "$CTX" wait --timeout=3m -n envoy-gateway-system \
  deployment/envoy-gateway --for=condition=Available

echo "==> [2/4] AI Gateway CRDs $AIEG_VERSION"
$H upgrade -i aieg-crd "$ROOT/envoy-ai-gateway/charts/ai-gateway-crds-helm-${AIEG_VERSION}.tgz" \
  --namespace envoy-ai-gateway-system --create-namespace --wait --timeout 5m

echo "==> [3/4] AI Gateway controller $AIEG_VERSION"
$H upgrade -i aieg "$ROOT/envoy-ai-gateway/charts/ai-gateway-helm-${AIEG_VERSION}.tgz" \
  --namespace envoy-ai-gateway-system --create-namespace \
  -f "$ROOT/envoy-ai-gateway/values/ai-gateway.values.yaml" --wait --timeout 5m
kubectl --context "$CTX" wait --timeout=3m -n envoy-ai-gateway-system \
  deployment/ai-gateway-controller --for=condition=Available

# Envoy Gateway resolves the AI Gateway extension server at startup; it was not
# running during step 1, so restart it now that the extension hook is reachable.
echo "==> [4/4] restarting Envoy Gateway to pick up the AI Gateway extension server"
kubectl --context "$CTX" -n envoy-gateway-system rollout restart deployment/envoy-gateway
kubectl --context "$CTX" -n envoy-gateway-system rollout status deployment/envoy-gateway --timeout=3m

echo "OK: Envoy AI Gateway stack ready in $CTX"
