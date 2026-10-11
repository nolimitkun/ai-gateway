# Local gateway tests — 2026-10-10

All three retained Kind clusters were upgraded and tested sequentially with
Kubernetes 1.36.1, KServe 0.20.0, BBR 1.2.1, Keycloak, feature policies, and
semantic routing. Envoy and agentgateway also had their native routing overlays
installed. These are CPU fixtures, not real GPU/vLLM performance tests.

| Stack | Tested gateway versions | Comparison started (UTC) | Sample |
|---|---|---|---|
| Kuadrant | Kuadrant 1.5.3, Istio 1.30.5 | 21:42 | 30/30 HTTP 200, two model pods |
| Envoy | Agent Router 1.2.0, Envoy Gateway 1.9.2 | 21:36 | 30/30 HTTP 200, two model pods |
| agentgateway | 1.6.0 | 21:32 | 30/30 HTTP 200, two model pods |

All stacks reported Gateway Programmed, route Accepted/ResolvedRefs, and
LLMInferenceService Ready. All four BBR accelerator pools routed correctly and
client-forged model headers were overwritten. Chat/SSE, model catalog,
embeddings, embedding dimensions, reranking, transcription, diarization,
negative API contracts, JWT authentication across five APIs, and tier ceilings
were exercised successfully. Semantic `auto` decisions reached the B300,
H200, and H100 fixtures on all stacks, and non-chat tasks bypassed the router.

Request limits returned 429 on request 6, quotas on request 4. Envoy isolated
request buckets by user. After enabling 1.6 keyed local rules, agentgateway
also isolated users; Kuadrant used shared buckets. Token
budgets returned 429 on request 3 for Kuadrant and Envoy. agentgateway's generic
InferencePool path does not provide the AI-backend metadata needed for token
budgets. Envoy and agentgateway answered approved CORS preflights and omitted
allow-origin for unapproved origins; the Kuadrant fixture has no CORS policy.

## Remaining limitations measured

- Native routing with BBR stopped reached 8/12 models on Envoy and 5/12 on
  agentgateway. Missing embedding/reranking models are listed in the root
  README comparison. Both native paths held the tested tier ceilings.
- Kuadrant tenant nesting remained unenforced, and the forged tenant-header
  probe escaped the bucket. Envoy nested limits worked within a route and
  ignored the forged header, but its two inference routes had separate buckets.
  agentgateway 1.6 keyed local rules now nested org/team buckets and ignored
  forged tenant headers without an external service. Counters remain per proxy
  and in memory; distributed and durable quotas were not tested.
- With the semantic router stopped, Kuadrant and Envoy served explicit chat
  with HTTP 200 and rejected `auto` with 404. agentgateway returned 500 for both,
  matching its configured fail-closed policy. The probes restored the routers.
- Long-window expiration, production GPU workloads, MCP, OIDC browser flows,
  external provider backends, and telemetry export were not tested.

A successful comparison process means the probes completed; the limitations
above are measured outcomes, not claims that every feature passed.

## Repairs made while testing

1. Docker Desktop was initially stopped. Its startup resumed two unrelated
   Kind nodes and exhausted much of the available memory. Those nodes were
   stopped, and the first Kuadrant Istio-CRD timeout passed on an isolated retry.
2. Retained objects had saved client-side configurations containing managed
   metadata. Reapplying them tried to clear `resourceVersion` and failed.
   Envoy ext-proc and native-overlay reconciliation now uses server-side apply,
   taking ownership of the repository-declared fields. Successful retries
   verified the ext-proc/native update paths.
3. The Envoy base installer omitted the optional global rate-limit settings
   despite existing BackendTrafficPolicy objects. This invalidated the policies
   and blocked initial proxy configuration. The installer now retains that
   values overlay when the fixture policy exists. Restoring the settings made
   the proxy Ready, and all Envoy deployment and comparison stages completed.

The three comparison clusters were stopped after testing and retain their
upgraded state. Raw results are in ignored `compare/results/<cluster>.json`;
phase logs and retry history are in ignored `compare/results/local-tests/`.
The root README contains the merged fresh results. The inference-path atlas
and earlier causal experiments remain historical evidence.

## New release feature probes

The follow-up sweep enabled agentgateway 1.6 local CEL keys, removed the
harness's hardcoded unsupported tenant verdict, and measured all three stacks
again. Agentgateway returned 429 on Alice's sixth rate-limit request while Bob
received 200. With org cap 5 and team cap 3, team A stopped on request 4, team B
on request 3, and the second org served all three requests. Forging the org
header still returned 429. The tenant validator checks the caps against the
harness and requires keys derived from signed JWT org/team claims. The first
user-key attempt used `jwt.sub` and fell into a shared bucket: the fixture
custom scope does not emit `sub`, verified by inspecting claim presence only.
Using its explicitly mapped `preferred_username` fixes the fixture key; the
validator now checks that mapping and the user-limit expressions together.

The new Envoy probe temporarily toggles `excludeFromModelsEndpoint` on the
native route's qwen3.8-27b rule. It checks visibility before and after, sends chat
to the hidden model, then restores the original field and verifies the catalog.
The probe passed: qwen3.8-27b disappeared, its chat request returned HTTP 200,
and the catalog was restored. The root README records this result and lists
other release additions that need dedicated MCP, stream, replica or provider fixtures.
