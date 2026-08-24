#!/usr/bin/env bash
# Compare the model catalog, chat, embeddings, reranking, and STT through one
# KServe service, plus the gateway features layered on that same path:
# Keycloak authentication, group authorization, request rate limiting, daily
# quotas, token budgets, and CORS.
#
# The feature probes are skipped automatically when `make policies` has not
# been run, so the routing comparison works on its own.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/compare/results/comparison-$(date +%Y%m%d-%H%M%S).txt}"
N="${N:-30}"
KU_BASE=http://localhost:8082
EA_BASE=http://localhost:8080
AG_BASE=http://localhost:8081
BODY='{"model":"mock-kserve","messages":[{"role":"user","content":"hello through KServe"}]}'
BIG_BODY='{"model":"kimi-k3","messages":[{"role":"user","content":"hello through KServe"}]}'
STREAM='{"model":"mock-kserve","stream":true,"stream_options":{"include_usage":true},"messages":[{"role":"user","content":"hello through KServe"}]}'
# Kuadrant and Envoy Gateway key the quota bucket on this value, so a second
# run on the same day still measures enforcement rather than an already-spent
# daily quota. agentgateway's local limits have no keyed descriptor, so its
# bucket is shared by every run inside the window.
RUN_ID="probe-$(date +%s)"

condition_status() {
  local context="$1" object="$2" condition="$3" namespace="${4:-ai-demo}"
  # A missing object must read "no", not abort the run under pipefail.
  { kubectl --context "$context" -n "$namespace" get "$object" -o json 2>/dev/null || true; } | python3 -c '
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

# Policy status lives under .status.conditions for Kuadrant and under
# .status.ancestors[] for Gateway API policy attachment, so both are searched.
policy_condition() {
  local context="$1" object="$2" condition="$3"
  { kubectl --context "$context" -n ai-demo get "$object" -o json 2>/dev/null || true; } | python3 -c '
import json, sys
want = sys.argv[1]
try:
    status = json.load(sys.stdin).get("status", {})
    conditions = list(status.get("conditions", []))
    for key in ("ancestors", "parents"):
        for ancestor in status.get(key, []):
            conditions.extend(ancestor.get("conditions", []))
    print("yes" if any(c.get("type") == want and c.get("status") == "True"
                       for c in conditions) else "no")
except Exception:
    print("no")' "$condition"
}

# Presence, not readiness: the feature probes must run whenever the policies
# were deployed, so that a policy the controller rejected shows up as failed
# enforcement rather than as an absent feature layer.
policies_present() {
  local context="$1" object
  shift
  for object in "$@"; do
    if kubectl --context "$context" -n ai-demo get "$object" >/dev/null 2>&1; then
      echo yes
      return
    fi
  done
  echo no
}

policies_accepted() {
  local context="$1" condition="$2" ready=0 total=0 object
  shift 2
  for object in "$@"; do
    total=$((total + 1))
    if [[ "$(policy_condition "$context" "$object" "$condition")" == yes ]]; then
      ready=$((ready + 1))
    fi
  done
  ((total)) || { echo n/a; return; }
  echo "$ready/$total"
}

fetch_token() {
  local base="$1" user="$2" password="$3"
  # An unreachable gateway must leave the token empty, not abort the run.
  { curl -sS -m 20 "$base/realms/ai-gateway/protocol/openid-connect/token" \
    -H 'content-type: application/x-www-form-urlencoded' \
    -d grant_type=password -d client_id=ai-gateway-cli \
    -d "username=$user" -d "password=$password" 2>/dev/null || true; } | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("access_token", ""))
except Exception:
    print("")'
}

KU_TOKEN=""
EA_TOKEN=""
AG_TOKEN=""
KU_FEATURES=no
EA_FEATURES=no
AG_FEATURES=no

