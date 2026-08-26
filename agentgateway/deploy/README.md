# agentgateway cluster deployment

This directory is the complete Kubernetes YAML inventory for
`ai-gw-agent`. It does not import manifests from either gateway tree. Shared
components are intentionally repeated so this cluster can be inspected from
one place; `make validate` keeps those copies synchronized.

| Order | Component | Path | Applied by |
|---|---|---|---|
| 1 | Kind node and host port 8081 | `cluster/kind.yaml` | `make cluster CLUSTER=ai-gw-agent` |
| 2 | agentgateway entry point | `gateway/` | `make gateway` |
| 3 | KServe controller values and CPU `LLMInferenceService` path | `kserve/controller-values.yaml`, `kserve/base/` | `make kserve` |
| 4 | Optional B300/H200/H100/L40S pools and placement | `kserve/pools/`, `kserve/gpu/` | `make pools` or a GPU overlay |
| 5 | BBR workload and native PreRouting TLS ext_proc policy | `llm-d/` | `make kserve` |
| 6 | Keycloak workload and route | `keycloak/` | `make policies` |
| 7 | JWT, authorization, local limit, quota, and CORS policies | `policies/` | `make policies` |
| 8 | Optional semantic-router workload, config, and route attachment | `semantic-router/` | `make semantic-router` |
| 9 | Optional real vLLM model services | `kserve/production/` | production deployment workflow |

The base and pool manifests are byte-equivalent to the former shared KServe
resources; only their repository paths changed.
