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

Then run the suite:

```bash
make compare
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
  manifests/         GatewayClass/Gateway, Backend+AIServiceBackend, AIGatewayRoute
agentgateway/
  charts/            pulled .tgz: agentgateway-crds, agentgateway
  values/            *.default.yaml = chart defaults; agentgateway.values.yaml = our overlay
  manifests/         Gateway, AgentgatewayBackend, HTTPRoute
mock-llm/            OpenAI-compatible mock (server.py is the source of truth)
scripts/             install + expose helpers
compare/             run-comparison.sh and results/
```

Charts are vendored as `.tgz` and installed from disk, so a re-run pins the exact
same bits. `values/*.default.yaml` is each chart's unmodified default values,
committed for diffing against future releases.

## Using a real provider

Point an `AIServiceBackend` / `AgentgatewayBackend` at a real endpoint and attach
credentials — `BackendSecurityPolicy` on the Envoy side, `AgentgatewayPolicy`
(`policies.auth.secretRef`) on the agentgateway side. Nothing else in the layout
changes.
