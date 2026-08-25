#!/usr/bin/env bash
# Deploy one KServe serving pool per accelerator class into one gateway
# cluster. The shared kserve-mock service stays in place: it keeps serving the
# CPU fixture models and every request that arrives without an x-model-class
# header. Run scripts/deploy-kserve.sh or `make up CLUSTER=<name>` first.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

if $DELETE; then
  echo "==> removing accelerator pools from $ctx"
  if [[ "$ctx" == "kind-ai-gw-kuadrant" ]]; then
    kubectl --context "$ctx" delete -k "$ROOT/kuadrant/pools-overlay" --ignore-not-found
  else
    kubectl --context "$ctx" delete -k "$ROOT/kserve/pools" --ignore-not-found
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

echo "==> accelerator pools in $ctx"
require_base "$ctx"
if [[ "$ctx" == "kind-ai-gw-kuadrant" ]]; then
  kubectl --context "$ctx" apply -k "$ROOT/kuadrant/pools-overlay"
  for pool in "${POOLS[@]}"; do
    kubectl --context "$ctx" -n ai-demo wait "certificate/kserve-$pool-epp-server" \
      --for=condition=Ready --timeout=3m
  done
else
  kubectl --context "$ctx" apply -k "$ROOT/kserve/pools"
fi
for pool in "${POOLS[@]}"; do
  wait_for_deployment "$ctx" "kserve-$pool-kserve"
  wait_for_deployment "$ctx" "kserve-$pool-kserve-router-scheduler"
  wait_for_route_accepted "$ctx" "kserve-$pool"
  kubectl --context "$ctx" -n ai-demo wait "llminferenceservice/kserve-$pool" \
    --for=condition=Ready --timeout=5m
  if [[ "$ctx" == "kind-ai-gw-kuadrant" ]]; then
    kubectl --context "$ctx" -n ai-demo wait "backendtlspolicy/kserve-$pool-epp" \
      --for=jsonpath='{.status.ancestors[0].conditions[0].status}'=True --timeout=3m
  fi
done

# Each pool answers only for the models its cards are sized for, so the served
# accelerator class is checked from the response itself.
declare -A FLAGSHIP=(
  [b300]=kimi-k3
  [h200]=deepseek-v4-flash
  [h100]=qwen3.8-27b
  [l40s]=bge-m3
)

check_pool() {
  local base="$1" pool="$2" model="${FLAGSHIP[$2]}" path='/v1/chat/completions' body
  if [[ "$pool" == l40s ]]; then
    path='/v1/embeddings'
    body="{\"model\":\"$model\",\"input\":\"gateway inference\"}"
  else
    body="{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}"
  fi
  curl -sS --fail-with-body -m 25 "$base$path" \
    "${AUTH_HEADER[@]}" \
    -H 'content-type: application/json' \
    -H "x-model-class: $pool" \
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

AUTH_HEADER=()
token="$(curl -sS -m 20 "$base/realms/ai-gateway/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=ai-gateway-cli -d username=alice -d password=alice 2>/dev/null |
  python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("access_token", ""))
except Exception: print("")')"
[[ -z "$token" ]] || AUTH_HEADER=(-H "authorization: Bearer $token")
echo "==> $base"
for pool in "${POOLS[@]}"; do
  check_pool "$base" "$pool"
done

echo "OK: B300, H200, H100, and L40S pools ready in $ctx"
