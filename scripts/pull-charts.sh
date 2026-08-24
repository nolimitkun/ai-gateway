#!/usr/bin/env bash
# Refresh all vendored charts used by the three KServe environments.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EG_VERSION="${EG_VERSION:-v1.8.1}"
AIEG_VERSION="${AIEG_VERSION:-v1.0.0}"
AGW_VERSION="${AGW_VERSION:-v1.4.1}"
KUADRANT_VERSION="${KUADRANT_VERSION:-1.5.2}"
KSERVE_VERSION="${KSERVE_VERSION:-v0.20.0}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.17.0}"

mkdir -p "$ROOT/kuadrant/charts"
helm pull oci://docker.io/envoyproxy/gateway-helm --version "$EG_VERSION" -d "$ROOT/envoy-ai-gateway/charts"
helm pull oci://docker.io/envoyproxy/ai-gateway-crds-helm --version "$AIEG_VERSION" -d "$ROOT/envoy-ai-gateway/charts"
helm pull oci://docker.io/envoyproxy/ai-gateway-helm --version "$AIEG_VERSION" -d "$ROOT/envoy-ai-gateway/charts"
helm pull oci://cr.agentgateway.dev/charts/agentgateway-crds --version "$AGW_VERSION" -d "$ROOT/agentgateway/charts"
helm pull oci://cr.agentgateway.dev/charts/agentgateway --version "$AGW_VERSION" -d "$ROOT/agentgateway/charts"
helm pull kuadrant-operator --repo https://kuadrant.io/helm-charts/ --version "$KUADRANT_VERSION" -d "$ROOT/kuadrant/charts"
helm pull cert-manager --repo https://charts.jetstack.io --version "$CERT_MANAGER_VERSION" -d "$ROOT/kserve/charts"
helm pull oci://ghcr.io/kserve/charts/kserve-llmisvc-crd --version "$KSERVE_VERSION" -d "$ROOT/kserve/charts"
helm pull oci://ghcr.io/kserve/charts/kserve-llmisvc-resources --version "$KSERVE_VERSION" -d "$ROOT/kserve/charts"

echo "charts refreshed"
