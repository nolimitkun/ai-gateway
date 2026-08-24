# KServe fixture

The same KServe 0.20.0 model, multi-task mock runtime, and scheduler
configuration is applied to the OpenShift-aligned Kuadrant, Envoy AI Gateway,
and agentgateway clusters.

## Files

- `manifests/llmisvc.yaml` defines two CPU runtime replicas and the scheduler.
- `pools/kserve-{b300,h200,h100,l40s}.yaml` add one serving pool per
  accelerator class, each with its own route, workload, endpoint picker, and
  `InferencePool`.
- `pools/kustomization.yaml` applies all four pools; `make pools` deploys them.
- `overlays/gpu/kustomization.yaml` adds the node selector, toleration, and
  `nvidia.com/gpu` request to the mock pools for placement testing. It does not
  turn the Python fixture into a production model server.
- `production/` defines the pinned vLLM runtime and four real, task-specific
  `LLMInferenceService` resources for chat, embeddings, reranking, and ASR.
- `../kuadrant/pools-overlay/` is the OpenShift-profile variant of the pools,
  with one cert-manager-issued endpoint picker certificate per pool.
- `manifests/route.yaml` is the only inference route in the repository.
- `manifests/cpu-presets.yaml` prevents KServe's GPU/vLLM presets from being
  merged into this complete laptop fixture.
- `kustomization.yaml` is the shared KServe base.
- `../kuadrant/kserve-overlay/kustomization.yaml` changes the Gateway
  references to `openshift-ai-inference` in `openshift-ingress` and mounts a
  cert-manager-issued EPP certificate.
- `../kuadrant/kserve-overlay/epp-certificate.yaml` creates the private CA and
  DNS-valid EPP server certificate used by the OpenShift profile.
- `manifests/envoy-inferencepool-rbac.yaml` lets Envoy Gateway watch
  `InferencePool`; Istio and agentgateway install equivalent RBAC.
- `values/kserve-llmisvc.values.yaml` keeps KServe from replacing the gateway
  stacks' Gateway API and inference-extension CRDs.

KServe reconciles the `LLMInferenceService` into:

```text
LLMInferenceService/kserve-mock
├── Deployment/kserve-mock-kserve (2 model pods)
├── Service/kserve-mock-kserve-workload-svc
├── Deployment/kserve-mock-kserve-router-scheduler
├── Service/kserve-mock-epp-service
├── InferencePool/kserve-mock-inference-pool
└── scheduler ServiceAccount and RBAC
```

and, for each accelerator class deployed by `make pools`:

```text
LLMInferenceService/kserve-b300     (kimi-k3, glm-5.3, deepseek-v4-pro)
LLMInferenceService/kserve-h200     (deepseek-v4-flash)
LLMInferenceService/kserve-h100     (qwen3.8-27b and the large embedding/audio models)
LLMInferenceService/kserve-l40s     (light embedding, reranking, and speech models)
```

The data path is:

```text
Gateway -> HTTPRoute -> InferencePool -> EPP -> selected model pod
```

The shared `kserve-mock` route matches `/v1` alone, and each pool route matches
`/v1` plus an `x-model-class` header, so the pool routes take precedence when
the header is present and the shared path is unchanged when it is not. A pool
replica gets `ACCELERATOR` in its environment and serves only the models of
that class; anything else returns HTTP 404 `model_not_served_here`.

The shared `HTTPRoute` matches `/v1`, which integrates all mock capabilities
with every gateway without provider-specific paths:

| Endpoint | Compatibility shape |
|---|---|
| `/v1/models` | OpenAI model list and retrieve, with task and tier metadata |
| `/v1/chat/completions` | OpenAI chat and server-sent event streaming |
| `/v1/embeddings` | OpenAI embeddings response list |
| `/rerank`, `/v1/rerank`, `/v2/rerank` | vLLM query/documents schema, token usage, and opt-in returned documents |
| `/v1/audio/transcriptions` | OpenAI multipart transcription upload, with segments and diarization |

One process answers for every model a pool serves. The tiered chat, STT, and
retrieval model identifiers are fixtures for gateway routing and allow-list
tests, not real weights; see the mock model catalog in the root
[README](../README.md).

## Why a mock runtime

This repository validates KServe ownership, readiness, scheduling, Gateway API
integration, pod selection, streaming, embeddings, reranking, and multipart
audio uploads on a CPU laptop. Storage initialization is disabled and
`mock-llm/server.py` is mounted into the KServe-owned pods.

That combined runtime is not a model-server recommendation. Production chat,
embedding, reranking, and transcription models normally have different
runtimes, scaling profiles, and accelerator requirements. `pools/` models
accelerator routing while keeping the CPU mock as the container on kind;
`overlays/gpu/` only validates placement and resource requests. The deployable
reference is `production/`: each task gets its own vLLM process, real Hugging
Face URI, storage initializer, GPU resources, health probes, cache and shared
memory volumes, and KServe scheduler. Its routes use `x-model-service` because
one accelerator class can host more than one independently scaled model.

The mock intentionally adds `mock_*` observability fields and optional
`diarization`/`num_speakers` transcription fields. Clients must not depend on
those in production. Everything else covered by
`scripts/validate-vllm-contract.py` is asserted against the same response
shape used by vLLM.

The required installation order is preserved by the scripts: Gateway API
Inference Extension CRDs are installed before each gateway provider, followed
by the KServe controller and the `LLMInferenceService`.

Envoy AI Gateway and agentgateway consume KServe's generated secure EPP
directly. The OpenShift profile mounts a cert-manager-issued server Secret and
adds a provider-specific `BackendTLSPolicy` plus a ConfigMap copy of its CA so
Istio can verify the endpoint picker. This avoids treating KServe 0.20.0's
self-signed leaf certificate (`CA:FALSE`) as a trust anchor, which Envoy
correctly rejects.
