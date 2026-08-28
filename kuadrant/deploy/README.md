# Kuadrant cluster deployment

This directory is the complete Kubernetes YAML inventory for
`ai-gw-kuadrant`. It does not import manifests from either gateway tree.
Shared components are intentionally repeated so this cluster can be inspected
from one place; `make validate` keeps those copies synchronized.

| Order | Component | Path | Applied by |
|---|---|---|---|
| 1 | Kind node and host port 8082 | `cluster/kind.yaml` | `make cluster CLUSTER=ai-gw-kuadrant` |
| 2 | Kuadrant instance, shared OpenShift-style Gateway, and base policy | `gateway/` | gateway installer and `make gateway` |
| 3 | KServe controller values and CPU `LLMInferenceService` path | `kserve/controller-values.yaml`, `kserve/base/` | `make kserve` |
| 4 | Optional B300/H200/H100/L40S pools and placement | `kserve/pools/`, `kserve/gpu/` | `make pools` or a GPU overlay |
| 5 | BBR workload and Istio ext_proc/TLS attachment | `llm-d/` | `make kserve` |
| 6 | Keycloak workload and OpenShift-style route attachment | `keycloak/` | `make policies` |
| 7 | Kuadrant auth, rate, quota, and token policies | `policies/` | `make policies` |
| 8 | Optional semantic-router workload, config, and Istio attachment | `semantic-router/` | `make semantic-router` |
| 9 | Optional real vLLM model services | `kserve/production/` | production deployment workflow |

The KServe Kustomizations include the same OpenShift-aligned cross-namespace
Gateway references, cert-manager certificates, and `BackendTLSPolicy` resources
used before this directory reorganization.

There is no `native-routing/` here, and that absence is the measurement. Istio
has no body-aware routing API -- `VirtualService`, `EnvoyFilter` and
`WasmPlugin` carry nothing that reads `body.model` -- which is why this profile
drives BBR from a raw `EnvoyFilter` with one patch per model rule while the
other two stacks have a first-class API for it. The comparison reports the
capability difference rather than a measurement of one.
