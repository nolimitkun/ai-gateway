#!/usr/bin/env bash
# Deeper feature probes across both gateways. run-comparison.sh covers the
# basics and latency; this one covers translation, failover, auth and formats.
#
# Requires both stacks up (make up) and the manifests applied.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/compare/results/features-$(date +%Y%m%d-%H%M%S).md}"
EAIG=http://localhost:8080
AGW=http://localhost:8081
CHAT='{"model":"%s","messages":[{"role":"user","content":"hi"}]}'

code() { # code <url> <header|-> <body>
  local u="$1" h="$2" b="$3" a=(-s -o /dev/null -m 20 -w '%{http_code}' -H 'content-type: application/json')
  [[ "$h" != "-" ]] && a+=(-H "$h")
  curl "${a[@]}" "$u" -d "$b"
}
ok() { [[ "$1" == "200" ]] && echo "yes" || echo "no ($1)"; }

echo "probing..." >&2

# --- cross-provider translation, Envoy side ------------------------------
# macOS ships bash 3.2, which has no associative arrays -- plain vars instead.
ea() { code "$EAIG/v1/chat/completions" - "$(printf "$CHAT" "probe-$1")"; }
ea_openai=$(ea openai);             ea_azure=$(ea azureopenai)
ea_bedrock=$(ea awsbedrock);        ea_vertex=$(ea gcpvertexai)
ea_gcpanth=$(ea gcpanthropic);      ea_awsanth=$(ea awsanthropic)
ea_anthropic=$(ea anthropic);       ea_cohere=$(ea cohere)
# --- cross-provider translation, agentgateway side -----------------------
agw_messages=$(code "$AGW/v1/chat/completions" 'x-route-to: messages' "$(printf "$CHAT" probe)")
agw_gemini=$(  code "$AGW/v1/chat/completions" 'x-route-to: gemini'   "$(printf "$CHAT" probe)")
agw_anthropic=$(code "$AGW/v1/chat/completions" 'x-route-to: anthropic' "$(printf "$CHAT" probe)")

# --- embeddings ----------------------------------------------------------
EMB='{"model":"mock-gpt-4o","input":["hello world"]}'
eaig_emb=$(code "$EAIG/v1/embeddings" - "$EMB")
agw_emb=$( code "$AGW/v1/embeddings"  - "$EMB")

# --- credential injection (client sends nothing) -------------------------
seen_auth() { # seen_auth <context>
  local ctx="$1" port=$((18000 + RANDOM % 900))
  kubectl --context "$ctx" -n ai-demo port-forward svc/mock-llm-alpha "$port:80" >/dev/null 2>&1 &
  local pf=$!; sleep 4
  curl -s -m 8 "localhost:$port/__requests" 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("n/a"); raise SystemExit
for r in reversed(d["requests"]):
    if r["path"].endswith("/chat/completions"):
        print(r["authorization"] or "none"); raise SystemExit
print("none")'
  kill $pf 2>/dev/null
}
code "$EAIG/v1/chat/completions" - "$(printf "$CHAT" mock-gpt-4o)" >/dev/null
code "$AGW/v1/chat/completions"  - "$(printf "$CHAT" mock-gpt-4o)" >/dev/null
eaig_auth=$(seen_auth kind-ai-gw-envoy)
agw_auth=$( seen_auth kind-ai-gw-agent)

mkdir -p "$(dirname "$OUT")"
cat > "$OUT" <<EOF
# Feature matrix — $(date -u '+%Y-%m-%d %H:%M UTC')

## Cross-provider translation (OpenAI-format client in, OpenAI response out)

Envoy AI Gateway, by \`AIServiceBackend.spec.schema.name\`:

| upstream schema | result |
|---|---|
| OpenAI | $(ok "$ea_openai") |
| AzureOpenAI | $(ok "$ea_azure") |
| AWSBedrock | $(ok "$ea_bedrock") |
| GCPVertexAI | $(ok "$ea_vertex") |
| AWSAnthropic | $(ok "$ea_awsanth") |
| GCPAnthropic | $(ok "$ea_gcpanth") |
| Anthropic (direct) | $(ok "$ea_anthropic") — translator refuses: \`unsupported API schema\` |
| Cohere | $(ok "$ea_cohere") — translator refuses: \`unsupported API schema\` |

agentgateway, by provider/format:

| config | result |
|---|---|
| custom provider, \`formats: [Messages]\` | $(ok "$agw_messages") |
| managed \`gemini\` w/ host override | $(ok "$agw_gemini") |
| managed \`anthropic\` w/ host override | $(ok "$agw_anthropic") — keeps OpenAI request path, then fails to parse the native response |

## Other endpoints

| | Envoy AI Gateway | agentgateway |
|---|---|---|
| \`/v1/embeddings\` | $(ok "$eaig_emb") | $(ok "$agw_emb") |

## Credential injection (client sent no credentials)

| | header the upstream received |
|---|---|
| Envoy AI Gateway (\`BackendSecurityPolicy\`) | \`$eaig_auth\` |
| agentgateway (\`AgentgatewayPolicy.backend.auth\`) | \`$agw_auth\` |
EOF
echo "wrote $OUT" >&2
cat "$OUT"
