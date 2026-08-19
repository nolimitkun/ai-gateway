# Envoy AI Gateway vs. agentgateway — hands-on comparison

Two local kind clusters, the same mock OpenAI upstreams behind each gateway, the
same functional suite run against both. Everything here is reproducible offline:
no API keys, no paid provider.

| | Envoy AI Gateway | agentgateway |
|---|---|---|
| version | v1.0.0 (2026-06-23) | v1.4.1 (2026-07-29) |
| cluster | `kind-ai-gw-envoy` | `kind-ai-gw-agent` |
| endpoint | http://localhost:8080 | http://localhost:8081 |
| control plane | Envoy Gateway v1.8.1 + AI Gateway controller | single agentgateway controller |

## Quick start

```bash
make up
```

Then run the suites:

```bash
make compare
```

```bash
make features
```

## Why two clusters

Both projects install cluster-scoped Gateway API CRDs at different versions —
Envoy Gateway bundles its own, agentgateway pins v1.6.0. In one cluster,
whichever chart installs second silently overwrites the other's CRDs. Separate
clusters keep each stack on its own supported matrix, so the comparison measures
the gateways rather than a CRD collision.

## Results

Measured on a 4-CPU / 10 GB Docker VM, 30 requests per sample, against a mock
upstream with 0 ms of its own latency. Raw runs land in `compare/results/`.

| Check | Envoy AI Gateway v1.0.0 | agentgateway v1.4.1 |
|---|---|---|
| model → upstream routing | works | works |
| streaming w/ usage chunk | yes | yes |
| weighted 80/20 split | 24 / 6 | 25 / 5 |
| p50 latency | **15 ms** | **8 ms** |
| control+data plane pods | 3 | 2 |
| data plane containers | `ai-gateway-extproc`, `envoy`, `shutdown-manager` | `agentgateway` |
| CRDs installed | 26 | 14 |

### The architectural difference behind the latency

Envoy AI Gateway keeps Envoy as the data plane and puts all AI logic in a
separate **`ai-gateway-extproc` process**, injected as a native sidecar (an
initContainer with `restartPolicy: Always` — which is why `kubectl get pods`
shows `3/3` while `.spec.containers` lists only two). Every LLM request crosses
a gRPC ext_proc hop out of Envoy and back.

agentgateway is a single Rust binary that parses the LLM request in-process.

That hop is the ~7 ms difference. In exchange, Envoy AI Gateway inherits Envoy's
entire policy surface — the same `BackendTrafficPolicy`, `SecurityPolicy`, and
`EnvoyProxy` resources any Envoy Gateway user already knows.

### How the same intent is expressed

Routing "model `mock-gpt-4o` → upstream alpha":

**Envoy AI Gateway** — three resources. The ext_proc filter lifts `model` out of
the JSON body into an `x-ai-eg-model` header *before* the routing decision, so
the route matches on model name directly:

```
Backend            (gateway.envoyproxy.io)   -- the upstream
  ← AIServiceBackend (aigateway.envoyproxy.io) -- + the API schema it speaks
      ← AIGatewayRoute  matches x-ai-eg-model: mock-gpt-4o
```

