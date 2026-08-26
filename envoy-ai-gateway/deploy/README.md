# Envoy AI Gateway cluster deployment

This directory is the complete Kubernetes YAML inventory for
`ai-gw-envoy`. It does not import manifests from either gateway tree. Shared
components are intentionally repeated so this cluster can be inspected from
one place; `make validate` keeps those copies synchronized.

| Order | Component | Path | Applied by |
|---|---|---|---|
| 1 | Kind node and host port 8080 | `cluster/kind.yaml` | `make cluster CLUSTER=ai-gw-envoy` |
| 2 | Envoy AI Gateway entry point | `gateway/` | `make gateway` |
| 3 | KServe controller values, RBAC, and CPU `LLMInferenceService` path | `kserve/controller-values.yaml`, `kserve/base/` | installer and `make kserve` |
| 4 | Optional B300/H200/H100/L40S pools and placement | `kserve/pools/`, `kserve/gpu/` | `make pools` or a GPU overlay |
| 5 | BBR workload, typed TLS backend, and route-scoped ext_proc policies | `llm-d/` | `make kserve` |
| 6 | Keycloak workload and route | `keycloak/` | `make policies` |
| 7 | Redis, AI route, security, request, quota, and token policies | `policies/` | `make policies` |
| 8 | Optional semantic-router workload, config, and ordered ext_proc chain | `semantic-router/` | `make semantic-router` |
| 9 | Optional real vLLM model services | `kserve/production/` | production deployment workflow |

The base and pool manifests are byte-equivalent to the former shared KServe
resources; only their repository paths changed.
