#!/usr/bin/env bash
# Re-pull the helm charts and refresh the vendored default values.
# Charts are vendored as .tgz so installs are reproducible; this script is how
# you move to a new upstream release.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EG_VERSION="${EG_VERSION:-v1.8.1}"
AIEG_VERSION="${AIEG_VERSION:-v1.0.0}"
AGW_VERSION="${AGW_VERSION:-v1.4.1}"

helm pull oci://docker.io/envoyproxy/gateway-helm          --version "$EG_VERSION"   -d "$ROOT/envoy-ai-gateway/charts"
helm pull oci://docker.io/envoyproxy/ai-gateway-crds-helm  --version "$AIEG_VERSION" -d "$ROOT/envoy-ai-gateway/charts"
helm pull oci://docker.io/envoyproxy/ai-gateway-helm       --version "$AIEG_VERSION" -d "$ROOT/envoy-ai-gateway/charts"
helm pull oci://cr.agentgateway.dev/charts/agentgateway-crds --version "$AGW_VERSION" -d "$ROOT/agentgateway/charts"
helm pull oci://cr.agentgateway.dev/charts/agentgateway      --version "$AGW_VERSION" -d "$ROOT/agentgateway/charts"

curl -fsSL https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/manifests/envoy-gateway-values.yaml \
  -o "$ROOT/envoy-ai-gateway/values/envoy-gateway.values.yaml"

for side in envoy-ai-gateway agentgateway; do
  for t in "$ROOT/$side"/charts/*.tgz; do
    helm show values "$t" > "$ROOT/$side/values/$(basename "$t" .tgz).default.yaml"
  done
done
echo "charts and default values refreshed"
