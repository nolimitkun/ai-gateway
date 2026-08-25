#!/usr/bin/env bash
# Install the closest kind analogue of OpenShift AI + Connectivity Link:
# Kuadrant policies on an Istio control plane and Envoy gateway proxy.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $# -eq 1 ]] || { echo "usage: $0 kind-ai-gw-kuadrant" >&2; exit 2; }
CTX="$1"
[[ "$CTX" == kind-ai-gw-kuadrant ]] || { echo "install-kuadrant.sh only supports kind-ai-gw-kuadrant" >&2; exit 2; }
GWAPI_VERSION="${GWAPI_VERSION:-1.4.1}"
GAIE_VERSION="${GAIE_VERSION:-v1.5.0}"
ISTIO_VERSION="${ISTIO_VERSION:-1.29.2}"
KUADRANT_VERSION="${KUADRANT_VERSION:-1.5.2}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.17.0}"
KUADRANT_CHART="$ROOT/kuadrant/charts/kuadrant-operator-${KUADRANT_VERSION}.tgz"

echo "==> [1/7] Gateway API CRDs v$GWAPI_VERSION"
kubectl --context "$CTX" apply --server-side -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/v${GWAPI_VERSION}/standard-install.yaml"

echo "==> [2/7] Gateway API Inference Extension CRDs $GAIE_VERSION"
kubectl --context "$CTX" apply --server-side -f "https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/${GAIE_VERSION}/v1-manifests.yaml"

echo "==> [3/7] cert-manager $CERT_MANAGER_VERSION"
helm --kube-context "$CTX" upgrade -i cert-manager "$ROOT/kserve/charts/cert-manager-${CERT_MANAGER_VERSION}.tgz" --namespace cert-manager --create-namespace --set crds.enabled=true --wait --timeout 5m

echo "==> [4/7] Istio $ISTIO_VERSION with InferencePool support"
helm --kube-context "$CTX" upgrade -i istio-base "$ROOT/kuadrant/charts/base-${ISTIO_VERSION}.tgz" --namespace istio-system --create-namespace --wait --timeout 5m
helm --kube-context "$CTX" upgrade -i istiod "$ROOT/kuadrant/charts/istiod-${ISTIO_VERSION}.tgz" --namespace istio-system --set pilot.env.ENABLE_GATEWAY_API_INFERENCE_EXTENSION=true --wait --timeout 8m

echo "==> [5/7] Kuadrant operator $KUADRANT_VERSION"
test -f "$KUADRANT_CHART" || {
  echo "missing $KUADRANT_CHART; run 'make charts'" >&2
  exit 1
}
helm --kube-context "$CTX" upgrade -i kuadrant-operator "$KUADRANT_CHART" --namespace kuadrant-system --create-namespace --wait --timeout 15m

echo "==> [6/7] Kuadrant operands"
kubectl --context "$CTX" apply -f "$ROOT/kuadrant/manifests/instance.yaml"
kubectl --context "$CTX" -n kuadrant-system wait kuadrant/kuadrant --for=condition=Ready --timeout=12m

echo "==> [7/7] controller readiness"
kubectl --context "$CTX" -n istio-system wait deployment/istiod \
  --for=condition=Available --timeout=3m
echo "OK: OpenShift-aligned Kuadrant + Istio/Envoy ready in $CTX"
