#!/usr/bin/env bash
# Reconcile the KServe-managed LLM workload and routing topology into one
# selected gateway comparison cluster.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/component-env.sh"
ctx="${1:?usage: deploy-kserve.sh <kubectl-context>}"
case "$ctx" in kind-ai-gw-kuadrant|kind-ai-gw-envoy|kind-ai-gw-agent) ;; *) echo "unsupported context: $ctx" >&2; exit 2 ;; esac
select_context_components "$ctx"
KSERVE_BASE="$COMPONENT_ROOT/kserve/base"
KSERVE_POOLS="$COMPONENT_ROOT/kserve/pools"
BBR_MANIFESTS="$COMPONENT_ROOT/llm-d"
SEMANTIC_MANIFESTS="$COMPONENT_ROOT/semantic-router"
pools_present=false
if kubectl --context "$ctx" -n ai-demo get llminferenceservice kserve-b300 >/dev/null 2>&1; then
  pools_present=true
fi

# A retained Kind node can report Ready before the KServe controller has
# reacquired its lease and opened the admission webhook. Applying an LLMI in
# that window fails with connection refused, so gate every reconcile here.
kubectl --context "$ctx" -n kserve rollout status \
  deployment/llmisvc-controller-manager --timeout=5m

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
        "deployment/$name" --timeout=10m
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for deployment/$name to be created in $ctx" >&2
  kubectl --context "$ctx" -n ai-demo get llminferenceservice kserve-mock -o yaml >&2
  return 1
}

wait_for_policy() {
  local ctx="$1" object="$2" deadline=$((SECONDS + 180))
  while ((SECONDS < deadline)); do
    if kubectl --context "$ctx" -n ai-demo get "$object" -o json 2>/dev/null |
      python3 -c '
import json, sys
status = json.load(sys.stdin).get("status", {})
conditions = list(status.get("conditions", []))
for key in ("ancestors", "parents"):
    for parent in status.get(key, []):
        conditions.extend(parent.get("conditions", []))
raise SystemExit(0 if any(
    item.get("type") == "Accepted" and item.get("status") == "True"
    for item in conditions
) else 1)
'; then
      echo "$object Accepted"
      return 0
    fi
    sleep 3
  done
  kubectl --context "$ctx" -n ai-demo get "$object" -o yaml >&2
  return 1
}

echo "==> KServe LLMInferenceService resources in $ctx"
if [[ "$ctx" == "kind-ai-gw-kuadrant" ]]; then
  if $pools_present; then
    kubectl --context "$ctx" apply -k "$KSERVE_POOLS"
  else
    kubectl --context "$ctx" apply -k "$KSERVE_BASE"
  fi
  kubectl --context "$ctx" -n ai-demo wait certificate/kserve-mock-epp-root-ca \
    --for=condition=Ready --timeout=3m
  kubectl --context "$ctx" -n ai-demo wait certificate/kserve-mock-epp-server \
    --for=condition=Ready --timeout=3m
else
  kubectl --context "$ctx" apply -f "$KSERVE_BASE/cpu-presets.yaml"
  if $pools_present; then
    kubectl --context "$ctx" apply -f "$KSERVE_POOLS/route.yaml"
  else
    kubectl --context "$ctx" apply -f "$KSERVE_BASE/route.yaml"
  fi
  kubectl --context "$ctx" apply -f "$KSERVE_BASE/llmisvc.yaml"
fi

echo "==> OpenAI body.model routing in $ctx"
kubectl --context "$ctx" apply -f "$BBR_MANIFESTS/body-based-router.yaml"
wait_for_deployment "$ctx" body-based-router
case "$ctx" in
  kind-ai-gw-kuadrant)
    kubectl --context "$ctx" apply -f "$BBR_MANIFESTS/kuadrant-extproc.yaml"
    ;;
  kind-ai-gw-envoy)
    kubectl --context "$ctx" apply -f "$BBR_MANIFESTS/envoy-backend.yaml"
    # Migration from the former standalone semantic-router policy. It targets
    # the same chat section and Envoy Gateway correctly marks both policies
    # conflicted if the old object is left behind.
    kubectl --context "$ctx" -n ai-demo delete \
      envoyextensionpolicy/semantic-router --ignore-not-found
    # Preserve the optional semantic-router chain during an idempotent KServe
    # reconcile; otherwise install the normal explicit-model chat processor.
    if kubectl --context "$ctx" -n ai-demo get deployment semantic-router >/dev/null 2>&1; then
      if $pools_present; then
        kubectl --context "$ctx" apply -f "$SEMANTIC_MANIFESTS/envoy-extproc-pools.yaml"
      else
        kubectl --context "$ctx" apply -f "$SEMANTIC_MANIFESTS/envoy-extproc.yaml"
      fi
    else
      if $pools_present; then
        kubectl --context "$ctx" apply -f "$BBR_MANIFESTS/envoy-chat-extproc-pools.yaml"
      else
        kubectl --context "$ctx" apply -f "$BBR_MANIFESTS/envoy-chat-extproc.yaml"
      fi
    fi
    if $pools_present; then
      kubectl --context "$ctx" apply -f "$BBR_MANIFESTS/envoy-task-extproc-pools.yaml"
    else
      kubectl --context "$ctx" apply -f "$BBR_MANIFESTS/envoy-task-extproc.yaml"
    fi
    wait_for_policy "$ctx" envoyextensionpolicy/model-body-router-chat
    wait_for_policy "$ctx" envoyextensionpolicy/model-body-router-tasks
    ;;
  kind-ai-gw-agent)
    kubectl --context "$ctx" -n ai-demo delete \
      agentgatewaypolicy/body-based-router-tls --ignore-not-found
    # This file carries the AgentgatewayBackend as well as the base policy, so
    # it is applied either way; the semantic chain then replaces the policy.
    kubectl --context "$ctx" apply -f "$BBR_MANIFESTS/agentgateway-extproc.yaml"
    # Preserve the optional semantic-router chain during an idempotent KServe
    # reconcile. It replaces model-body-router under the same name, so
    # re-applying the base file above would silently undo it and send `auto`
    # back to the CPU fixture.
    if kubectl --context "$ctx" -n ai-demo get deployment semantic-router >/dev/null 2>&1; then
      kubectl --context "$ctx" apply -f "$SEMANTIC_MANIFESTS/agentgateway-extproc.yaml"
    fi
    wait_for_policy "$ctx" agentgatewaypolicy/model-body-router
    ;;
esac
wait_for_deployment "$ctx" kserve-mock-kserve
wait_for_deployment "$ctx" kserve-mock-kserve-router-scheduler
wait_for_route_accepted "$ctx"
kubectl --context "$ctx" -n ai-demo wait llminferenceservice/kserve-mock \
  --for=condition=Ready --timeout=5m
if [[ "$ctx" == "kind-ai-gw-kuadrant" ]]; then
  kubectl --context "$ctx" -n ai-demo get secret kserve-mock-epp-tls -o json | python3 -c '
import base64, json, sys
secret = json.load(sys.stdin)
print(json.dumps({
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "kserve-mock-epp-ca", "namespace": "ai-demo"},
    "data": {"ca.crt": base64.b64decode(secret["data"]["ca.crt"]).decode()},
}))' | kubectl --context "$ctx" apply -f -
  kubectl --context "$ctx" apply -f "$KSERVE_BASE/epp-tls.yaml"
  kubectl --context "$ctx" -n ai-demo wait backendtlspolicy/kserve-mock-epp \
    --for=jsonpath='{.status.ancestors[0].conditions[0].status}'=True --timeout=3m
fi

echo "OK: KServe LLMInferenceService path ready in $ctx"