`AIServiceBackend` **rejects a plain core/v1 Service** in v1.0.0
([#902](https://github.com/envoyproxy/ai-gateway/issues/902)), so the extra
`Backend` wrapper is mandatory.

**agentgateway** — two resources, and one is a stock Gateway API type:

```
Service
  ← AgentgatewayBackend  ai.groups[].providers[].custom.backendRef
      ← HTTPRoute        normal Gateway API matches
```

The AI behaviour lives in the backend; the route stays a plain `HTTPRoute`.
Priority failover is expressed as ordered `groups` inside one backend rather
than as a separate traffic policy.

### Observability

Both emit OpenTelemetry GenAI semantic conventions — this is genuinely a tie on
correctness, and differs mainly in packaging.

| | Envoy AI Gateway | agentgateway |
|---|---|---|
| endpoint | extproc sidecar `:1064/metrics` | proxy pod `:15020/metrics` |
| metric | `gen_ai_client_token_usage` | `agentgateway_gen_ai_client_token_usage` |
| families exposed | 5 (AI only; Envoy stats are separate) | 68 (AI + full proxy) |
| extra labels | `gen_ai_original_model` (survives model override) | `gateway`, `listener`, `route` |
| access log | Envoy access log | GenAI fields inline (`gen_ai.usage.input_tokens`, …) |

agentgateway also serves a built-in UI on the proxy's `localhost:15000/ui`.

### Token accounting for rate limits

Envoy AI Gateway writes token counts into Envoy **dynamic metadata** under
`io.envoy.ai_gateway` (`llmRequestCosts` in `03-route.yaml`), which Envoy
Gateway's `BackendTrafficPolicy` then consumes as a rate-limit cost. It is a
two-resource dance, but it reuses Envoy's existing global rate limiter.

agentgateway attaches policy to the model/backend directly — its
`AgentgatewayPolicy` covers `auth`, `authorization`, `promptGuard`, `health`,
`transformations`, and `tunnel` in one type.

## Feature comparison

Everything below was executed against the running clusters. The mock upstream
records the path, headers and body it actually received, so translation and
credential injection are demonstrated rather than asserted.

### Cross-provider translation

Client speaks OpenAI; the upstream is declared as something else. The gateway
must rewrite the request *and* translate the native response back.

**Envoy AI Gateway** — set `AIServiceBackend.spec.schema.name`. Proven by the
path the upstream received:

| schema | upstream path received | round trip |
|---|---|---|
| OpenAI | `/v1/chat/completions` | yes |
| AzureOpenAI | `/openai/deployments/<model>/chat/completions?api-version=…` | yes |
| AWSBedrock | `/model/<model>/converse` | yes |
| GCPVertexAI | `publishers/google/models/<model>:generateContent` | yes |
| GCPAnthropic | `publishers/anthropic/models/<model>:rawPredict` | yes |
| AWSAnthropic | `/model/<model>/invoke` | yes |
| **Anthropic (direct)** | — | **no** — `unsupported API schema` |
| **Cohere** | — | **no** — `unsupported API schema` |

That direct `Anthropic` is unsupported as an *upstream* target is worth knowing:
Anthropic-on-cloud (Bedrock/Vertex) works, api.anthropic.com does not.

**agentgateway** — declare the format on the provider. A custom provider with
`formats: [{type: Messages}]` does the full round trip: the mock answers from
`/v1/messages` with an Anthropic body (`msg_…`, `input_tokens`) and the client
still receives OpenAI `choices[].message`.

Overriding `host` on a *managed* provider (`anthropic`, `bedrock`) does **not**
work as a way to point at an arbitrary endpoint: agentgateway keeps the OpenAI
request path and then fails parsing the response
(``missing field `input_tokens` ``). Use a `custom` provider with explicit
`formats` for self-hosted endpoints.

### Failover

Neither gateway fails over on a bare upstream 503 — priority/groups are not
per-request retry. Both need an explicit policy, and the outcome differs:

| | config | result with primary returning 503 |
|---|---|---|
| Envoy AI Gateway | `BackendTrafficPolicy.retry.numAttemptsPerPriority: 1` + `backendRefs[].priority` | **works** — 8/8 requests served by the secondary |
| agentgateway | `AgentgatewayPolicy.traffic.retry` + ordered `ai.groups` | **not reproduced** — logs show `retry.attempt=3` all against the *same* endpoint; the second group was never tried |

For agentgateway I also tried `backend.health.eviction` with
`consecutiveFailures: 2` and an explicit `unhealthyCondition: "response.code >= 500"`
(its docs note the default only lowers a health score and never evicts), and
with a dedicated route and backend. Traffic still stayed on the failing
provider. Treating this as "I could not reproduce cross-group failover in
v1.4.1" rather than a definitive gap — there may be a knob I did not find.

### Credential injection

Client sends no credentials; the gateway attaches the upstream's. Verified by
reading the `Authorization` header the mock recorded:

| | policy | header upstream received |
|---|---|---|
| Envoy AI Gateway | `BackendSecurityPolicy` (`type: APIKey`) | `Bearer demo-key-eaig-123` |
| agentgateway | `AgentgatewayPolicy.backend.auth.secretRef` | `Bearer demo-key-agw-456` |

Envoy adds the `Bearer ` prefix to the raw secret value; agentgateway sends the
Secret's `Authorization` key verbatim, so the prefix must be inside the Secret.

### Endpoint coverage beyond chat

| | Envoy AI Gateway | agentgateway |
|---|---|---|
| `/v1/embeddings` | works | **fails** (503) |

On the Envoy side, `schema: OpenAI` covers the whole OpenAI surface. agentgateway
requires formats to be enumerated per provider — and even with a dedicated
backend declaring only `formats: [{type: Embeddings}]` behind an exact-path
route, the request was still parsed as a chat completion
(``missing field `messages` ``). The proxy log confirms the dedicated route
handled it (`route=ai-demo/llm-embeddings`), so this is not a routing mistake.

### Policy surface

The same capabilities are packaged very differently.

**Envoy AI Gateway** spreads them across Envoy Gateway's existing types:
`BackendTrafficPolicy` (retry, rate limit, circuit breaking), `SecurityPolicy`
(JWT, ext_auth, CORS), `BackendSecurityPolicy` (upstream credentials),
`EnvoyExtensionPolicy`, plus AI-specific `QuotaPolicy` and `MCPRoute`.

**agentgateway** puts nearly everything in one `AgentgatewayPolicy`:

- `spec.traffic` — `rateLimit`, `retry`, `timeouts`, `jwtAuthentication`,
  `apiKeyAuthentication`, `basicAuthentication`, `authorization`, `cors`, `csrf`,
  `extAuth`, `extProc`, `transformation`, `headerModifiers`, `hostRewrite`
- `spec.backend` — `ai`, `auth`, `health`, `mcp`, `tls`, `tunnel`,
  `transformation`

If you already run Envoy Gateway, the first is familiar and composable. If you
are starting fresh, the second is far less to learn.

### Agent-native surfaces (not exercised here)

Both ship MCP support and neither was tested — calling this out rather than
implying coverage. Envoy AI Gateway has a dedicated `MCPRoute` CRD;
agentgateway has `AgentgatewayBackend.spec.mcp` alongside `spec.a2a`
(Agent2Agent) and `spec.aws` (Bedrock AgentCore). agentgateway also exposes
`policies.promptGuard` for prompt/content filtering, which has no direct
single-resource equivalent on the Envoy side.

## Known gap found during this exercise

agentgateway's `AgentgatewayModel` API (`agentgatewayModels.enabled=true`) is
enabled in `agentgateway/values/agentgateway.values.yaml` and the controller
syncs the resources, but in v1.4.1 the models **never attach to the Gateway** —
`attachedRoutes` stays `0`, the proxy's `modelCatalog.sources` stays empty, and
requests 404. The controller logs `enqueueStatus unknown external type
*agentgateway.AgentgatewayModel` and the resources never get a `status`.

The attempted manifest is preserved as
`agentgateway/manifests/model-api-unsupported.yaml.disabled`. The working config
uses `AgentgatewayBackend` + `HTTPRoute` instead. Treat `AgentgatewayModel` as
not-yet-wired in v1.4.1 — worth re-testing on the next release, since it is the
closest analogue to `AIGatewayRoute` and would collapse the two resources into
one.

## Layout

```
clusters/            kind configs (one per gateway)
envoy-ai-gateway/
  charts/            pulled .tgz: gateway-helm, ai-gateway-crds-helm, ai-gateway-helm
  values/            *.default.yaml = chart defaults; ai-gateway.values.yaml = our overlay
  manifests/         Gateway, Backend+AIServiceBackend, AIGatewayRoute,
                     translation probes, retry/failover, BackendSecurityPolicy
agentgateway/
  charts/            pulled .tgz: agentgateway-crds, agentgateway
  values/            *.default.yaml = chart defaults; agentgateway.values.yaml = our overlay
  manifests/         Gateway, AgentgatewayBackend, HTTPRoute,
                     translation probes, retry+health, AgentgatewayPolicy auth
mock-llm/            OpenAI-compatible mock, also serving Anthropic/Bedrock/Vertex
                     native paths, request introspection and a failure toggle
scripts/             install + expose helpers
compare/             run-comparison.sh, feature-matrix.sh and results/
```

Charts are vendored as `.tgz` and installed from disk, so a re-run pins the exact
same bits. `values/*.default.yaml` is each chart's unmodified default values,
committed for diffing against future releases.

## Using a real provider

Point an `AIServiceBackend` / `AgentgatewayBackend` at a real endpoint and attach
credentials — `BackendSecurityPolicy` on the Envoy side, `AgentgatewayPolicy`
(`policies.auth.secretRef`) on the agentgateway side. Nothing else in the layout
changes.
