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
| 9 | Optional native body-model routing on `native.local`, beside the BBR path | `native-routing/` | `make native-routing` |
| 10 | Optional real vLLM model services | `kserve/production/` | production deployment workflow |

The base and pool manifests are byte-equivalent to the former shared KServe
resources; only their repository paths changed.

`native-routing/` is additive and never replaces `llm-d/`. On paper
`AgentgatewayModel` matches the model in the request itself, so the twelve
mappings would need no routing header and the tier ceiling could sit on the
model resource rather than on a header a client could forge.

Measured, none of it takes effect. With the body-based router stopped the
comparison reads 5/12, and the five are the semantic router writing
`x-gateway-model-name`, not the models: deleting all twelve changes nothing,
suppressing that transformation drops every model to the shared fixture with
the models still installed, the resources carry no `status`, and the data plane
never attributes a route to one. Whether that is an error in these manifests or
a limitation of agentgateway v1.4.1 is not established -- they ship because
they are the reproduction. See `docs/open-questions.md`.
