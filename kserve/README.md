# KServe fixture

The same KServe 0.20.0 model, multi-task mock runtime, and scheduler
configuration is applied to the OpenShift-aligned Kuadrant, Envoy AI Gateway,
and agentgateway clusters.

## Files

- `manifests/llmisvc.yaml` defines two CPU runtime replicas and the scheduler.
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

The data path is:

```text
Gateway -> HTTPRoute -> InferencePool -> EPP -> selected model pod
```

The single `HTTPRoute` matches `/v1`, which integrates all mock capabilities
with every gateway without provider-specific paths:

| Endpoint | Compatibility shape |
|---|---|
| `/v1/models` | OpenAI model list and retrieve, with task and tier metadata |
| `/v1/chat/completions` | OpenAI chat and server-sent event streaming |
| `/v1/embeddings` | OpenAI embeddings response list |
| `/v1/rerank` | common query/documents reranking schema |
| `/v1/audio/transcriptions` | OpenAI multipart transcription upload, with segments and diarization |

One process answers for every model in the catalog. The tiered chat, STT, and
retrieval model identifiers are fixtures for gateway routing and allow-list
tests, not separate workloads; see the mock model catalog in the root
[README](../README.md).

## Why a mock runtime

This repository validates KServe ownership, readiness, scheduling, Gateway API
integration, pod selection, streaming, embeddings, reranking, and multipart
audio uploads on a CPU laptop. Storage initialization is disabled and
`mock-llm/server.py` is mounted into the KServe-owned pods.

That combined runtime is not a model-server recommendation. Production chat,
embedding, reranking, and transcription models normally have different
runtimes, scaling profiles, and accelerator requirements. Deploy task-specific
backends and routes; for LLM workloads, use a real model URI and KServe runtime
config, enable storage initialization, allocate GPU resources, and select
scheduler plugins that consume queue and KV-cache telemetry.

The required installation order is preserved by the scripts: Gateway API
Inference Extension CRDs are installed before each gateway provider, followed
by the KServe controller and the `LLMInferenceService`.

Envoy AI Gateway and agentgateway consume KServe's generated secure EPP
directly. The OpenShift profile mounts a cert-manager-issued server Secret and
adds a provider-specific `BackendTLSPolicy` plus a ConfigMap copy of its CA so
Istio can verify the endpoint picker. This avoids treating KServe 0.20.0's
self-signed leaf certificate (`CA:FALSE`) as a trust anchor, which Envoy
correctly rejects.
