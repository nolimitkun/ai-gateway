# KServe LLMInferenceService gateway comparison

This repository runs one KServe `LLMInferenceService` through three Gateway API
stacks and compares chat completions, embeddings, reranking, and speech-to-text
over the resulting data path:

| Stack | Pinned version | Local endpoint |
|---|---:|---|
| OpenShift profile | Kuadrant 1.5.2 / Istio 1.29.2 / Envoy proxy | <http://localhost:8082> |
| Envoy AI Gateway | Envoy AI Gateway 1.0.0 / Envoy Gateway 1.8.1 | <http://localhost:8080> |
| agentgateway | agentgateway 1.4.1 | <http://localhost:8081> |
| shared model control plane | KServe 0.20.0 | one installation per cluster |

There is only one inference path in the repository. KServe owns the workload,
Service, llm-d endpoint picker, and `InferencePool`; each gateway receives the
same `HTTPRoute` to that pool. The route matches `/v1`, so every mock API is
tested through the identical gateway and endpoint-selection chain.

Kuadrant is not itself a proxy. The OpenShift profile approximates OpenShift
AI with Connectivity Link by combining Kuadrant policy, an Istio Gateway API
control plane, an Envoy gateway proxy, and a shared
`Gateway/openshift-ai-inference` in `openshift-ingress`. It attaches a
`RateLimitPolicy` to the KServe route.

## Quick start

Prerequisites: Docker, kind, kubectl, Helm, curl, and Python 3.

```bash
make up
make compare
```

`make up` creates three kind clusters. The first run downloads container images
but all Helm charts are pinned and vendored in the repository.

For the OpenShift profile, installation order matters:

1. install the core Gateway API and Inference Extension CRDs;
2. install Istio with Gateway API Inference Extension support enabled;
3. install Kuadrant and its policy operands;
4. create the shared `openshift-ai-inference` Gateway;
5. apply the KServe overlay that references the shared Gateway across
   namespaces.

OpenShift's Ingress Operator is not available on kind. Istio therefore stands
in for the OpenShift Gateway controller and manages the Envoy proxy directly.
The resource names, shared-Gateway topology, route attachment, policy layer,
and KServe data path follow the OpenShift AI shape.

A direct chat request is identical for every gateway endpoint:

```bash
curl http://localhost:8082/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"mock-kserve","messages":[{"role":"user","content":"hello"}]}'
```

Use ports `8080` and `8081` for Envoy AI Gateway and agentgateway. No
gateway-selection header is required because there is no alternate route.

### Mock inference APIs

The shared CPU runtime exposes five deterministic APIs:

| Capability | Endpoint | Request format | Mock behavior |
|---|---|---|---|
| model catalog | `GET /v1/models` | optional `task` and `tier` query filters | lists every mock model with its task, tier, and capability metadata |
| chat | `POST /v1/chat/completions` | OpenAI JSON, including streaming | identifies the selected KServe pod and returns tier-dependent usage |
| embeddings | `POST /v1/embeddings` | OpenAI JSON with string or string-array `input` | returns normalized, deterministic vectors at the model's dimensionality |
| reranking | `POST /v1/rerank` | JSON with `query`, `documents`, and optional `top_n` | ranks documents by deterministic token overlap |
| speech-to-text | `POST /v1/audio/transcriptions` | OpenAI-style multipart `file` and `model` fields, plus `response_format`, `diarization`, and `num_speakers` | returns a deterministic transcript, timestamped segments, and speaker turns |

Use any of the three gateway base URLs with the same requests:

```bash
BASE=http://localhost:8082

curl "$BASE/v1/models?task=chat&tier=big"

curl "$BASE/v1/embeddings" \
  -H 'content-type: application/json' \
  -d '{"model":"mock-embedding","input":["gateway inference","KServe routing"]}'

curl "$BASE/v1/rerank" \
  -H 'content-type: application/json' \
  -d '{"model":"mock-reranker","query":"gateway inference","documents":["unrelated","gateway inference routing"],"top_n":1}'

curl "$BASE/v1/audio/transcriptions" \
  -F 'file=@sample.wav;type=audio/wav' \
  -F 'model=mock-whisper'
```

