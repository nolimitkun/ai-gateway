# KServe fixture

The same KServe 0.20.0 `LLMInferenceService` is applied to the Kuadrant, Envoy
AI Gateway, and agentgateway clusters.

## Files

- `manifests/llmisvc.yaml` defines two CPU model replicas and the scheduler.
- `manifests/route.yaml` is the only inference route in the repository.
- `manifests/cpu-presets.yaml` prevents KServe's GPU/vLLM presets from being
  merged into this complete laptop fixture.
- `manifests/envoy-inferencepool-rbac.yaml` lets both Envoy-backed stacks
  watch `InferencePool`; agentgateway installs equivalent RBAC.
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

## Why a mock runtime

This repository validates KServe ownership, readiness, scheduling, Gateway API
integration, pod selection, and streaming on a CPU laptop. Storage
initialization is disabled and `mock-llm/server.py` is mounted into the
KServe-owned pods.

That runtime is not a model-server recommendation. For production, use a real
model URI and KServe runtime config, enable storage initialization, allocate
GPU resources, and select scheduler plugins that consume the model server's
queue and KV-cache telemetry.

The required installation order is preserved by the scripts: Gateway API
Inference Extension CRDs are installed before each gateway provider, followed
by the KServe controller and the `LLMInferenceService`.

The custom scheduler mounts KServe's generated TLS Secret. Both Envoy AI
Gateway stacks and agentgateway consume that secure EPP directly.