features_for() {
  case "$1" in
    "$KU_BASE") printf '%s' "$KU_FEATURES" ;;
    "$EA_BASE") printf '%s' "$EA_FEATURES" ;;
    "$AG_BASE") printf '%s' "$AG_FEATURES" ;;
  esac
}

# Keycloak on its own is not the feature layer: `make keycloak` installs the
# realm without any policy, and tokens would then be accepted everywhere.
# Probes report what is missing instead of reporting an open path as enforced.
feature_guard() {
  local base="$1"
  if [[ "$(features_for "$base")" != yes ]]; then
    echo "no policy"
  elif [[ -z "$(token_for "$base")" ]]; then
    echo "token error"
  fi
}

token_for() {
  case "$1" in
    "$KU_BASE") printf '%s' "$KU_TOKEN" ;;
    "$EA_BASE") printf '%s' "$EA_TOKEN" ;;
    "$AG_BASE") printf '%s' "$AG_TOKEN" ;;
  esac
}

# Every functional probe runs authenticated when policies are deployed and
# anonymous when they are not, so one script covers both states of the repo.
acurl() {
  local base="$1" token
  shift
  token="$(token_for "$base")"
  if [[ -n "$token" ]]; then
    curl -H "authorization: Bearer $token" "$@"
  else
    curl "$@"
  fi
}

status_code() {
  local base="$1"
  shift
  acurl "$base" -sS -o /dev/null -w '%{http_code}' -m 25 "$@"
}

samples() {
  local base="$1" raw status rest elapsed body pod
  for _ in $(seq 1 "$N"); do
    raw=$(acurl "$base" -sS -m 25 -w '|%{time_total}|%{http_code}' "$base/v1/chat/completions" -H 'content-type: application/json' -d "$BODY")
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
  count=$(acurl "$1" -sS -m 25 "$1/v1/chat/completions" -H 'content-type: application/json' -d "$STREAM" | grep -c '"usage"' || true)
  [[ "$count" -gt 0 ]] && echo yes || echo no
}