### Mock model catalog

Every model below is a fixture. No weights are loaded and no vendor API is
contacted: the identifiers, tiers, and capability flags exist so gateway model
routing, allow-lists, and OpenAI-compatible clients can be tested against a
realistic model list. A request for a model that is not in the catalog returns
HTTP 404 with `model_not_found`, and a model used for the wrong task returns
HTTP 400.

Chat models, by tier:

| Model | Tier | Accelerator | Context window | Max output |
|---|---|---|---:|---:|
| `kimi-k3` | big | B300 | 262144 | 16384 |
| `glm-5.3` | big | B300 | 204800 | 16384 |
| `deepseek-v4-pro` | big | B300 | 163840 | 32768 |
| `deepseek-v4-flash` | medium | H200 | 131072 | 8192 |
| `qwen3.8-27b` | small | H100 | 65536 | 8192 |
| `mock-kserve` | fixture | CPU | 8192 | 1024 |

The tier is visible in the response: `mock_tier`, the reported
`completion_tokens`, and the assistant message all follow it, so a routing rule
that is supposed to send a request to a small model can be checked from the
response alone. `max_tokens` above a model's output limit returns HTTP 400 with
`context_length_exceeded`; below the tier's own completion length it truncates
the answer, reports the requested token count, and finishes with `length`.

```bash
curl "$BASE/v1/chat/completions" \
  -H 'content-type: application/json' \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hello"}]}'
```

Speech-to-text models, with ASR and diarization:

| Model | Tier | Accelerator | ASR | Diarization | Max speakers |
|---|---|---|---|---|---:|
| `whisper-large-v3` | big | L40S | yes | yes | 8 |
| `voxtral-small-24b` | big | H100 | yes | yes | 8 |
| `voxtral-mini-3b` | small | L40S | yes | no | 1 |
| `mock-whisper` | fixture | CPU | yes | yes | 4 |

`response_format=verbose_json` adds timestamped segments. `diarization=true`
labels each segment with a `SPEAKER_NN` turn and adds a per-speaker summary;
`num_speakers` pins the speaker count, and the transcript grows to give every
pinned speaker a turn. Diarization on an ASR-only model such as
`voxtral-mini-3b` returns HTTP 400. Whisper has no native diarization, so that
flag mirrors the usual whisper plus speaker-attribution pipeline rather than a
single model.

```bash
curl "$BASE/v1/audio/transcriptions" \
  -F 'file=@meeting.wav;type=audio/wav' \
  -F 'model=whisper-large-v3' \
  -F 'response_format=verbose_json' \
  -F 'diarization=true' \
  -F 'num_speakers=3'
```

Embedding and reranking models for retrieval-augmented generation:

| Model | Task | Tier | Accelerator | Dimensions | Shortened dimensions |
|---|---|---|---|---:|---|
| `qwen3-embedding-8b` | embedding | big | H100 | 4096 | yes |
| `e5-mistral-7b-instruct` | embedding | big | H100 | 4096 | no |
| `bge-m3` | embedding | medium | L40S | 1024 | no |
| `jina-embeddings-v3` | embedding | medium | L40S | 1024 | yes |
| `nomic-embed-text-v2-moe` | embedding | small | L40S | 768 | yes |
| `mock-embedding` | embedding | fixture | CPU | 8 | no |
| `bge-reranker-v2-m3` | rerank | medium | L40S | n/a | n/a |
| `jina-reranker-v2-base-multilingual` | rerank | small | L40S | n/a | n/a |
| `mock-reranker` | rerank | fixture | CPU | n/a | n/a |

Models that support shortened dimensions accept a `dimensions` value below
their native width, which is how Matryoshka embeddings are truncated in a RAG
index; the others reject it. A full RAG path — embed, retrieve, rerank — can be
exercised end to end through any gateway:

