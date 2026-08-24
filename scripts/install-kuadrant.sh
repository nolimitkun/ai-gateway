#!/usr/bin/env bash
# Install Kuadrant on the same Envoy AI Gateway data path used by KServe.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTX="kind-ai-gw-kuadrant"
GAIE_VERSION="${GAIE_VERSION:-v1.5.0}"
EG_VERSION="${EG_VERSION:-v1.8.1}"
AIEG_VERSION="${AIEG_VERSION:-v1.0.0}"
KUADRANT_VERSION="${KUADRANT_VERSION:-1.5.2}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.17.0}"
KUADRANT_CHART="$ROOT/kuadrant/charts/kuadrant-operator-${KUADRANT_VERSION}.tgz"
H="helm --kube-context $CTX"

# Envoy Gateway's chart owns the core Gateway API CRDs. Applying a second
# release first causes Helm server-side-apply ownership conflicts.
echo "==> [1/8] Gateway API Inference Extension CRDs $GAIE_VERSION"
kubectl --context "$CTX" apply --server-side -f "https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/${GAIE_VERSION}/v1-manifests.yaml"
kubectl --context "$CTX" apply -f "$ROOT/kserve/manifests/envoy-inferencepool-rbac.yaml"

echo "==> [2/8] cert-manager $CERT_MANAGER_VERSION"
helm --kube-context "$CTX" upgrade -i cert-manager "$ROOT/kserve/charts/cert-manager-${CERT_MANAGER_VERSION}.tgz" --namespace cert-manager --create-namespace --set crds.enabled=true --wait --timeout 5m

echo "==> [3/8] Envoy Gateway $EG_VERSION"
$H upgrade -i eg "$ROOT/envoy-ai-gateway/charts/gateway-helm-${EG_VERSION}.tgz" \
  --namespace envoy-gateway-system --create-namespace \
  -f "$ROOT/envoy-ai-gateway/values/envoy-gateway.values.yaml" --wait --timeout 8m
kubectl --context "$CTX" -n envoy-gateway-system wait deployment/envoy-gateway \
  --for=condition=Available --timeout=3m

echo "==> [4/8] Envoy AI Gateway CRDs and controller $AIEG_VERSION"
$H upgrade -i aieg-crd "$ROOT/envoy-ai-gateway/charts/ai-gateway-crds-helm-${AIEG_VERSION}.tgz" \
  --namespace envoy-ai-gateway-system --create-namespace --wait --timeout 5m
$H upgrade -i aieg "$ROOT/envoy-ai-gateway/charts/ai-gateway-helm-${AIEG_VERSION}.tgz" \
  --namespace envoy-ai-gateway-system --create-namespace \
  -f "$ROOT/envoy-ai-gateway/values/ai-gateway.values.yaml" --wait --timeout 5m
kubectl --context "$CTX" -n envoy-ai-gateway-system wait \
  deployment/ai-gateway-controller --for=condition=Available --timeout=3m

echo "==> [5/8] restart Envoy Gateway with the AI extension available"
kubectl --context "$CTX" -n envoy-gateway-system rollout restart deployment/envoy-gateway
kubectl --context "$CTX" -n envoy-gateway-system rollout status deployment/envoy-gateway --timeout=3m

echo "==> [6/8] Kuadrant operator $KUADRANT_VERSION"
test -f "$KUADRANT_CHART" || {
  echo "missing $KUADRANT_CHART; run 'make charts'" >&2
  exit 1
}
helm --kube-context "$CTX" upgrade -i kuadrant-operator "$KUADRANT_CHART" --namespace kuadrant-system --create-namespace --wait --timeout 8m

echo "==> [7/8] Kuadrant operands"
kubectl --context "$CTX" apply -f "$ROOT/kuadrant/manifests/instance.yaml"
kubectl --context "$CTX" -n kuadrant-system wait kuadrant/kuadrant --for=condition=Ready --timeout=8m

echo "==> [8/8] controller readiness"
kubectl --context "$CTX" -n envoy-gateway-system wait deployment/envoy-gateway \
  --for=condition=Available --timeout=3m
echo "OK: Kuadrant + Envoy AI Gateway ready in $CTX"
