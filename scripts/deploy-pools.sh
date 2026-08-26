#!/usr/bin/env bash
# Deploy one KServe serving pool per accelerator class into one gateway
# cluster. The shared kserve-mock service stays in place: it keeps serving the
# CPU fixture models and requests whose body.model has no accelerator mapping.
# The base KServe deployment also installs the official BBR processor that
# derives the internal routing header. Run `make up CLUSTER=<name>` first.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/component-env.sh"
POOLS=(b300 h200 h100 l40s)
DELETE=false
ctx=""
base=""
while (($#)); do
  case "$1" in
    --delete) DELETE=true; shift ;;
    --context)
      [[ -n "${2:-}" ]] || { echo "--context requires a kubectl context" >&2; exit 2; }
      ctx="$2"
      shift 2
      ;;
    --base-url)
      [[ -n "${2:-}" ]] || { echo "--base-url requires a URL" >&2; exit 2; }
      base="$2"
      shift 2
      ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$ctx" in kind-ai-gw-kuadrant) base="${base:-http://localhost:8082}" ;; kind-ai-gw-envoy) base="${base:-http://localhost:8080}" ;; kind-ai-gw-agent) base="${base:-http://localhost:8081}" ;; *) echo "--context must name one comparison context" >&2; exit 2 ;; esac
select_context_components "$ctx"
KSERVE_BASE="$COMPONENT_ROOT/kserve/base"
KSERVE_POOLS="$COMPONENT_ROOT/kserve/pools"
LLMD_MANIFESTS="$COMPONENT_ROOT/llm-d"
SEMANTIC_MANIFESTS="$COMPONENT_ROOT/semantic-router"

if $DELETE; then
  echo "==> removing accelerator pools from $ctx"
  if [[ "$ctx" == "kind-ai-gw-kuadrant" ]]; then
    kubectl --context "$ctx" delete -k "$KSERVE_POOLS" --ignore-not-found
    # The pool overlay owns the expanded form of the shared route. Reconcile
    # its base form after deleting the optional pools.
    kubectl --context "$ctx" apply -k "$KSERVE_BASE"
  else
    kubectl --context "$ctx" delete -k "$KSERVE_POOLS" --ignore-not-found
    kubectl --context "$ctx" apply -f "$KSERVE_BASE/route.yaml"
    if [[ "$ctx" == "kind-ai-gw-envoy" ]]; then
      if kubectl --context "$ctx" -n ai-demo get deployment semantic-router >/dev/null 2>&1; then
        kubectl --context "$ctx" apply -f "$SEMANTIC_MANIFESTS/envoy-extproc.yaml"
      else
        kubectl --context "$ctx" apply -f "$LLMD_MANIFESTS/envoy-chat-extproc.yaml"
      fi
      kubectl --context "$ctx" apply -f "$LLMD_MANIFESTS/envoy-task-extproc.yaml"
    fi
  fi
  echo "OK: accelerator pools removed; the shared KServe path is untouched"
  exit 0
fi

require_base() {
  local ctx="$1"
  if ! kubectl --context "$ctx" -n ai-demo get configmap mock-llm-src >/dev/null 2>&1; then
    echo "mock-llm-src is missing in $ctx; run 'make runtime CLUSTER=<name>' first" >&2
    return 1
  fi
  if ! kubectl --context "$ctx" -n ai-demo get deployment body-based-router >/dev/null 2>&1; then
    echo "body-based-router is missing in $ctx; run 'make kserve CLUSTER=<name>' first" >&2
    return 1
  fi
  if [[ "$ctx" == "kind-ai-gw-kuadrant" ]] &&
     ! kubectl --context "$ctx" -n ai-demo get configmap kserve-mock-epp-ca >/dev/null 2>&1; then
    echo "kserve-mock-epp-ca is missing in $ctx; run 'make kserve CLUSTER=ai-gw-kuadrant' first" >&2
    return 1
  fi
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
  return 1
}

wait_for_route_accepted() {
  local ctx="$1" route="$2" deadline=$((SECONDS + 180)) accepted
  while ((SECONDS < deadline)); do
    accepted="$(kubectl --context "$ctx" -n ai-demo get httproute "$route" \
      -o 'jsonpath={range .status.parents[*].conditions[?(@.type=="Accepted")]}{.status}{"\n"}{end}' 2>/dev/null || true)"
    if grep -qx True <<<"$accepted"; then
      echo "httproute.gateway.networking.k8s.io/$route accepted"
      return 0
    fi
    sleep 2
  done
  kubectl --context "$ctx" -n ai-demo get httproute "$route" -o yaml >&2
  return 1
}

wait_for_object() {
  local ctx="$1" object="$2" deadline=$((SECONDS + 180))
  while ((SECONDS < deadline)); do
    if kubectl --context "$ctx" -n ai-demo get "$object" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for $object in $ctx" >&2
  return 1
}

echo "==> accelerator pools in $ctx"
require_base "$ctx"
if [[ "$ctx" == "kind-ai-gw-kuadrant" ]]; then
  kubectl --context "$ctx" apply -k "$KSERVE_POOLS"
  for pool in "${POOLS[@]}"; do
    kubectl --context "$ctx" -n ai-demo wait "certificate/kserve-$pool-epp-server" \
      --for=condition=Ready --timeout=3m
  done
else
  kubectl --context "$ctx" apply -k "$KSERVE_POOLS"
fi

# Envoy Gateway policies target named route sections. Once pool-specific
# sections exist, cover them too so a forged internal header cannot bypass BBR
# on the request's initial route selection.
if [[ "$ctx" == "kind-ai-gw-envoy" ]]; then
  if kubectl --context "$ctx" -n ai-demo get deployment semantic-router >/dev/null 2>&1; then
    kubectl --context "$ctx" apply -f "$SEMANTIC_MANIFESTS/envoy-extproc-pools.yaml"
  else
    kubectl --context "$ctx" apply -f "$LLMD_MANIFESTS/envoy-chat-extproc-pools.yaml"
  fi
  kubectl --context "$ctx" apply -f "$LLMD_MANIFESTS/envoy-task-extproc-pools.yaml"
fi

# The shared route is applied in the same transaction as four
# LLMInferenceServices, before their generated pools necessarily exist. KServe
# can retain that first BackendNotFound observation because the pool event does
# not change the referenced route. Once every pool exists, annotate each owner
# with the route generation to trigger one deterministic reconciliation.
for pool in "${POOLS[@]}"; do
  wait_for_object "$ctx" "inferencepool/kserve-$pool-inference-pool"
done
route_generation="$(kubectl --context "$ctx" -n ai-demo get httproute kserve-mock \
  -o jsonpath='{.metadata.generation}')"
for pool in "${POOLS[@]}"; do
  kubectl --context "$ctx" -n ai-demo annotate --overwrite \
    "llminferenceservice/kserve-$pool" \
    "ai-gateway.mock/route-generation=$route_generation"
done
for pool in "${POOLS[@]}"; do
  wait_for_deployment "$ctx" "kserve-$pool-kserve"
  wait_for_deployment "$ctx" "kserve-$pool-kserve-router-scheduler"
  kubectl --context "$ctx" -n ai-demo wait "llminferenceservice/kserve-$pool" \
    --for=condition=Ready --timeout=5m
  if [[ "$ctx" == "kind-ai-gw-kuadrant" ]]; then
    kubectl --context "$ctx" -n ai-demo wait "backendtlspolicy/kserve-$pool-epp" \
      --for=jsonpath='{.status.ancestors[0].conditions[0].status}'=True --timeout=3m
  fi
done
wait_for_route_accepted "$ctx" kserve-mock

# Each pool answers only for the models its cards are sized for. No accelerator
# header is sent: body.model must be sufficient to reach the right pool.
check_pool() {
  local base="$1" pool="$2" path='/v1/chat/completions' body
  local model
  case "$pool" in
    b300) model=kimi-k3 ;;
    h200) model=deepseek-v4-flash ;;
    h100) model=qwen3.8-27b ;;
    l40s) model=bge-m3 ;;
    *) echo "unknown pool: $pool" >&2; return 2 ;;
  esac
  if [[ "$pool" == l40s ]]; then
    path='/v1/embeddings'
    body="{\"model\":\"$model\",\"input\":\"gateway inference\"}"
  else
    body="{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}"
  fi
  curl -sS --fail-with-body -m 25 "$base$path" \
    "${REQUEST_HEADERS[@]}" \
    -d "$body" |
    python3 -c "