```bash
curl "$BASE/v1/embeddings" \
  -H 'content-type: application/json' \
  -d '{"model":"qwen3-embedding-8b","input":["gateway inference"],"dimensions":1024}'

curl "$BASE/v1/rerank" \
  -H 'content-type: application/json' \
  -d '{"model":"bge-reranker-v2-m3","query":"gateway inference","documents":["unrelated","gateway inference routing"],"top_n":1}'
```

### Accelerator pools

Models of different sizes do not share a card. `make pools` adds one KServe
serving pool per accelerator class alongside the shared service, so each model
is served from the pool its weights are sized for:

| Pool | Accelerator | Tier | Models |
|---|---|---|---|
| `kserve-b300` | NVIDIA B300 | big | `kimi-k3`, `glm-5.3`, `deepseek-v4-pro` |
| `kserve-h200` | NVIDIA H200 | medium | `deepseek-v4-flash` |
| `kserve-h100` | NVIDIA H100 | small and large others | `qwen3.8-27b`, `qwen3-embedding-8b`, `e5-mistral-7b-instruct`, `voxtral-small-24b` |
| `kserve-l40s` | NVIDIA L40S | light others | `bge-m3`, `jina-embeddings-v3`, `nomic-embed-text-v2-moe`, both rerankers, `whisper-large-v3`, `voxtral-mini-3b` |
| `kserve-mock` | CPU | fixture | `mock-kserve`, `mock-embedding`, `mock-reranker`, `mock-whisper` |

Each pool is a full `LLMInferenceService`: its own workload, endpoint picker,
`InferencePool`, and `HTTPRoute`. A pool replica serves only the models of its
class — `ACCELERATOR` in the pod environment selects the slice of the catalog —
and returns HTTP 404 `model_not_served_here`, naming the correct class, for
anything else. That makes a misrouted request visible instead of silently
answered by the wrong hardware.

Requests select a pool with an `x-model-class` header, which Istio/Envoy, Envoy
AI Gateway, and agentgateway all match identically; body-based model routing is
provider-specific. Requests without the header keep going to the shared
`kserve-mock` route:

```bash
curl "$BASE/v1/chat/completions" \
  -H 'content-type: application/json' \
  -H 'x-model-class: b300' \
  -d '{"model":"kimi-k3","messages":[{"role":"user","content":"hello"}]}'
```

The response reports the class that served it:

```json
{"model": "kimi-k3", "mock_tier": "big", "mock_accelerator": "b300", "...": "..."}
```

`GET /v1/models` on a pool lists only that pool's models, and `/health` reports
its accelerator class, so a pool's inventory can be read from the pool itself.

kind has no accelerators, so `make pools` deploys the pools with the CPU mock
and CPU requests: the topology, routing, and per-pool catalogs are real, the
hardware is not. `kserve/overlays/gpu` carries the same pools with the node
selector, `nvidia.com/gpu` request, and toleration each class needs on a GPU
cluster, where the mock container is also replaced by a real runtime.

```bash
make pools        # add the four accelerator pools to all three clusters
make pools-down   # remove them; the shared KServe path stays up
```

Useful targets:

```bash
make status
make compare
make test
make agent-ui
make down
```

`make agent-ui` port-forwards the agentgateway UI to
<http://localhost:15000/ui>.

## Deployment dependencies

This repository has one automated deployment target and one documented
platform variant. Do not install both KServe distributions in the same
cluster.

