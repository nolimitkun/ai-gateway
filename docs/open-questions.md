# Open questions

Decisions this repository has deferred, not limitations it has measured. The
comparison table in the README records what each stack *does*; this file
records the places where what it does was a side effect rather than a choice,
or where a gap could be closed and has not been.

Every entry states what was measured, on which cluster, and what the options
cost. None of them block anything today.

---

## 1. Should `model: auto` degrade or deny above the caller's tier ceiling?

**Status:** open. Currently denies.

The tier check judges the model the semantic router *picked*, not the
one the client asked for. Authorization reads `x-gateway-model-name`, and by
the time it runs that header holds the resolved model — on Envoy and Kuadrant
BBR derived it from the body the router rewrote, on agentgateway a PreRouting
transformation copied it from `x-selected-model`. No filter in any of the three
chains still knows the client said `auto`.

Measured 2026-08-27 on all three clusters. `dave` has a `medium` tier ceiling.
He sends an identical body every time and only the prompt text varies:

| prompt | router picks | Envoy | agentgateway | Kuadrant |
|---|---|---|---|---|
| "prove … irrational" | kimi-k3 (B300) | 403 | 403 | 403 |
| "refactor this python function" | deepseek-v4-flash | 200 | 200 | 200 |
| "hello there, good morning" | qwen3.8-27b | not run | 200 | 200 |

`carol` and `alice`, whose ceiling is `big`, return 200 on all three prompts on
all three stacks.

This is arguably correct — a tier claim is a ceiling, not a
suggestion. The problem is that it is opaque: dave never named `kimi-k3`,
cannot predict the 403 from his request, and the body says only "the token
token tier does not allow the resolved model tier" without naming an allowed
an allowed alternative.

Options, ascending cost:

1. **Make the denial actionable.** Name the tier and an allowed alternative in
   the message. All three stacks already carry a configurable string —
   Kuadrant's `response.unauthorized.message` in
   `kuadrant/deploy/policies/auth-policy.yaml`, and the equivalents for Envoy
   Gateway's `SecurityPolicy` and agentgateway's `AgentgatewayPolicy`. Turns a
   dead end into a retry.
2. **Degrade instead of denying.** Give the router a tier-aware model set so it
   never picks a tier the caller cannot reach. This is the behaviour
   most people assume `auto` already has. Needs the caller's identity at the
   router, which none of the three stacks passes to it today — unverified
   whether the vLLM Semantic Router v0.3.0 can take identity from a header.
3. **Leave it**, recorded as a decision rather than an accident.

Full write-up: `docs/inference-path-atlas.html` section 08.

---

## 2. Is `FailClosed` the right trade for the agentgateway semantic router?

**Status:** open. Currently `FailClosed`, and it is the one place where a
cross-stack comparison row deliberately differs.

agentgateway v1.4.1 has one external-processing slot per phase per target, so
the semantic router holds the PreRouting slot for chat and resolves the model
name that selects the pool *and* that the tier-ceiling authorization reads. Its
availability is therefore load-bearing in a way it is not on the other two
stacks.

Measured with `kubectl -n ai-demo scale deployment/semantic-router --replicas=0`:

| | result |
|---|---|
| `FailOpen` | explicit chat 200, auto 404 — but a medium-tier caller asking for `kimi-k3` **was served it**, because no filter could name the model for the authorization check |
| `FailClosed` | all chat 500, no bypass; embeddings and reranking unaffected |

