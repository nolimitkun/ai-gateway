#!/usr/bin/env bash
# Install Envoy AI Gateway for the KServe-only comparison.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $# -eq 1 ]] || { echo "usage: $0 kind-ai-gw-envoy" >&2; exit 2; }
CTX="$1"
[[ "$CTX" == kind-ai-gw-envoy ]] || { echo "install-envoy-ai-gateway.sh only supports kind-ai-gw-envoy" >&2; exit 2; }
EG_VERSION="${EG_VERSION:-v1.8.1}"
AIEG_VERSION="${AIEG_VERSION:-v1.0.0}"
GAIE_VERSION="${GAIE_VERSION:-v1.5.0}"
H="helm --kube-context $CTX"

echo "==> [1/5] Gateway API Inference Extension CRDs $GAIE_VERSION"
kubectl --context "$CTX" apply --server-side -f \
  "https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/${GAIE_VERSION}/v1-manifests.yaml"

kubectl --context "$CTX" apply -f "$ROOT/kserve/manifests/envoy-inferencepool-rbac.yaml"

echo "==> [2/5] Envoy Gateway $EG_VERSION"
$H upgrade -i eg "$ROOT/envoy-ai-gateway/charts/gateway-helm-${EG_VERSION}.tgz" \
  --namespace envoy-gateway-system --create-namespace \
  -f "$ROOT/envoy-ai-gateway/values/envoy-gateway.values.yaml" --wait --timeout 5m
kubectl --context "$CTX" wait --timeout=3m -n envoy-gateway-system \
  deployment/envoy-gateway --for=condition=Available
bash "$ROOT/scripts/fix-envoy-gateway-crd.sh" "$CTX"

echo "==> [3/5] AI Gateway CRDs $AIEG_VERSION"
$H upgrade -i aieg-crd "$ROOT/envoy-ai-gateway/charts/ai-gateway-crds-helm-${AIEG_VERSION}.tgz" \
  --namespace envoy-ai-gateway-system --create-namespace --wait --timeout 5m

echo "==> [4/5] AI Gateway controller $AIEG_VERSION"
$H upgrade -i aieg "$ROOT/envoy-ai-gateway/charts/ai-gateway-helm-${AIEG_VERSION}.tgz" \
  --namespace envoy-ai-gateway-system --create-namespace \
  -f "$ROOT/envoy-ai-gateway/values/ai-gateway.values.yaml" --wait --timeout 5m
kubectl --context "$CTX" wait --timeout=3m -n envoy-ai-gateway-system \
  deployment/ai-gateway-controller --for=condition=Available

# Envoy Gateway resolves the AI Gateway extension server at startup; it was not
# running during step 1, so restart it now that the extension hook is reachable.
echo "==> [5/5] InferencePool RBAC and Envoy Gateway restart"
kubectl --context "$CTX" -n envoy-gateway-system rollout restart deployment/envoy-gateway
kubectl --context "$CTX" -n envoy-gateway-system rollout status deployment/envoy-gateway --timeout=3m

echo "OK: Envoy AI Gateway stack ready in $CTX"
