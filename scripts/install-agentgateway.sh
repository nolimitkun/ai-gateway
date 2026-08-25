#!/usr/bin/env bash
# Install the agentgateway stack into kind-ai-gw-agent.
# agentgateway v1.4.1 is self-contained: it ships its own controller and
# GatewayClass ("agentgateway"), so no additional Gateway provider is required.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $# -eq 1 ]] || { echo "usage: $0 kind-ai-gw-agent" >&2; exit 2; }
CTX="$1"
[[ "$CTX" == kind-ai-gw-agent ]] || { echo "install-agentgateway.sh only supports kind-ai-gw-agent" >&2; exit 2; }
GWAPI_VERSION="${GWAPI_VERSION:-1.6.0}"
GAIE_VERSION="${GAIE_VERSION:-v1.5.0}"
AGW_VERSION="${AGW_VERSION:-v1.4.1}"
H="helm --kube-context $CTX"

echo "==> [1/4] Gateway API CRDs v${GWAPI_VERSION} (standard channel)"
kubectl --context "$CTX" apply --server-side -f \
  "https://github.com/kubernetes-sigs/gateway-api/releases/download/v${GWAPI_VERSION}/standard-install.yaml"

echo "==> [2/4] Gateway API Inference Extension CRDs $GAIE_VERSION"
kubectl --context "$CTX" apply --server-side -f \
  "https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/${GAIE_VERSION}/v1-manifests.yaml"

echo "==> [3/4] agentgateway CRDs $AGW_VERSION"
$H upgrade -i agentgateway-crds "$ROOT/agentgateway/charts/agentgateway-crds-${AGW_VERSION}.tgz" \
  --namespace agentgateway-system --create-namespace --wait --timeout 5m

echo "==> [4/4] agentgateway control plane $AGW_VERSION"
$H upgrade -i agentgateway "$ROOT/agentgateway/charts/agentgateway-${AGW_VERSION}.tgz" \
  --namespace agentgateway-system --create-namespace \
  -f "$ROOT/agentgateway/values/agentgateway.values.yaml" --wait --timeout 5m

kubectl --context "$CTX" wait --timeout=3m -n agentgateway-system \
  deployment --all --for=condition=Available
echo "OK: agentgateway stack ready in $CTX"
