#!/usr/bin/env bash
# Canonical manifest tree for one gateway context. This file changes paths
# only: every directory contains the same resources previously spread across
# clusters/, kserve/, llm-d/, keycloak/, semantic-router/, and provider roots.

select_context_components() {
  [[ $# -eq 1 && -n "$1" ]] || {
    echo "usage: select_context_components <kubectl-context>" >&2
    return 2
  }

  case "$1" in
    kind-ai-gw-kuadrant)
      COMPONENT_ROOT="$ROOT/kuadrant/deploy"
      ;;
    kind-ai-gw-envoy)
      COMPONENT_ROOT="$ROOT/envoy-ai-gateway/deploy"
      ;;
    kind-ai-gw-agent)
      COMPONENT_ROOT="$ROOT/agentgateway/deploy"
      ;;
    *)
      echo "unsupported kubectl context '$1'" >&2
      return 2
      ;;
  esac

  export COMPONENT_ROOT
}