embeddings_api() {
  local response
  response=$(acurl "$1" -sS -m 25 "$1/v1/embeddings" \
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
  response=$(acurl "$1" -sS -m 25 "$1/v1/rerank" \
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
  response=$(acurl "$1" -sS -m 25 "$1/v1/models" || true)
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
    results+=$(acurl "$base" -sS -m 25 "$base/v1/chat/completions" \
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
    response=$(acurl "$base" -sS -m 25 "$base/v1/embeddings" \
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
  response=$(acurl "$1" -sS -m 25 "$1/v1/audio/transcriptions" \
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
  response=$(acurl "$1" -sS -m 25 "$1/v1/audio/transcriptions" \
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

# --- gateway feature probes -------------------------------------------------

token_issuance() {
  [[ -n "$(token_for "$1")" ]] && echo yes || echo no
}

# Anonymous, forged, and valid credentials against the same endpoint.
authentication_probe() {
  local base="$1" token anonymous forged valid guard
  guard="$(feature_guard "$base")"
  if [[ -n "$guard" ]]; then
    echo "$guard"
    return
  fi
  token="$(token_for "$base")"
  anonymous=$(curl -sS -o /dev/null -w '%{http_code}' -m 25 "$base/v1/chat/completions" \
    -H 'content-type: application/json' -d "$BODY" || true)
  forged=$(curl -sS -o /dev/null -w '%{http_code}' -m 25 "$base/v1/chat/completions" \
    -H 'authorization: Bearer not.a.real.token' \
    -H 'content-type: application/json' -d "$BODY" || true)
  valid=$(curl -sS -o /dev/null -w '%{http_code}' -m 25 "$base/v1/chat/completions" \
    -H "authorization: Bearer $token" \
    -H 'content-type: application/json' -d "$BODY" || true)
  echo "$anonymous / $forged / $valid"
}

# mallory is only in `guests`; bob is in `model-users` but not
# `platform-admins`, so only alice may reach the B300 class.
authorization_probe() {
  local base="$1" guest member admin outsider restricted allowed guard
  guard="$(feature_guard "$base")"
  if [[ -n "$guard" ]]; then
    echo "$guard"
    return
  fi
  guest="$(fetch_token "$base" mallory mallory)"
  member="$(fetch_token "$base" bob bob)"
  admin="$(token_for "$base")"
  [[ -n "$guest" && -n "$member" ]] || { echo "token error"; return; }
  outsider=$(curl -sS -o /dev/null -w '%{http_code}' -m 25 "$base/v1/chat/completions" \
    -H "authorization: Bearer $guest" \
    -H 'content-type: application/json' -d "$BODY" || true)
  restricted=$(curl -sS -o /dev/null -w '%{http_code}' -m 25 "$base/v1/chat/completions" \
    -H "authorization: Bearer $member" -H 'x-model-class: b300' \
    -H 'content-type: application/json' -d "$BIG_BODY" || true)
  allowed=$(curl -sS -o /dev/null -w '%{http_code}' -m 25 "$base/v1/chat/completions" \
    -H "authorization: Bearer $admin" -H 'x-model-class: b300' \
    -H 'content-type: application/json' -d "$BIG_BODY" || true)
  echo "$outsider / $restricted / $allowed"
}

# Sends up to `tries` requests carrying an opt-in probe header and reports
# where the limit closed. The header keeps ordinary traffic out of the budget.
limit_probe() {
  local base="$1" header="$2" tries="$3" body="${4:-$BODY}" code index guard
  guard="$(feature_guard "$base")"
  if [[ -n "$guard" ]]; then
    echo "$guard"
    return
  fi
  for index in $(seq 1 "$tries"); do
    code=$(status_code "$base" "$base/v1/chat/completions" -H "$header" \
      -H 'content-type: application/json' -d "$body" || true)
    if [[ "$code" == 429 ]]; then
      echo "429 on request $index of $tries"
      return
    fi
  done
  echo "no 429 in $tries"
}

# Envoy AI Gateway extracts token counts only for traffic on an
# AIGatewayRoute, which this repository pins to the ai.local hostname; the
# other two stacks read usage on the shared route.
token_limit_probe() {
  local base="$1" code index guard
  guard="$(feature_guard "$base")"
  if [[ -n "$guard" ]]; then
    echo "$guard"
    return
  fi
  for index in $(seq 1 6); do
    if [[ "$base" == "$EA_BASE" ]]; then
      # The AI route is JWT-protected like the plain one, and the bucket key
      # comes from the verified claim rather than from a client header.
      code=$(status_code "$base" "$base/v1/chat/completions" \
        -H 'host: ai.local' \
        -H 'content-type: application/json' -d "$BIG_BODY" || true)
    else
      code=$(status_code "$base" "$base/v1/chat/completions" \
        -H 'x-token-limit-probe: true' \
        -H 'content-type: application/json' -d "$BIG_BODY" || true)
    fi
    if [[ "$code" == 429 ]]; then
      echo "429 on request $index of 6"
      return
    fi
  done
  echo "no 429 in 6"
}

# agentgateway counts local limits inside the proxy with no keyed descriptor,
# so RUN_ID cannot give it a fresh bucket the way it does for the other two.
quota_probe() {
  local base="$1" result
  result="$(limit_probe "$base" "x-quota-probe: $RUN_ID" 6)"
  if [[ "$base" == "$AG_BASE" && "$result" == 429* ]]; then
    result="$result, bucket shared across runs"
  fi
  echo "$result"
}

cors_probe() {
  local headers
  headers=$(curl -sS -o /dev/null -D - -m 25 -X OPTIONS "$1/v1/chat/completions" \
    -H 'origin: https://console.example.com' \
    -H 'access-control-request-method: POST' \
    -H 'access-control-request-headers: authorization' 2>/dev/null || true)
  if grep -qi '^access-control-allow-origin' <<<"$headers"; then
    echo yes
  else
    echo no
  fi
}

ready_replicas() {
  kubectl --context "$1" -n ai-demo get deployment/kserve-mock-kserve -o 'jsonpath={.status.readyReplicas}/{.spec.replicas}' 2>/dev/null || echo n/a
}

owned_resources() {
  { kubectl --context "$1" -n ai-demo get deployment,service,inferencepool -o json 2>/dev/null || true; } | python3 -c '
import json, sys
try:
    items = json.load(sys.stdin).get("items", [])
    print(sum(any(o.get("kind") == "LLMInferenceService" and o.get("name") == "kserve-mock"
                  for o in item.get("metadata", {}).get("ownerReferences", []))
              for item in items))
except Exception:
    print("n/a")'
}

echo "collecting KServe comparison ($N requests per gateway)..." >&2
KU_TOKEN=$(fetch_token "$KU_BASE" alice alice)
EA_TOKEN=$(fetch_token "$EA_BASE" alice alice)
AG_TOKEN=$(fetch_token "$AG_BASE" alice alice)
# Only the objects `make policies` adds count as the feature layer; the
# RateLimitPolicy `make up` installs would otherwise make the Kuadrant column
# look protected when it is not.
KU_FEATURES=$(policies_present kind-ai-gw-kuadrant \
  authpolicy/kserve-mock tokenratelimitpolicy/kserve-mock)
EA_FEATURES=$(policies_present kind-ai-gw-envoy \
  securitypolicy/kserve-mock backendtrafficpolicy/kserve-mock)
AG_FEATURES=$(policies_present kind-ai-gw-agent \
  agentgatewaypolicy/kserve-mock-jwt agentgatewaypolicy/kserve-mock-rate-limit)
if [[ "$KU_FEATURES$EA_FEATURES$AG_FEATURES" == *yes* ]]; then
  echo "gateway policies deployed; running the feature probes too" >&2
else
  echo "no gateway policies found; run 'make policies' for the feature probes" >&2
fi

ku_samples=$(samples "$KU_BASE")
ea_samples=$(samples "$EA_BASE")
ag_samples=$(samples "$AG_BASE")

ku_gateway=$(condition_status kind-ai-gw-kuadrant gateway/openshift-ai-inference Programmed openshift-ingress)
ea_gateway=$(condition_status kind-ai-gw-envoy gateway/ai-gateway Programmed)
ag_gateway=$(condition_status kind-ai-gw-agent gateway/ai-gateway Programmed)
ku_route="$(condition_status kind-ai-gw-kuadrant httproute/kserve-mock Accepted) / $(condition_status kind-ai-gw-kuadrant httproute/kserve-mock ResolvedRefs)"
ea_route="$(condition_status kind-ai-gw-envoy httproute/kserve-mock Accepted) / $(condition_status kind-ai-gw-envoy httproute/kserve-mock ResolvedRefs)"
ag_route="$(condition_status kind-ai-gw-agent httproute/kserve-mock Accepted) / $(condition_status kind-ai-gw-agent httproute/kserve-mock ResolvedRefs)"
ku_llmisvc=$(condition_status kind-ai-gw-kuadrant llminferenceservice/kserve-mock Ready)
ea_llmisvc=$(condition_status kind-ai-gw-envoy llminferenceservice/kserve-mock Ready)
ag_llmisvc=$(condition_status kind-ai-gw-agent llminferenceservice/kserve-mock Ready)

ku_policies=$(policies_accepted kind-ai-gw-kuadrant Enforced \
  authpolicy/kserve-mock ratelimitpolicy/kserve-mock tokenratelimitpolicy/kserve-mock)
ea_policies=$(policies_accepted kind-ai-gw-envoy Accepted \
  securitypolicy/kserve-mock backendtrafficpolicy/kserve-mock aigatewayroute/kserve-mock-ai)
ag_policies=$(policies_accepted kind-ai-gw-agent Accepted \
  agentgatewaypolicy/kserve-mock-jwt agentgatewaypolicy/kserve-mock-members \
  agentgatewaypolicy/kserve-mock-big-tier agentgatewaypolicy/kserve-mock-rate-limit \
  agentgatewaypolicy/kserve-mock-cors)

mkdir -p "$(dirname "$OUT")"
cat > "$OUT" <<EOF
# KServe gateway comparison — $(date -u '+%Y-%m-%d %H:%M UTC')

All environments run KServe v0.20.0 with the same LLMInferenceService,
two-replica multi-task CPU runtime, KServe-managed llm-d EPP, InferencePool,
and HTTPRoute.
Sample size: $N requests per gateway.

## Inference path

| Check | OpenShift profile (Kuadrant + Istio/Envoy) | Envoy AI Gateway | agentgateway |
|---|---|---|---|
| Gateway Programmed | $ku_gateway | $ea_gateway | $ag_gateway |
| LLMInferenceService Ready | $ku_llmisvc | $ea_llmisvc | $ag_llmisvc |
| HTTPRoute Accepted / ResolvedRefs | $ku_route | $ea_route | $ag_route |
| workload replicas ready | $(ready_replicas kind-ai-gw-kuadrant) | $(ready_replicas kind-ai-gw-envoy) | $(ready_replicas kind-ai-gw-agent) |
| KServe-owned Deployments/Services/Pool | $(owned_resources kind-ai-gw-kuadrant) | $(owned_resources kind-ai-gw-envoy) | $(owned_resources kind-ai-gw-agent) |
| chat endpoint selection and success | $(printf '%s\n' "$ku_samples" | distribution) | $(printf '%s\n' "$ea_samples" | distribution) | $(printf '%s\n' "$ag_samples" | distribution) |
| streaming usage chunk | $(stream_usage "$KU_BASE") | $(stream_usage "$EA_BASE") | $(stream_usage "$AG_BASE") |
| embeddings API | $(embeddings_api "$KU_BASE") | $(embeddings_api "$EA_BASE") | $(embeddings_api "$AG_BASE") |
| reranking API | $(rerank_api "$KU_BASE") | $(rerank_api "$EA_BASE") | $(rerank_api "$AG_BASE") |
| model catalog (GET /v1/models) | $(models_api "$KU_BASE") | $(models_api "$EA_BASE") | $(models_api "$AG_BASE") |
| tiered chat models | $(tiered_chat "$KU_BASE") | $(tiered_chat "$EA_BASE") | $(tiered_chat "$AG_BASE") |
| RAG embedding models | $(rag_embeddings_api "$KU_BASE") | $(rag_embeddings_api "$EA_BASE") | $(rag_embeddings_api "$AG_BASE") |
| speech-to-text API | $(stt_api "$KU_BASE") | $(stt_api "$EA_BASE") | $(stt_api "$AG_BASE") |
| speaker diarization | $(diarization_api "$KU_BASE") | $(diarization_api "$EA_BASE") | $(diarization_api "$AG_BASE") |
| p50 gateway-to-KServe latency | $(printf '%s\n' "$ku_samples" | p50) | $(printf '%s\n' "$ea_samples" | p50) | $(printf '%s\n' "$ag_samples" | p50) |

## Gateway features

Measured only when \`make policies\` has been run; otherwise the probes read
"no policy" and the readiness row counts only the policy objects \`make up\`
already installs. Enforcement checks report the request number that first
returned HTTP 429.

| Check | OpenShift profile (Kuadrant + Istio/Envoy) | Envoy AI Gateway | agentgateway |
|---|---|---|---|
| policy objects reporting ready | $ku_policies | $ea_policies | $ag_policies |
| Keycloak token issuance | $(token_issuance "$KU_BASE") | $(token_issuance "$EA_BASE") | $(token_issuance "$AG_BASE") |
| authentication: anonymous / forged / valid | $(authentication_probe "$KU_BASE") | $(authentication_probe "$EA_BASE") | $(authentication_probe "$AG_BASE") |
| authorization: guest / non-admin B300 / admin B300 | $(authorization_probe "$KU_BASE") | $(authorization_probe "$EA_BASE") | $(authorization_probe "$AG_BASE") |
| request rate limit (5 per minute) | $(limit_probe "$KU_BASE" 'x-rate-limit-probe: true' 8) | $(limit_probe "$EA_BASE" 'x-rate-limit-probe: true' 8) | $(limit_probe "$AG_BASE" 'x-rate-limit-probe: true' 8) |
| quota limit (3 per window) | $(quota_probe "$KU_BASE") | $(quota_probe "$EA_BASE") | $(quota_probe "$AG_BASE") |
| token limit (100 tokens per minute) | $(token_limit_probe "$KU_BASE") | $(token_limit_probe "$EA_BASE") | $(token_limit_probe "$AG_BASE") |
| CORS preflight answered | $(cors_probe "$KU_BASE") | $(cors_probe "$EA_BASE") | $(cors_probe "$AG_BASE") |

### How each stack expresses those features

Configuration facts, not measurements: the resource each gateway uses for the
same job, and where the counters live.

| Feature | OpenShift profile (Kuadrant) | Envoy AI Gateway | agentgateway |
|---|---|---|---|
| JWT / OIDC authentication | \`AuthPolicy\` \`authentication.jwt\` | \`SecurityPolicy\` \`jwt.providers\` | \`AgentgatewayPolicy\` \`traffic.jwtAuthentication\` |
| browser login flow | \`OIDCPolicy\` | \`SecurityPolicy\` \`oidc\` | MCP OAuth metadata, Keycloak preset |
| API key authentication | \`AuthPolicy\` \`authentication.apiKey\`, Secret-backed | \`SecurityPolicy\` \`apiKeyAuth\` | \`traffic.apiKeyAuthentication\`, Secret or ConfigMap |
| mTLS client identity | \`AuthPolicy\` \`authentication.x509\` | \`ClientTrafficPolicy\` TLS settings | \`frontend.tls\` |
| external authorization | \`AuthPolicy\` \`metadata.http\`, OPA, SpiceDB | \`SecurityPolicy\` \`extAuth\` | \`traffic.extAuth\` |
| authorization rules | pattern matching, CEL, OPA Rego | claim and header rules, first match wins | CEL \`matchExpressions\`, Allow/Deny/Require |
| request rate limiting | \`RateLimitPolicy\`, Limitador | \`BackendTrafficPolicy\` global or local | \`traffic.rateLimit.local\` or global |
| quota windows | any window up to \`24h\` in one policy | \`unit: Day\`, \`Month\`, \`Year\` | local limits stop at \`Hours\`; longer needs a global service |
| token budgets | \`TokenRateLimitPolicy\`, reads \`usage.total_tokens\` | \`llmRequestCosts\` into Envoy metadata, charged by the rate limit filter | \`unit: Tokens\` on a rate limit descriptor |
| counter storage | Limitador, installed by the operator | external Redis plus the rate limit service | in-process for local limits |
| per-identity buckets | CEL counters over auth identity | rate limit descriptors over headers | descriptors over CEL, global mode only |
| LLM request shaping | none | \`AIGatewayRoute\` body and header mutation, model name override | \`backend.ai\` model aliases, prompt prepend/append, prompt caching |
| prompt guardrails | none | none | \`backend.ai.promptGuard\` |
| provider credentials | none | \`BackendSecurityPolicy\` | backend auth on \`AgentgatewayBackend\` |
| MCP routing | none | \`MCPRoute\` | native MCP support with OAuth |
| telemetry controls | \`TelemetryPolicy\` metric labels | \`EnvoyProxy\` telemetry | \`frontend.metrics\`, \`tracing\`, \`accessLog\` |

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