`FailClosed` shipped, on the reasoning that BBR already carries it ("pool
selection is part of the serving path") and the router has taken over BBR's job
here. The cost is that `Semantic router unavailable` reads
`explicit 500 / auto 500` for agentgateway against `explicit 200 / auto 404`
elsewhere, breaking a contract that used to be identical in all three columns.

Reversing is one word on the chat arm of
`agentgateway/deploy/semantic-router/agentgateway-extproc.yaml`. Worth trying
first: whether agentgateway's `traffic.authorization` CEL can reach the request
body, which would let the tier rule test `body.model` directly and close the
gap without losing availability. A `PostRouting` BBR pass was already tried and
does not work — authorization does not see that filter's header mutation.

Full write-up: `docs/inference-path-atlas.html` section 07.

---

## 3. Should the Kuadrant profile get a CORS policy?

**Status:** open. Currently absent, and the comparison reports the absence.

Kuadrant is the only stack with no CORS configuration. A preflight measures
`405`, against `200` plus the allow-list on the other two. Istio's CORS filter
is present in the chain but unconfigured, because no Kuadrant policy in this
repository writes a rule.

This is a genuine gap in the column rather than a rendering of one: Kuadrant
has no CORS policy kind, so it would need an Istio `VirtualService` or another
`EnvoyFilter` — which is the same "one abstraction level below the other two"
trade the ext_proc attachment already demonstrates. Closing it makes the CORS
row comparable; leaving it keeps the column honest about what Kuadrant's own
API covers.

---

## 4. Deploy a rate-limit service for agentgateway?

**Status:** open. Currently reported as a gap.

agentgateway's nested org/team buckets are not expressible with what is
deployed: a `local` entry takes no key, and `conditional` is first-match-wins,
so it cannot charge an org counter and a team counter for the same request.
`rateLimit.global` *does* nest, and is the best-specified of the three stacks
for it — descriptor entries are CEL and so is the per-descriptor cost — but
`global.backendRef` is required and this repository deploys a rate limit
service for Envoy Gateway only.

So the gap is a deployment task, not a capability gap, and the comparison
currently reports it as one ("Needs an external rate limit service"). Deploying
one would let the agentgateway column show nesting and make the row a real
three-way comparison.

---

## 5. Kuadrant tenant buckets key only on something the caller controls

**Status:** measured and documented; escalation not attempted.

Three keyings were tried against the nested tenant limits on Kuadrant 1.5.2:

| keying | result |
|---|---|
| `auth.identity.org` / `.team` | unenforced — Authorino's identity metadata is not exposed to the rate-limit action in this topology |
| the `x-auth-org` / `x-auth-team` headers the `AuthPolicy` injects | unenforced — injected toward the upstream, so absent from `request.headers` when the wasm rate-limit action evaluates |
| a client-supplied `x-auth-org` / `x-auth-team` | **enforced**, and therefore forgeable — exhausting a real bucket to 429 and resending the same token with `x-auth-org: evil` returns 200 |

The only keying that binds is the one a caller controls, which is not a tenancy
control. The limits are kept, unpromoted, because they are what the comparison
measures. The open part is whether this is worth raising upstream as a Kuadrant
issue rather than only recording it here.

---

## 6. `make validate` needs an explicit interpreter

**Status:** open, pre-existing, trivial.

`make validate` fails with "PyYAML is required" under the default `python3`.
The working invocation is `make validate PYTHON=.venv/bin/python`. Deliberately
left out of scope by several changes because it predates them, which is exactly
how a papercut survives. Either default `PYTHON` to a venv when one exists, or
have the target fail with the working command in the message.

---

## 7. Retained-cluster workflow health

**Status:** operational, watch it.

Docker Desktop wedged three times in one session on 2026-08-27: containers that
would not start with no error output, and one the daemon could not kill
("tried to kill container, but did not receive an exit event", and `setns`
failures on `docker exec`). Each needed a full Docker Desktop restart, and one
kind node came back with its API server unreachable until restarted again.

The retained-cluster model — `make stop-cluster` / `make start-cluster` rather
than rebuilding — is what keeps a three-stack comparison affordable, so this is
worth watching rather than acting on immediately. If it recurs, `make down-all`
and a rebuild is more predictable than more restarts. Note also that a
just-restarted cluster produces cold-start artifacts that look like regressions:
one Envoy run reported streaming usage and `[DONE]` as `No`, and three direct
SSE requests immediately afterwards returned both.

---

## 8. Should the two stacks that can route natively keep BBR?

**Status:** open. The overlay exists and has never run against a cluster.

All three stacks deploy the same external component — Gateway API Inference
Extension BBR 1.2.1 — to copy `body.model` into a header that Gateway API
matching can see. Reading the vendored CRDs rather than the deployment, only
one of them needs it:

| Stack | Native body→model | From the CRD |
|---|---|---|
| Envoy AI Gateway | yes | `AIGatewayRoute`: the model "is extracted from the request content before the routing decision" |
| agentgateway | yes | `AgentgatewayModel.match.model` "matched against client requests"; a `Custom` provider's `backendRef` "may target only a namespace-local Service or InferencePool" |
| Kuadrant | no | no body-aware API; BBR runs from a raw `EnvoyFilter` with a per-route patch per model rule |

`make native-routing CLUSTER=<name>` installs the same twelve model-to-pool
mappings in the native API on the `native.local` hostname, leaving the BBR path
serving everything else in the same cluster, so one request can go down either.

**Measured 2026-08-28** on `ai-gw-envoy` and `ai-gw-agent`, one cluster at a
time:

| | Envoy AI Gateway | agentgateway |
|---|---|---|
| pools reached with BBR scaled to 0 | 4/4 | 4/4 |
| forged `x-ai-eg-model` | body wins, header ignored | no client-visible model header |
| tier ceiling, both rungs | Held (medium 403 / big 200; medium model 200) | Held (same) |
| catalog / `auto` | 200 / 404 | 200 / 200 |

The capability is no longer read off a schema. BBR is not required for routing
on either stack, and — the part that was genuinely open — the tier ceiling
holds on Envoy without it, so ext_authz does observe the `x-ai-eg-model` header
its own AI processor writes.

Three things were worth deciding once it had run. Two now have answers:

1. **Whether Envoy can express the tier ceiling at all without BBR — answered:
   it can.** The filter-order question resolved in the overlay's favour, and
   both rungs bind on `native.local` with no body-based router in the path.
   What stays open is whether to depend on it: nothing in the Envoy AI Gateway
   schema *promises* that ordering, so this is one measured configuration on
   one version, not a contract.
2. **Whether agentgateway's per-model authorization is strictly better.**
   `AgentgatewayModel.policies.authorization` attaches the rule to the model
   rather than to a header, so there is no routing header to forge and no
   filter order to depend on. It held, so it is the strongest form of the tier
   ceiling in the repository, and the header-based rules on the other two
   stacks read as a workaround rather than the reference design. Whether to
   say so in the comparison is the open part.
3. **Whether to drop BBR from those two clusters entirely — measured, and it
   is not a deletion.** Two runs on `ai-gw-envoy` settled both halves.

   The capability half is now fully answered in the overlay's favour. With the
   semantic router attached to the native `AIGatewayRoute` and no body-based
   router anywhere in that path, `auto` resolved to kimi-k3, deepseek-v4-flash
   and qwen3.8-27b and reached b300, h200 and h100 — identical to the BBR
   control path. So Envoy AI Gateway's own processor extracts the model the
   router rewrote into the body, and `auto`, the one thing the first run showed
   not following a caller to the native hostname, follows once the router is
   attached there. Nothing about BBR is irreplaceable on that stack.

   The removal half failed, and failed unsafely. Scaling BBR to zero, deleting
   both of its `EnvoyExtensionPolicy` attachments, and pointing the
   `AIGatewayRoute` at every hostname did not hand the ordinary path over to
   it: `kserve-mock` still won, its twelve model rules no longer matched
   anything because nothing writes `x-gateway-model-name`, and every request
   fell through to the shared CPU fixture with HTTP 200. Worse, `dave` — a
   medium-tier token — was served `kimi-k3`, which must be 403. The
   `SecurityPolicy` gates on the same header, so removing BBR removed the
   authorization input at the same time as the routing input, and both failed
   open and silent.

   The redesign was then attempted in the safe order — make the native
   mechanism primary and move the tier rules onto the header it writes, prove
   a medium-tier token is still refused a big model, and only then delete BBR.
   It got through the safety gate on both stacks and failed after it, for a
   different reason on each.

   **Envoy: BBR is necessary.** Made the `AIGatewayRoute` primary, slimmed
   `kserve-mock` to the catalog and speech paths it cannot match, and moved the
   `SecurityPolicy` to `x-ai-eg-model`. `dave` was correctly refused kimi-k3
   and served deepseek-v4-flash with the BBR pod scaled to zero, so the
   authorization half works. Routing does not: the native path serves **8 of
   the 12 models**. Both h100 embedding models answer 500 and both rerankers
   404, on a path where the BBR route serves all twelve — verified back to back
   in the same cluster minutes apart. And the semantic router cannot be scoped:
   `AIGatewayRoute.rules` has no `name` field in either v1alpha1 or v1beta1, so
   the generated HTTPRoute rules carry no section names and an
   `EnvoyExtensionPolicy` cannot target a subset of them. Attaching the
   chat-only router to the whole route makes it answer embeddings and rerank
   with its own "endpoint not found"; splitting into two AIGatewayRoutes gives
   two `PathPrefix /` catch-alls that fight, with the same result; omitting the
   router removes `auto` entirely.

   **agentgateway: the native path is complete but listener-bound.** It routes
   **12 of 12** with BBR at zero, keeps `auto` and the catalog, has no
   client-visible model header to forge, and carries the tier ceiling on the
   model resource rather than on a header. But that is all on its own `native`
   listener. `AgentgatewayModel`s re-parented to the default `http` listener
   404 every model, with `sectionName` removed and with it set to `http`, and
   still 404 after the `ai.routes` formats policy was copied onto that
   listener. The default path's chat routing survives BBR only because the
   semantic router's transformation writes `x-gateway-model-name` itself; the
   task paths fall to the shared fixture.

   agentgateway was then tried on its own, since its native path is the
   complete one. Two routes to a removal, both measured, both dead ends:

   * **Models on the default listener.** 404 for all twelve, with
     `sectionName` removed, with it set to `http`, and again after the
     `ai.routes` formats policy was copied onto that listener.
   * **A transformation reading the body.** `traffic.transformation` computes
     header values from CEL, so `x-gateway-model-name: request.body.model`
     would replace BBR outright. The policy is accepted and reports
     `Attached to all targets`, and it silently does nothing: with BBR removed,
     the five chat models still routed — the semantic router writes that header
     itself — and all seven task models fell to the shared CPU fixture.
     Replacing the same expression with the literal `"bge-m3"` sent an
     unrelated embedding request to the l40s pool, which answered 404 for a
     model it does not serve. So the arm fires and CEL evaluates; what does not
     work is reading the request body. The CRD agrees on a second reading: its
     body access is `transformation.request.body`, which *writes* JSON fields,
     and the header `value` examples are all headers, `jwt` claims and paths.

   Removing BBR from agentgateway therefore costs embeddings and rerank their
   accelerator pools — seven models onto the shared fixture — so it was not
   shipped. This is the same failure class as two PreRouting ext_proc policies:
   accepted, attached, inert.

   The two rows the comparison did not cover were then measured, because
   without them "agentgateway's native path is functionally complete on its own
   hostname" was a claim rather than a result. Both follow the caller:

   * **Multipart speech.** HTTP 200 on `native.local`, the same shared fixture
     that answers it on the default host. Every transcription model is
     small-tier, so there is no accelerator pool to lose. Now a permanent part
     of the `native_gaps` row on both stacks.
   * **Rate limiting.** 429 on request 6 of 8 on `native.local`, the same
     ceiling the default host enforces, and the two share one bucket — eight
     requests to the default host immediately afterwards were refused from the
     first. This was the opposite of the expectation: `kserve-mock-rate-limit`
     targets `HTTPRoute kserve-mock`, which does not serve that hostname, so
     the limit was predicted not to apply. It does. The first attempt to
     measure it was confounded by running the default host first and spending
     the shared bucket; the answer above is from a re-run after the
     one-minute window reset, with `native.local` going first.

   Rate limiting is deliberately left out of the harness. Probing it would
   spend the same bucket the `request_limit` row measures, and a row that
   corrupts another row is worse than a row recorded by hand.

   So agentgateway's native path is a complete functional replacement for BBR —
   twelve of twelve models, `auto`, the catalog, speech, the tier ceiling, no
   forgeable header, and rate limiting — on `native.local`. What is not
   replaceable is making it the default path, and that is a listener-binding
   limitation rather than a missing capability.

   So on all three stacks BBR still owns the default path. The overlay is a
   real capability demonstration on its own hostname, not a drop-in
   replacement, and "deliberately additive" turns out to have been the correct
   design rather than a cautious one. All three clusters were restored and
   re-verified with full comparison runs.
