# Open questions

Decisions this repository has deferred, not limitations it has measured. The
comparison table in the README records what each stack *does*; this file
records the places where what it does was a side effect rather than a choice,
or where a gap could be closed and has not been.

Every entry states what was measured, on which cluster, and what the options
cost. None of them block anything today.

---

## 1. Should `model: auto` degrade or deny when the caller is not entitled?

**Status:** open. Currently denies.

The entitlement check judges the model the semantic router *picked*, not the
one the client asked for. Authorization reads `x-gateway-model-name`, and by
the time it runs that header holds the resolved model — on Envoy and Kuadrant
BBR derived it from the body the router rewrote, on agentgateway a PreRouting
transformation copied it from `x-selected-model`. No filter in any of the three
chains still knows the client said `auto`.

Measured 2026-08-27 on all three clusters. `dave` is in `acme/support` — a
`model-users` member, not an admin, not on the entitled `acme/research` team.
He sends an identical body every time and only the prompt text varies:

| prompt | router picks | Envoy | agentgateway | Kuadrant |
|---|---|---|---|---|
| "prove … irrational" | kimi-k3 (B300) | 403 | 403 | 403 |
| "refactor this python function" | deepseek-v4-flash | 200 | 200 | 200 |
| "hello there, good morning" | qwen3.8-27b | not run | 200 | 200 |

`carol` (entitled) and `alice` (admin) return 200 on all three prompts on all
three stacks.

This is arguably correct — an entitlement should be a ceiling, not a
suggestion. The problem is that it is opaque: dave never named `kimi-k3`,
cannot predict the 403 from his request, and the body says only "the token
identity is not allowed to call this model class" without naming the class or
an allowed alternative.

Options, ascending cost:

1. **Make the denial actionable.** Name the class and an allowed alternative in
   the message. All three stacks already carry a configurable string —
   Kuadrant's `response.unauthorized.message` in
   `kuadrant/deploy/policies/auth-policy.yaml`, and the equivalents for Envoy
   Gateway's `SecurityPolicy` and agentgateway's `AgentgatewayPolicy`. Turns a
   dead end into a retry.
2. **Degrade instead of denying.** Give the router an entitlement-aware model
   set so it never picks a class the caller cannot reach. This is the behaviour
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
name that selects the pool *and* that the B300 authorization rule reads. Its
availability is therefore load-bearing in a way it is not on the other two
stacks.

Measured with `kubectl -n ai-demo scale deployment/semantic-router --replicas=0`:

| | result |
|---|---|
| `FailOpen` | explicit chat 200, auto 404 — but an unentitled caller asking for `kimi-k3` **was served it**, because no filter could name the model for the authorization check |
| `FailClosed` | all chat 500, no bypass; embeddings and reranking unaffected |

`FailClosed` shipped, on the reasoning that BBR already carries it ("pool
selection is part of the serving path") and the router has taken over BBR's job
here. The cost is that `Semantic router unavailable` reads
`explicit 500 / auto 500` for agentgateway against `explicit 200 / auto 404`
elsewhere, breaking a contract that used to be identical in all three columns.

Reversing is one word on the chat arm of
`agentgateway/deploy/semantic-router/agentgateway-extproc.yaml`. Worth trying
first: whether agentgateway's `traffic.authorization` CEL can reach the request
body, which would let the B300 rule test `body.model` directly and close the
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