| Condition | Open-source KServe reference | Red Hat OpenShift AI |
|---|---|---|
| Intended use | local development and gateway comparison | OpenShift-managed model serving |
| Kubernetes platform | kind with Kubernetes 1.36.1 | OpenShift Container Platform 4.19.9 or later |
| KServe distribution | upstream KServe 0.20.0, installed by this repository | KServe managed by Red Hat OpenShift AI |
| Platform version | not applicable | Red Hat OpenShift AI 3.5 |
| LLM API | `serving.kserve.io/v1alpha2` `LLMInferenceService` in this pinned upstream release | use the `LLMInferenceService` API installed by the selected OpenShift AI release; currently documented as `serving.kserve.io/v1alpha1` |
| Gateway prerequisite | this repository installs each GatewayClass and Gateway | a `GatewayClass` and `Gateway/openshift-ai-inference` in `openshift-ingress` |
| Model runtime | multi-task CPU mock for repeatable API tests; use task-specific production runtimes | an enabled OpenShift AI serving runtime, normally vLLM for LLMs |
| Installation command | `make up` | install and configure OpenShift AI; do not run `scripts/install-kserve.sh` |
| Validation status here | automated and tested | compatibility guidance only; not automated by this repository |

### Open-source KServe path

The automated kind deployment requires:

- Docker, kind, kubectl, Helm, curl, and Python 3 on the workstation;
- Gateway API and Gateway API Inference Extension CRDs;
- cert-manager 1.17.0;
- upstream KServe 0.20.0;
- one supported gateway stack: the OpenShift-aligned Kuadrant profile, Envoy
  AI Gateway, or agentgateway;
- enough local capacity for three kind control-plane nodes and their controller
  and proxy images.

The checked-in CPU fixture does not require a GPU, object store, or model
download. A production vLLM deployment additionally needs supported
accelerators and drivers, model storage or a model registry, image-pull
credentials, and appropriately sized memory and ephemeral storage.

### OpenShift AI path

For the same distributed-inference architecture on OpenShift, target
OpenShift Container Platform 4.19.9 or later with Red Hat OpenShift AI 3.5.
The OpenShift AI model serving platform must already be enabled and must own
the KServe installation.

Additional conditions from the OpenShift AI distributed-inference deployment
guide:

- OpenShift Service Mesh v2 must not be installed for this Gateway API
  distributed-inference topology;
- the cluster must provide `GatewayClass` and
  `Gateway/openshift-ai-inference` in `openshift-ingress`;
- bare-metal clusters need an external entry point for the Gateway Service,
  such as MetalLB;
- authentication must be configured for the inference endpoint;
- LeaderWorkerSet is optional for ordinary deployments and is required when a
  server's tensor, pipeline, or data parallelism spans more than eight
  accelerators.

The upstream manifest is therefore a schema reference, not a directly
portable OpenShift installation bundle. For OpenShift, use the API version
served by the OpenShift AI-managed KServe operator, reference the
`openshift-ai-inference` Gateway, replace the mock container with an enabled
serving runtime, and apply the cluster's storage, accelerator, security, and
authentication requirements.

Version conditions were checked against the
[OpenShift AI 3.5 distributed inference guide](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/deploy_models_using_distributed_inference_with_llm-d/deploying-models-using-distributed-inference_distributed-inference).
Confirm the Red Hat supported-configuration matrix for the exact z-stream and
accelerator before a production deployment.

## Architecture

Each box below exists independently in each kind cluster. The model, scheduler,
and pool configuration are shared. The OpenShift profile applies a small
Kustomize overlay that changes the Gateway references and supplies the trusted
EPP certificate chain required by Istio.

