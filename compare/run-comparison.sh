#!/usr/bin/env bash
# Compare three gateway stacks through one identical KServe LLMInferenceService.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/compare/results/comparison-$(date +%Y%m%d-%H%M%S).md}"
N="${N:-30}"
BODY='{"model":"mock-kserve","messages":[{"role":"user","content":"hello through KServe"}]}'
STREAM='{"model":"mock-kserve","stream":true,"stream_options":{"include_usage":true},"messages":[{"role":"user","content":"hello through KServe"}]}'

condition_status() {
  local context="$1" object="$2" condition="$3"
  kubectl --context "$context" -n ai-demo get "$object" -o json 2>/dev/null | python3 -c '
import json, sys
want = sys.argv[1]
try:
    obj = json.load(sys.stdin)
    conditions = obj.get("status", {}).get("conditions", [])
    if obj.get("kind") == "HTTPRoute":
        conditions = [c for parent in obj.get("status", {}).get("parents", [])
                      for c in parent.get("conditions", [])]
    print("yes" if any(c.get("type") == want and c.get("status") == "True"
                       for c in conditions) else "no")
except Exception:
    print("no")' "$condition"
}

samples() {
  local base="$1" raw status rest elapsed body pod
  for _ in $(seq 1 "$N"); do
    raw=$(curl -sS -m 25 -w '|%{time_total}|%{http_code}' "$base/v1/chat/completions" -H 'content-type: application/json' -d "$BODY")
    status=${raw##*|}
    rest=${raw%|*}
    elapsed=${rest##*|}
    body=${rest%|*}
    pod=unknown
    if [[ "$body" =~ Hello\ from\ ([^[:space:]\(]+) ]]; then
      pod=${BASH_REMATCH[1]}
    fi
    python3 -c "print('$pod', round(float('${elapsed:-0}') * 1000), '$status')"
  done
}

distribution() {
  awk '$3==200 {ok++} $1=="unknown" {unknown++} $1!="unknown" {seen[$1]=1}
       END {for (pod in seen) pods++; printf "%d pods; %d unknown; %d/%d HTTP 200",
       pods+0, unknown+0, ok+0, NR}'
}

p50() {
  awk '{print $2}' | sort -n | awk '{a[NR]=$1} END {if (NR) print a[int((NR+1)/2)] " ms"; else print "n/a"}'
}

stream_usage() {
  local count
  count=$(curl -sS -m 25 "$1/v1/chat/completions" -H 'content-type: application/json' -d "$STREAM" | grep -c '"usage"' || true)
  [[ "$count" -gt 0 ]] && echo yes || echo no
}

ready_replicas() {
  kubectl --context "$1" -n ai-demo get deployment/kserve-mock-kserve -o 'jsonpath={.status.readyReplicas}/{.spec.replicas}' 2>/dev/null || echo n/a
}

owned_resources() {
  kubectl --context "$1" -n ai-demo get deployment,service,inferencepool -o json 2>/dev/null | python3 -c '
import json, sys
try:
    items = json.load(sys.stdin).get("items", [])
    print(sum(any(o.get("kind") == "LLMInferenceService" and o.get("name") == "kserve-mock"
                  for o in item.get("metadata", {}).get("ownerReferences", []))
              for item in items))
except Exception:
    print("n/a")'
}

policy_ready() {
  kubectl --context kind-ai-gw-kuadrant -n ai-demo get ratelimitpolicy/kserve-mock -o json 2>/dev/null | python3 -c '
import json, sys
try:
    conditions = json.load(sys.stdin).get("status", {}).get("conditions", [])
    ready = any(c.get("status") == "True" and c.get("type") in
                ("Accepted", "Available", "Enforced") for c in conditions)
    print("yes" if ready else "no")
except Exception:
    print("no")'
}

echo "collecting KServe comparison ($N requests per gateway)..." >&2
ku_samples=$(samples http://localhost:8082)
ea_samples=$(samples http://localhost:8080)
ag_samples=$(samples http://localhost:8081)

ku_gateway=$(condition_status kind-ai-gw-kuadrant gateway/ai-gateway Programmed)
ea_gateway=$(condition_status kind-ai-gw-envoy gateway/ai-gateway Programmed)
ag_gateway=$(condition_status kind-ai-gw-agent gateway/ai-gateway Programmed)
ku_route="$(condition_status kind-ai-gw-kuadrant httproute/kserve-mock Accepted) / $(condition_status kind-ai-gw-kuadrant httproute/kserve-mock ResolvedRefs)"
ea_route="$(condition_status kind-ai-gw-envoy httproute/kserve-mock Accepted) / $(condition_status kind-ai-gw-envoy httproute/kserve-mock ResolvedRefs)"
ag_route="$(condition_status kind-ai-gw-agent httproute/kserve-mock Accepted) / $(condition_status kind-ai-gw-agent httproute/kserve-mock ResolvedRefs)"
ku_llmisvc=$(condition_status kind-ai-gw-kuadrant llminferenceservice/kserve-mock Ready)
ea_llmisvc=$(condition_status kind-ai-gw-envoy llminferenceservice/kserve-mock Ready)
ag_llmisvc=$(condition_status kind-ai-gw-agent llminferenceservice/kserve-mock Ready)

mkdir -p "$(dirname "$OUT")"
cat > "$OUT" <<EOF
# KServe gateway comparison — $(date -u '+%Y-%m-%d %H:%M UTC')

All environments run KServe v0.20.0 with the same LLMInferenceService,
two-replica CPU runtime, KServe-managed llm-d EPP, InferencePool, and HTTPRoute.
Sample size: $N requests per gateway.

| Check | Kuadrant + Envoy AI Gateway | Envoy AI Gateway | agentgateway |
|---|---|---|---|
| Gateway Programmed | $ku_gateway | $ea_gateway | $ag_gateway |
| LLMInferenceService Ready | $ku_llmisvc | $ea_llmisvc | $ag_llmisvc |
| HTTPRoute Accepted / ResolvedRefs | $ku_route | $ea_route | $ag_route |
| workload replicas ready | $(ready_replicas kind-ai-gw-kuadrant) | $(ready_replicas kind-ai-gw-envoy) | $(ready_replicas kind-ai-gw-agent) |
| KServe-owned Deployments/Services/Pool | $(owned_resources kind-ai-gw-kuadrant) | $(owned_resources kind-ai-gw-envoy) | $(owned_resources kind-ai-gw-agent) |
| endpoint selection and success | $(printf '%s\n' "$ku_samples" | distribution) | $(printf '%s\n' "$ea_samples" | distribution) | $(printf '%s\n' "$ag_samples" | distribution) |
| streaming usage chunk | $(stream_usage http://localhost:8082) | $(stream_usage http://localhost:8080) | $(stream_usage http://localhost:8081) |
| p50 gateway-to-KServe latency | $(printf '%s\n' "$ku_samples" | p50) | $(printf '%s\n' "$ea_samples" | p50) | $(printf '%s\n' "$ag_samples" | p50) |
| Kuadrant RateLimitPolicy ready | $(policy_ready) | n/a | n/a |

## Shared path

    client -> Gateway -> HTTPRoute -> InferencePool
           -> KServe-managed EPP -> selected KServe model pod

Kuadrant is a policy layer. Its column uses Envoy Gateway with the Envoy AI
Gateway InferencePool extension, matching the unprotected Envoy column while
adding Kuadrant policy. Latency is a local smoke-test against a zero-delay
mock, not a production benchmark.
EOF

echo "wrote $OUT" >&2
cat "$OUT"
