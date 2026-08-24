#!/usr/bin/env bash
# Reconcile the same KServe-managed LLM workload and routing topology into all
# three gateway comparison clusters.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

wait_for_route_accepted() {
  local ctx="$1" deadline=$((SECONDS + 180)) accepted
  while ((SECONDS < deadline)); do
    accepted="$(kubectl --context "$ctx" -n ai-demo get httproute kserve-mock \
      -o 'jsonpath={range .status.parents[*].conditions[?(@.type=="Accepted")]}{.status}{"\n"}{end}' 2>/dev/null || true)"
    if grep -qx True <<<"$accepted"; then
      echo "httproute.gateway.networking.k8s.io/kserve-mock accepted"
      return 0
    fi
    sleep 2
  done
  kubectl --context "$ctx" -n ai-demo get httproute kserve-mock -o yaml >&2
  return 1
}

wait_for_deployment() {
  local ctx="$1" name="$2" deadline=$((SECONDS + 180))
  while ((SECONDS < deadline)); do
    if kubectl --context "$ctx" -n ai-demo get deployment "$name" >/dev/null 2>&1; then
      kubectl --context "$ctx" -n ai-demo rollout status \
        "deployment/$name" --timeout=5m
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for deployment/$name to be created in $ctx" >&2
  kubectl --context "$ctx" -n ai-demo get llminferenceservice kserve-mock -o yaml >&2
  return 1
}

for ctx in kind-ai-gw-kuadrant kind-ai-gw-envoy kind-ai-gw-agent; do
  echo "==> KServe LLMInferenceService resources in $ctx"
  kubectl --context "$ctx" apply -f "$ROOT/kserve/manifests/cpu-presets.yaml"
  kubectl --context "$ctx" apply -f "$ROOT/kserve/manifests/route.yaml"
  kubectl --context "$ctx" apply -f "$ROOT/kserve/manifests/llmisvc.yaml"
  wait_for_deployment "$ctx" kserve-mock-kserve
  wait_for_deployment "$ctx" kserve-mock-kserve-router-scheduler
  wait_for_route_accepted "$ctx"
  kubectl --context "$ctx" -n ai-demo wait llminferenceservice/kserve-mock \
    --for=condition=Ready --timeout=5m
done

echo "OK: KServe LLMInferenceService path ready in all clusters"