```mermaid
flowchart LR
  CLIENT["OpenAI-compatible client"]

  subgraph Gateways["Gateway stack (one per cluster)"]
    KU["Kuadrant policies<br/>RateLimitPolicy"]
    KUOS["OpenShift profile<br/>Istio control plane + Envoy proxy"]
    EAIG["Envoy AI Gateway<br/>without Kuadrant policy"]
    AG["agentgateway<br/>controller + Rust data plane"]
  end

  subgraph GatewayAPI["Gateway API"]
    GW["Gateway<br/>gateway.networking.k8s.io/v1"]
    ROUTE["HTTPRoute/kserve-mock<br/>gateway.networking.k8s.io/v1"]
    POOL["InferencePool/kserve-mock-inference-pool<br/>inference.networking.k8s.io/v1"]
  end

  subgraph KServe["KServe 0.20.0"]
    CTRL["LLMInferenceService controller"]
    LLMISVC["LLMInferenceService/kserve-mock<br/>serving.kserve.io/v1alpha2"]
    EPP["llm-d EPP<br/>Deployment + Service"]
    WORKLOAD["model workload<br/>Deployment + Service"]
    PODS["2 multi-task inference pods<br/>catalog + chat + embeddings + rerank + STT"]
  end

  KU -->|attaches policy| ROUTE
  KU -->|configures| KUOS
  CLIENT --> KUOS
  CLIENT --> EAIG
  CLIENT --> AG
  KUOS --> GW
  EAIG --> GW
  AG --> GW
  GW --> ROUTE
  ROUTE --> POOL
  POOL -->|endpointPickerRef| EPP
  EPP -->|selects endpoint| PODS
  GW -->|forwards to selected Pod IP| PODS

  CTRL -->|watches| LLMISVC
  LLMISVC -->|references| GW
  LLMISVC -->|references| ROUTE
  CTRL -->|reconciles| POOL
  CTRL -->|reconciles| EPP
  CTRL -->|reconciles| WORKLOAD
  WORKLOAD --> PODS
```

## Resource schema

The repository declares only the portable resources needed around KServe:

| Owner | Resource | Purpose |
|---|---|---|
| repository | `GatewayClass` / `Gateway` | selects each gateway implementation |
| repository | `HTTPRoute/kserve-mock` | sends all chat, embedding, reranking, and STT `/v1` traffic to the KServe pool |
| repository | `LLMInferenceService/kserve-mock` | desired model, workload, replicas, router, and scheduler |
| repository | two `LLMInferenceServiceConfig` objects | replace GPU/vLLM defaults with the complete CPU fixture |
| repository, Kuadrant cluster only | `RateLimitPolicy/kserve-mock` | proves Kuadrant policy attachment without constraining the sample |
| repository, Kuadrant cluster only | `Gateway/openshift-ai-inference` | reproduces the OpenShift shared-Gateway name and namespace |
| repository, Kuadrant cluster only | Kustomize overlay | changes the route and `LLMInferenceService` Gateway references to `openshift-ingress` |
| repository, Kuadrant cluster only | cert-manager `Issuer` / `Certificate` objects | issue a private CA and DNS-valid server certificate for the KServe endpoint picker |
| repository, Kuadrant cluster only | `BackendTLSPolicy/kserve-mock-epp` | lets Istio verify KServe's TLS-secured endpoint picker |
| KServe controller | workload `Deployment` and `Service` | runs two model replicas |
| KServe controller | scheduler `Deployment` and `Service` | runs the llm-d endpoint picker |
| KServe controller | `InferencePool` and scheduler RBAC | exposes endpoint-aware routing to the gateway |

The core desired state is:

```yaml
apiVersion: serving.kserve.io/v1alpha2
kind: LLMInferenceService
metadata:
  name: kserve-mock
  namespace: ai-demo
spec:
  replicas: 2
  model:
    uri: hf://local/kserve-mock
    name: mock-kserve
  router:
    gateway:
      refs:
        - name: ai-gateway
    route:
      http:
        refs:
          - name: kserve-mock
    scheduler:
      pool:
        spec:
          endpointPickerRef:
            name: kserve-mock-epp-service
            port:
              number: 9002
```

See [kserve/README.md](kserve/README.md) for the fixture-specific choices and
the production model upgrade path.

For the OpenShift profile, the Gateway portion is overlaid as:

```yaml
router:
  gateway:
    refs:
      - name: openshift-ai-inference
        namespace: openshift-ingress
```

## What the comparison measures

`make compare` checks:

- `Gateway` Programmed and `HTTPRoute` Accepted/ResolvedRefs conditions;
- `LLMInferenceService` Ready and its two workload replicas;
- KServe controller ownership of the generated workload, Services, scheduler,
  and pool;
- successful distribution across both model pods;
- OpenAI streaming with a usage chunk;
- the model catalog on `GET /v1/models` and tier-correct chat routing across the
  big, medium, and small models;