import json, sys
try:
    body = json.load(sys.stdin)
except Exception as error:
    print('$pool -> invalid response: ' + str(error), file=sys.stderr)
    raise SystemExit(1)
accelerator = body.get('mock_accelerator')
pod = body.get('mock_pod', '')
if accelerator != '$pool' or not pod.startswith('kserve-$pool-'):
    print(
        '$pool -> expected accelerator $pool on a kserve-$pool pod, got ' +
        str(accelerator) + ' on ' + str(pod or 'unknown'),
        file=sys.stderr,
    )
    raise SystemExit(1)
print('$pool -> ' + accelerator + ' on ' + pod)"
}

REQUEST_HEADERS=(-H 'content-type: application/json')
case "$ctx" in
  kind-ai-gw-kuadrant) auth_object=authpolicy/kserve-mock ;;
  kind-ai-gw-envoy) auth_object=securitypolicy/kserve-mock ;;
  kind-ai-gw-agent) auth_object=agentgatewaypolicy/kserve-mock-jwt ;;
esac
auth_required=false
if kubectl --context "$ctx" -n ai-demo get "$auth_object" >/dev/null 2>&1; then
  auth_required=true
fi
token=""
deadline=$((SECONDS + 300))
while [[ -z "$token" ]] && ((SECONDS < deadline)); do
  token="$(curl -sS -m 20 "$base/realms/ai-gateway/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=ai-gateway-cli -d username=alice -d password=alice 2>/dev/null |
    python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("access_token", ""))
except Exception: print("")')"
  $auth_required || break
  [[ -n "$token" ]] || sleep 3
done
if $auth_required && [[ -z "$token" ]]; then
  echo "authentication policy exists but Keycloak did not issue a probe token" >&2
  exit 1
fi
[[ -z "$token" ]] || REQUEST_HEADERS+=(-H "authorization: Bearer $token")
echo "==> $base"
for pool in "${POOLS[@]}"; do
  check_pool "$base" "$pool"
done

# A client-supplied internal header must not override a valid body.model. The
# final accelerator proves the route-cache recomputation used BBR's value; the
# KServe EPP consumes this internal header and need not forward it downstream.
curl -sS --fail-with-body -m 25 "$base/v1/chat/completions" \
  "${REQUEST_HEADERS[@]}" \
  -H 'x-gateway-model-name: qwen3.8-27b' \
  -d '{"model":"kimi-k3","messages":[{"role":"user","content":"hello"}]}' |
  python3 -c '
import json, sys
body = json.load(sys.stdin)
if body.get("mock_accelerator") != "b300":
    raise SystemExit("client header overrode body.model: " + str(body))
print("client header spoof -> overwritten by body.model")'

echo "OK: body.model routes B300, H200, H100, and L40S pools in $ctx"
