#!/usr/bin/env bash
# Runs the same functional suite against both gateways and writes a report.
#
#   Envoy AI Gateway -> http://localhost:8080  (kind ai-gw-envoy)
#   agentgateway     -> http://localhost:8081  (kind ai-gw-agent)
#
# Usage: compare/run-comparison.sh [output-file]
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/compare/results/comparison-$(date +%Y%m%d-%H%M%S).md}"
EAIG=http://localhost:8080
AGW=http://localhost:8081
N="${N:-20}"

# Model routing is expressed differently by design: Envoy AI Gateway matches on
# the model name lifted out of the request body, agentgateway matches on a
# normal HTTPRoute header. Each side gets the idiomatic form of the same intent.
post() { # post <base> <extra-header|-> <json>
  local base="$1" hdr="$2" body="$3"
  if [[ "$hdr" == "-" ]]; then
    curl -s -m 25 "$base/v1/chat/completions" -H 'content-type: application/json' -d "$body"
  else
    curl -s -m 25 "$base/v1/chat/completions" -H 'content-type: application/json' -H "$hdr" -d "$body"
  fi
}

timed() { # timed <base> <hdr> <json> -> milliseconds
  local base="$1" hdr="$2" body="$3" args=(-s -o /dev/null -m 25 -w '%{time_total}')
  [[ "$hdr" != "-" ]] && args+=(-H "$hdr")
  local t
  t=$(curl "${args[@]}" "$base/v1/chat/completions" -H 'content-type: application/json' -d "$body")
  python3 -c "print(round(float('$t')*1000))"
}

p50() { sort -n | awk '{a[NR]=$1} END{if(NR)print a[int((NR+1)/2)]; else print "n/a"}'; }

body_alpha='{"model":"mock-gpt-4o","messages":[{"role":"user","content":"hello"}]}'
body_beta='{"model":"mock-claude","messages":[{"role":"user","content":"hello"}]}'
body_split='{"model":"mock-split","messages":[{"role":"user","content":"x"}]}'
body_stream='{"model":"mock-gpt-4o","stream":true,"stream_options":{"include_usage":true},"messages":[{"role":"user","content":"hi"}]}'

echo "collecting ($N requests per latency sample)..." >&2

# --- routing correctness -------------------------------------------------
eaig_alpha=$(post $EAIG - "$body_alpha" | grep -oE 'from (alpha|beta)' || echo MISS)
eaig_beta=$( post $EAIG - "$body_beta"  | grep -oE 'from (alpha|beta)' || echo MISS)
agw_alpha=$( post $AGW  - "$body_alpha" | grep -oE 'from (alpha|beta)' || echo MISS)
agw_beta=$(  post $AGW  'x-route-to: beta' "$body_beta" | grep -oE 'from (alpha|beta)' || echo MISS)

# --- streaming (final chunk must carry usage) ----------------------------
eaig_stream=$(curl -s -m 25 $EAIG/v1/chat/completions -H 'content-type: application/json' -d "$body_stream" \
  | grep -c '"usage"' || true)
agw_stream=$(curl -s -m 25 $AGW/v1/chat/completions -H 'content-type: application/json' -d "$body_stream" \
  | grep -c '"usage"' || true)

# --- weighted split 80/20 ------------------------------------------------
eaig_split=$(for i in $(seq 1 $N); do post $EAIG - "$body_split"; echo; done \
  | grep -oE 'from (alpha|beta)' | sort | uniq -c | tr '\n' ' ')
agw_split=$(for i in $(seq 1 $N); do post $AGW 'x-route-to: split' "$body_alpha"; echo; done \
  | grep -oE 'from (alpha|beta)' | sort | uniq -c | tr '\n' ' ')

# --- latency p50 through each gateway (same zero-latency upstream) -------
eaig_p50=$(for i in $(seq 1 $N); do timed $EAIG - "$body_alpha"; done | p50)
agw_p50=$( for i in $(seq 1 $N); do timed $AGW  - "$body_alpha"; done | p50)

# --- resource footprint --------------------------------------------------
res() { # res <context> <ns...>
  local ctx="$1"; shift
  for ns in "$@"; do
    kubectl --context "$ctx" -n "$ns" top pod --no-headers 2>/dev/null \
      | awk -v n="$ns" '{print n"/"$1" "$2" "$3}'
  done
}
eaig_pods=$(kubectl --context kind-ai-gw-envoy get pods -A --no-headers 2>/dev/null \
  | grep -cE 'envoy-gateway-system|envoy-ai-gateway-system')
agw_pods=$(kubectl --context kind-ai-gw-agent get pods -A --no-headers 2>/dev/null \
  | grep -cE 'agentgateway-system|ai-demo.*ai-gateway')
# Count init containers too: Envoy AI Gateway injects ai-gateway-extproc as a
# native sidecar (an initContainer with restartPolicy=Always), so it is invisible
# in .spec.containers yet is a real per-request process hop.
dp_containers() { # dp_containers <context> <ns> <label>
  kubectl --context "$1" -n "$2" get pod -l "$3" -o json 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
if not d.get("items"): print("n/a"); raise SystemExit
p=d["items"][0]["spec"]
names=[c["name"] for c in p.get("initContainers",[]) if c.get("restartPolicy")=="Always"]
names+=[c["name"] for c in p.get("containers",[])]
print(", ".join(names))'
}
eaig_dp=$(dp_containers kind-ai-gw-envoy envoy-gateway-system app.kubernetes.io/component=proxy)
agw_dp=$(dp_containers kind-ai-gw-agent ai-demo gateway.networking.k8s.io/gateway-name=ai-gateway)

mkdir -p "$(dirname "$OUT")"
cat > "$OUT" <<EOF
# Gateway comparison — $(date -u '+%Y-%m-%d %H:%M UTC')

Same mock OpenAI upstreams (\`alpha\` 0ms, \`beta\` 250ms) behind both gateways.
Sample size: $N requests per measurement.

| Check | Envoy AI Gateway v1.0.0 | agentgateway v1.4.1 |
|---|---|---|
| route model → alpha | \`$eaig_alpha\` | \`$agw_alpha\` |
| route model → beta | \`$eaig_beta\` | \`$agw_beta\` |
| streaming usage chunk | $( [ "${eaig_stream:-0}" -gt 0 ] && echo "yes" || echo "no" ) | $( [ "${agw_stream:-0}" -gt 0 ] && echo "yes" || echo "no" ) |
| weighted 80/20 split | $eaig_split | $agw_split |
| p50 latency (0ms upstream) | ${eaig_p50} ms | ${agw_p50} ms |
| control+data plane pods | $eaig_pods | $agw_pods |
| data plane containers | \`$eaig_dp\` | \`$agw_dp\` |

## How the same intent is expressed

**Envoy AI Gateway** — model name is extracted from the JSON body into the
\`x-ai-eg-model\` header before routing, so the route matches on model directly:

    Backend -> AIServiceBackend -> AIGatewayRoute (matches x-ai-eg-model)

**agentgateway** — a stock HTTPRoute selects an AgentgatewayBackend, which
holds the provider/priority configuration:

    Service -> AgentgatewayBackend (ai.groups[].providers[]) <- HTTPRoute
EOF
echo "wrote $OUT" >&2
cat "$OUT"