- OpenAI-compatible embeddings with deterministic 8-dimensional vectors;
- RAG embedding models at their native 4096- and 1024-dimensional widths;
- deterministic reranking and `top_n` ordering;
- multipart speech-to-text upload and response handling;
- speaker diarization with consistent segment and speaker labels;
- local p50 request latency;
- Kuadrant `RateLimitPolicy` readiness.

Raw Markdown results are written to `compare/results/`. The latency value is a
local smoke-test against a zero-delay Python runtime. It is useful for catching
regressions in this fixture, not for ranking production gateways.

The first column is an OpenShift architectural analogue: Kuadrant policy,
Istio control plane, and Envoy proxy. The second uses Envoy AI Gateway and
Envoy Gateway directly. The third uses agentgateway's Rust proxy. The first
column is intended for topology and compatibility comparison, not for claiming
Red Hat product certification.

## Latest result

The validated run on 24 August 2026 used 30 requests per gateway:

| Check | OpenShift profile | Envoy AI Gateway | agentgateway |
|---|---:|---:|---:|
| successful requests | 30/30 | 30/30 | 30/30 |
| selected model pods | 2 | 2 | 2 |
| streaming usage | yes | yes | yes |
| embeddings | yes | yes | yes |
| reranking | yes | yes | yes |
| speech-to-text | yes | yes | yes |
| local chat p50 | 299 ms | 230 ms | 307 ms |

All Gateways were Programmed, all routes were Accepted/ResolvedRefs, and all
`LLMInferenceService` objects were Ready with 2/2 workload replicas. See the
[raw comparison result](compare/results/comparison-20260824-173720.md).

## Why three clusters

The gateway charts own cluster-scoped Gateway API and extension CRDs on
different release matrices. One cluster per stack prevents one installer from
upgrading another stack's CRDs and makes teardown deterministic.

## Repository layout

```text
clusters/             three kind cluster definitions
kuadrant/             OpenShift-style Gateway overlay, Istio provider, and Kuadrant policy
envoy-ai-gateway/     pinned charts, values, and Gateway
agentgateway/         pinned charts, values, and Gateway
kserve/               controller charts, LLMInferenceService, route, and presets
kserve/pools/         one serving pool per accelerator class
kserve/overlays/gpu/  accelerator placement for those pools on a GPU cluster
mock-llm/              deterministic multi-task CPU runtime and tests
scripts/               install and deployment orchestration
compare/               single three-gateway KServe comparison and raw results
```

## Production model

The laptop fixture disables storage initialization and mounts a small Python
server from a ConfigMap. The catalog is a fixture as well: one process answers
for every model identifier it advertises. Serving chat, embeddings, reranking,
and STT from one
`LLMInferenceService` is deliberately a gateway integration fixture, not a
production model topology. For production, deploy a model runtime suited to
each task, give each workload its own scaling and accelerator profile, and
route the corresponding API to that backend. `make pools` and
`kserve/overlays/gpu` show that split: one `LLMInferenceService` per
accelerator class, each with its own endpoint picker, pool, and route. For an LLM, restore the KServe
vLLM runtime preset, GPU resources, storage initialization, and telemetry-aware
scheduler plugins.

References:

- [KServe LLMInferenceService installation](https://kserve.github.io/website/docs/install/llmisvc-install)
- [KServe LLMInferenceService architecture](https://kserve.github.io/website/docs/concepts/architecture/control-plane-llmisvc)
- [Kuadrant overview](https://docs.kuadrant.io/)
- [Kuadrant Helm installation](https://docs.kuadrant.io/dev/install-helm/)
- [OpenShift AI distributed inference](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/deploy_models_using_distributed_inference_with_llm-d/deploying-models-using-distributed-inference_distributed-inference)
- [OpenShift Gateway API implementation](https://docs.redhat.com/en/documentation/openshift_container_platform/4.19/html-single/ingress_and_load_balancing/ingress_and_load_balancing)
