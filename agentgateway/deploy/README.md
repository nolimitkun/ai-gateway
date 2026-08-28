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

`native-routing/` is additive and never replaces `llm-d/`. `AgentgatewayModel`
matches the model in the request itself, so the twelve mappings need no routing
header at all, and the tier ceiling sits on the model resource rather than on a
header a client could forge. Measured on its own listener it is a complete
replacement for BBR -- twelve of twelve models, `auto`, the catalog, speech,
the tier ceiling and rate limiting all follow a caller who switches hostname.
What does not work is making it the default path: the models 404 on the `http`
listener however they are attached, and `traffic.transformation` cannot read
the request body to write the routing header itself. See
`docs/open-questions.md`.
