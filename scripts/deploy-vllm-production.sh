#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/component-env.sh"

usage() {
  echo "usage: $0 <kubectl-context> [--delete]" >&2
  exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
context=$1
action=${2:-}
[[ -z "$action" || "$action" == "--delete" ]] || usage

# Known comparison contexts use their self-contained gateway tree. Preserve
# the former shared path for arbitrary external GPU contexts.
PRODUCTION_MANIFESTS="$ROOT/kserve/production"
REQUIRED_POLICY=""
OBSOLETE_PRODUCTION_POLICIES=()
case "$context" in
  kind-ai-gw-kuadrant)
    select_context_components "$context"
    PRODUCTION_MANIFESTS="$COMPONENT_ROOT/kserve/production"
    REQUIRED_POLICY=authpolicy/kserve-mock
    ;;
  kind-ai-gw-envoy)
    select_context_components "$context"
    PRODUCTION_MANIFESTS="$COMPONENT_ROOT/kserve/production"
    REQUIRED_POLICY=securitypolicy/kserve-mock
    OBSOLETE_PRODUCTION_POLICIES=(
      securitypolicy/vllm-big-tier
      securitypolicy/vllm-small-medium
    )
    ;;
  kind-ai-gw-agent)
    select_context_components "$context"
    PRODUCTION_MANIFESTS="$COMPONENT_ROOT/kserve/production"
    REQUIRED_POLICY=agentgatewaypolicy/kserve-mock-jwt
    OBSOLETE_PRODUCTION_POLICIES=(
      agentgatewaypolicy/vllm-big-tier
      agentgatewaypolicy/vllm-members
    )
    ;;
esac

kubectl --context "$context" cluster-info >/dev/null
if [[ "$action" == "--delete" ]]; then
  kubectl --context "$context" delete -k "$PRODUCTION_MANIFESTS" --ignore-not-found
  exit
fi

gpu_nodes=$(kubectl --context "$context" get nodes \
  -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' | grep -Ec '^[1-9][0-9]*$' || true)
if [[ "$gpu_nodes" -eq 0 ]]; then
  echo "no node in $context advertises allocatable nvidia.com/gpu" >&2
  exit 1
fi

# The three repository-owned gateway contexts promise the same authenticated
# tier contract. Do not install open production routes there when the shared
# Keycloak policy layer has not been reconciled first. Arbitrary external
# contexts retain the compatibility bundle and own their security integration.
if [[ -n "$REQUIRED_POLICY" ]] &&
   ! kubectl --context "$context" -n ai-demo get "$REQUIRED_POLICY" >/dev/null 2>&1; then
  cluster=${context#kind-}
  echo "production routes require the gateway policy layer; run 'make policies CLUSTER=$cluster' first" >&2
  exit 1
fi

for obsolete_policy in "${OBSOLETE_PRODUCTION_POLICIES[@]}"; do
  kubectl --context "$context" -n ai-demo delete "$obsolete_policy" --ignore-not-found
done

# Apply the overlay, not the files it lists. The Kuadrant profile provisions no
# ai-demo/ai-gateway -- its only Gateway is openshift-ingress/openshift-ai-inference
# -- so its production routes and services are attached by kustomize patches,
# exactly as its base and pools trees are. `kubectl apply -f` bypasses those
# patches, leaving every route unattached and this script's Ready wait to time
# out thirty minutes later. The --delete path already uses -k.
#
# kustomize emits LLMInferenceServiceConfig and the routes ahead of the
# LLMInferenceServices that reference them in baseRefs and route refs, which is
# the ordering the previous file-by-file sequence was written to guarantee.
kubectl --context "$context" apply -k "$PRODUCTION_MANIFESTS"
kubectl --context "$context" -n ai-demo wait llminferenceservice \
  --for=condition=Ready --timeout=30m \
  vllm-chat vllm-embedding vllm-rerank vllm-transcription
