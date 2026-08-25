#!/usr/bin/env bash
# Canonical mapping for one comparison cluster. Source this file, then call
# select_cluster <kind-cluster-name>. It deliberately accepts cluster names,
# not kubectl contexts, so every Make target has one consistent public input.

select_cluster() {
  [[ $# -eq 1 && -n "$1" ]] || {
    echo "usage: select_cluster ai-gw-kuadrant|ai-gw-envoy|ai-gw-agent" >&2
    return 2
  }

  CLUSTER="$1"
  CONTEXT="kind-$CLUSTER"
  case "$CLUSTER" in
    ai-gw-kuadrant)
      STACK=kuadrant
      KIND_CONFIG=clusters/kind-kuadrant.yaml
      GATEWAY_NAMESPACE=openshift-ingress
      GATEWAY_NAME=openshift-ai-inference
      BASE_URL=http://localhost:8082
      HOST_PORT=8082
      ;;
    ai-gw-envoy)
      STACK=envoy
      KIND_CONFIG=clusters/kind-envoy-ai-gateway.yaml
      GATEWAY_NAMESPACE=ai-demo
      GATEWAY_NAME=ai-gateway
      BASE_URL=http://localhost:8080
      HOST_PORT=8080
      ;;
    ai-gw-agent)
      STACK=agent
      KIND_CONFIG=clusters/kind-agentgateway.yaml
      GATEWAY_NAMESPACE=ai-demo
      GATEWAY_NAME=ai-gateway
      BASE_URL=http://localhost:8081
      HOST_PORT=8081
      ;;
    *)
      echo "unsupported CLUSTER '$CLUSTER'; expected ai-gw-kuadrant, ai-gw-envoy, or ai-gw-agent" >&2
      return 2
      ;;
  esac
  export CLUSTER CONTEXT STACK KIND_CONFIG GATEWAY_NAMESPACE GATEWAY_NAME BASE_URL HOST_PORT
}
