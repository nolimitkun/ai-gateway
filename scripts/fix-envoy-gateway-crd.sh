#!/usr/bin/env bash
# Envoy Gateway v1.8.1 publishes BackendTrafficPolicy.limit.requests as int32
# but gives it the uint32 maximum (4294967295). Kubernetes 1.36 rejects every
# BackendTrafficPolicy because that boundary cannot be represented by int32.
# Keep the upstream type and clamp only its invalid maximum until the vendored
# chart carries the upstream CRD correction.
set -euo pipefail

[[ $# -eq 1 ]] || { echo "usage: $0 kind-ai-gw-envoy" >&2; exit 2; }
envoy_crd_context="$1"
[[ "$envoy_crd_context" == kind-ai-gw-envoy ]] || { echo "unsupported context: $envoy_crd_context" >&2; exit 2; }
envoy_crd_name=backendtrafficpolicies.gateway.envoyproxy.io
envoy_crd_path=/spec/versions/0/schema/openAPIV3Schema/properties/spec/properties/rateLimit/properties/global/properties/rules/items/properties/limit/properties/requests/maximum
envoy_crd_current="$(kubectl --context "$envoy_crd_context" get crd "$envoy_crd_name" \
  -o 'jsonpath={.spec.versions[0].schema.openAPIV3Schema.properties.spec.properties.rateLimit.properties.global.properties.rules.items.properties.limit.properties.requests.maximum}')"

if [[ "$envoy_crd_current" == 4294967295 ]]; then
  kubectl --context "$envoy_crd_context" patch crd "$envoy_crd_name" --type=json \
    -p="[{\"op\":\"replace\",\"path\":\"$envoy_crd_path\",\"value\":2147483647}]"
  echo "BackendTrafficPolicy int32 maximum corrected in $envoy_crd_context"
else
  echo "BackendTrafficPolicy maximum already compatible in $envoy_crd_context ($envoy_crd_current)"
fi
