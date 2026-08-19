#!/usr/bin/env bash
# Install the agentgateway stack into kind-ai-gw-agent.
# agentgateway v1.4.1 is self-contained: it ships its own controller and
# GatewayClass ("agentgateway"), so no kgateway control plane is required.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTX="kind-ai-gw-agent"
GWAPI_VERSION="${GWAPI_VERSION:-1.6.0}"
AGW_VERSION="${AGW_VERSION:-v1.4.1}"
H="helm --kube-context $CTX"

echo "==> [1/3] Gateway API CRDs v${GWAPI_VERSION} (standard channel)"
kubectl --context "$CTX" apply --server-side -f \
  "https://github.com/kubernetes-sigs/gateway-api/releases/download/v${GWAPI_VERSION}/standard-install.yaml"

echo "==> [2/3] agentgateway CRDs $AGW_VERSION"
$H upgrade -i agentgateway-crds "$ROOT/agentgateway/charts/agentgateway-crds-${AGW_VERSION}.tgz" \
  --namespace agentgateway-system --create-namespace --wait --timeout 5m

echo "==> [3/3] agentgateway control plane $AGW_VERSION"
$H upgrade -i agentgateway "$ROOT/agentgateway/charts/agentgateway-${AGW_VERSION}.tgz" \
  --namespace agentgateway-system --create-namespace \
  -f "$ROOT/agentgateway/values/agentgateway.values.yaml" --wait --timeout 5m

kubectl --context "$CTX" wait --timeout=3m -n agentgateway-system \
  deployment --all --for=condition=Available
echo "OK: agentgateway stack ready in $CTX"
