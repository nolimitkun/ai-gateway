#!/usr/bin/env bash
# Compare the model catalog, chat, embeddings, reranking, and STT through one
# KServe service.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/compare/results/comparison-$(date +%Y%m%d-%H%M%S).md}"
N="${N:-30}"
BODY='{"model":"mock-kserve","messages":[{"role":"user","content":"hello through KServe"}]}'
STREAM='{"model":"mock-kserve","stream":true,"stream_options":{"include_usage":true},"messages":[{"role":"user","content":"hello through KServe"}]}'

condition_status() {
  local context="$1" object="$2" condition="$3" namespace="${4:-ai-demo}"
  kubectl --context "$context" -n "$namespace" get "$object" -o json 2>/dev/null | python3 -c '
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

embeddings_api() {
  local response
  response=$(curl -sS -m 25 "$1/v1/embeddings" \
    -H 'content-type: application/json' \
    -d '{"model":"mock-embedding","input":["gateway inference","KServe routing"]}' || true)
  printf '%s' "$response" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
    data = body.get("data", [])
    valid = (body.get("object") == "list" and len(data) == 2 and
             all(item.get("object") == "embedding" and
                 len(item.get("embedding", [])) == 8 for item in data))
    print("yes" if valid else "no")
except Exception:
    print("no")'
}

rerank_api() {
  local response
  response=$(curl -sS -m 25 "$1/v1/rerank" \
    -H 'content-type: application/json' \
    -d '{"model":"mock-reranker","query":"gateway inference","documents":["unrelated","gateway inference routing","gateway"],"top_n":2}' || true)
  printf '%s' "$response" | python3 -c '
import json, sys
try:
    results = json.load(sys.stdin).get("results", [])
    valid = len(results) == 2 and results[0].get("index") == 1
    print("yes" if valid else "no")
except Exception:
    print("no")'
}

models_api() {
  local response
  response=$(curl -sS -m 25 "$1/v1/models" || true)
  printf '%s' "$response" | python3 -c '
import json, sys
expected = {"kimi-k3", "glm-5.3", "deepseek-v4-pro", "deepseek-v4-flash", "qwen3.8-27b",
            "whisper-large-v3", "voxtral-small-24b", "qwen3-embedding-8b", "bge-m3"}
try:
    data = json.load(sys.stdin).get("data", [])
    ids = {card.get("id") for card in data}
    print(f"{len(data)} models" if expected <= ids else "no")
except Exception:
    print("no")'
}

tiered_chat() {
  local base="$1" tier model results=""
  for pair in big:kimi-k3 medium:deepseek-v4-flash small:qwen3.8-27b; do
    tier=${pair%%:*}
    model=${pair#*:}
    results+=$(curl -sS -m 25 "$base/v1/chat/completions" \
      -H 'content-type: application/json' \
      -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"hello through KServe\"}]}" |
      python3 -c "
import json, sys
try:
    body = json.load(sys.stdin)
    print('yes' if body.get('model') == '$model' and body.get('mock_tier') == '$tier' else 'no')
except Exception:
    print('no')")
  done
  [[ "$results" == "yesyesyes" ]] && echo "big/medium/small" || echo no
}

rag_embeddings_api() {
  local base="$1" response results=""
  for pair in qwen3-embedding-8b:4096 bge-m3:1024; do
    response=$(curl -sS -m 25 "$base/v1/embeddings" \
      -H 'content-type: application/json' \
      -d "{\"model\":\"${pair%%:*}\",\"input\":[\"retrieval augmented generation\"]}" || true)
    results+=$(printf '%s' "$response" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin).get('data', [])
    print('yes' if len(data) == 1 and len(data[0].get('embedding', [])) == ${pair##*:} else 'no')
except Exception:
    print('no')")
  done
  [[ "$results" == "yesyes" ]] && echo yes || echo no
}

diarization_api() {
  local response
  response=$(curl -sS -m 25 "$1/v1/audio/transcriptions" \
    -F 'file=@/dev/null;filename=meeting.wav;type=audio/wav' \
    -F 'model=whisper-large-v3' \
    -F 'diarization=true' \
    -F 'num_speakers=3' || true)
  printf '%s' "$response" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
    speakers = {segment.get("speaker") for segment in body.get("segments", [])}
    valid = (body.get("diarization") is True and speakers and
             speakers == {entry.get("id") for entry in body.get("speakers", [])})
    print(f"{len(speakers)} speakers" if valid else "no")
except Exception:
    print("no")'
}

stt_api() {
  local response
  response=$(curl -sS -m 25 "$1/v1/audio/transcriptions" \
    -F 'file=@/dev/null;filename=sample.wav;type=audio/wav' \
    -F 'model=mock-whisper' || true)
  printf '%s' "$response" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
    valid = body.get("model") == "mock-whisper" and "sample.wav" in body.get("text", "")
    print("yes" if valid else "no")
except Exception:
    print("no")'
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

ku_gateway=$(condition_status kind-ai-gw-kuadrant gateway/openshift-ai-inference Programmed openshift-ingress)
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
two-replica multi-task CPU runtime, KServe-managed llm-d EPP, InferencePool,
and HTTPRoute.
Sample size: $N requests per gateway.

| Check | OpenShift profile (Kuadrant + Istio/Envoy) | Envoy AI Gateway | agentgateway |
|---|---|---|---|
| Gateway Programmed | $ku_gateway | $ea_gateway | $ag_gateway |
| LLMInferenceService Ready | $ku_llmisvc | $ea_llmisvc | $ag_llmisvc |
| HTTPRoute Accepted / ResolvedRefs | $ku_route | $ea_route | $ag_route |
| workload replicas ready | $(ready_replicas kind-ai-gw-kuadrant) | $(ready_replicas kind-ai-gw-envoy) | $(ready_replicas kind-ai-gw-agent) |
| KServe-owned Deployments/Services/Pool | $(owned_resources kind-ai-gw-kuadrant) | $(owned_resources kind-ai-gw-envoy) | $(owned_resources kind-ai-gw-agent) |
| chat endpoint selection and success | $(printf '%s\n' "$ku_samples" | distribution) | $(printf '%s\n' "$ea_samples" | distribution) | $(printf '%s\n' "$ag_samples" | distribution) |
| streaming usage chunk | $(stream_usage http://localhost:8082) | $(stream_usage http://localhost:8080) | $(stream_usage http://localhost:8081) |
| embeddings API | $(embeddings_api http://localhost:8082) | $(embeddings_api http://localhost:8080) | $(embeddings_api http://localhost:8081) |
| reranking API | $(rerank_api http://localhost:8082) | $(rerank_api http://localhost:8080) | $(rerank_api http://localhost:8081) |
| model catalog (GET /v1/models) | $(models_api http://localhost:8082) | $(models_api http://localhost:8080) | $(models_api http://localhost:8081) |
| tiered chat models | $(tiered_chat http://localhost:8082) | $(tiered_chat http://localhost:8080) | $(tiered_chat http://localhost:8081) |
| RAG embedding models | $(rag_embeddings_api http://localhost:8082) | $(rag_embeddings_api http://localhost:8080) | $(rag_embeddings_api http://localhost:8081) |
| speech-to-text API | $(stt_api http://localhost:8082) | $(stt_api http://localhost:8080) | $(stt_api http://localhost:8081) |
| speaker diarization | $(diarization_api http://localhost:8082) | $(diarization_api http://localhost:8080) | $(diarization_api http://localhost:8081) |
| p50 gateway-to-KServe latency | $(printf '%s\n' "$ku_samples" | p50) | $(printf '%s\n' "$ea_samples" | p50) | $(printf '%s\n' "$ag_samples" | p50) |
| Kuadrant RateLimitPolicy ready | $(policy_ready) | n/a | n/a |

## Shared path

    client -> Gateway -> HTTPRoute -> InferencePool
           -> KServe-managed EPP -> selected KServe model pod

The Kuadrant column mirrors OpenShift AI's shared-Gateway topology with an
\`openshift-ai-inference\` Gateway in \`openshift-ingress\`, an Istio control plane,
an Envoy proxy, and Kuadrant policy. It is a kind analogue, not a Red Hat
product installation. Latency is a local smoke-test against a zero-delay mock,
not a production benchmark.
EOF

echo "wrote $OUT" >&2
cat "$OUT"
