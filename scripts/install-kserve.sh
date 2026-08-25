#!/usr/bin/env bash
# Install the standalone KServe LLMInferenceService controller in one
# comparison cluster. The selected gateway stack already provides Gateway API
# and Gateway API Inference Extension CRDs.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KSERVE_VERSION="${KSERVE_VERSION:-v0.20.0}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.17.0}"
ctx="${1:?usage: install-kserve.sh <kubectl-context>}"
case "$ctx" in kind-ai-gw-kuadrant|kind-ai-gw-envoy|kind-ai-gw-agent) ;; *) echo "unsupported context: $ctx" >&2; exit 2 ;; esac

H=(helm --kube-context "$ctx")

echo "==> cert-manager $CERT_MANAGER_VERSION in $ctx"
"${H[@]}" upgrade -i cert-manager \
  "$ROOT/kserve/charts/cert-manager-${CERT_MANAGER_VERSION}.tgz" \
  --namespace cert-manager --create-namespace \
  --set crds.enabled=true --wait --timeout 5m

echo "==> KServe LLMInferenceService CRDs $KSERVE_VERSION in $ctx"
"${H[@]}" upgrade -i kserve-llmisvc-crd \
  "$ROOT/kserve/charts/kserve-llmisvc-crd-${KSERVE_VERSION}.tgz" \
  --namespace kserve --create-namespace --wait --timeout 5m

echo "==> KServe LLMInferenceService controller $KSERVE_VERSION in $ctx"
"${H[@]}" upgrade -i kserve-llmisvc-resources \
  "$ROOT/kserve/charts/kserve-llmisvc-resources-${KSERVE_VERSION}.tgz" \
  --namespace kserve --create-namespace \
  -f "$ROOT/kserve/values/kserve-llmisvc.values.yaml" \
  --wait --timeout 5m

kubectl --context "$ctx" -n kserve rollout status \
  deployment/llmisvc-controller-manager --timeout=3m

echo "OK: KServe LLMInferenceService controller ready in $ctx"
