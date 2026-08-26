#!/usr/bin/env bash
# Install the shared Keycloak realm used by every gateway's auth policy.
#
# One Keycloak per cluster: the selected gateway validates tokens against an
# issuer it can resolve in-cluster.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ctx="${1:?usage: install-keycloak.sh <kubectl-context>}"
case "$ctx" in kind-ai-gw-kuadrant|kind-ai-gw-envoy|kind-ai-gw-agent) ;; *) echo "unsupported context: $ctx" >&2; exit 2 ;; esac

echo "==> Keycloak in $ctx"
keycloak_existed=false
realm_changed=true
if kubectl --context "$ctx" -n ai-demo get deployment/keycloak >/dev/null 2>&1; then
  keycloak_existed=true
fi
if kubectl --context "$ctx" -n ai-demo get configmap/keycloak-realm -o json 2>/dev/null |
  python3 -c '
import json, pathlib, sys
current = json.load(sys.stdin).get("data", {}).get("ai-gateway-realm.json", "")
expected = pathlib.Path(sys.argv[1]).read_text()
raise SystemExit(0 if current == expected else 1)
' "$ROOT/keycloak/realm/ai-gateway-realm.json"; then
  realm_changed=false
fi
kubectl --context "$ctx" create namespace ai-demo \
  --dry-run=client -o yaml | kubectl --context "$ctx" apply -f -
kubectl --context "$ctx" -n ai-demo create configmap keycloak-realm \
  --from-file=ai-gateway-realm.json="$ROOT/keycloak/realm/ai-gateway-realm.json" \
  --dry-run=client -o yaml | kubectl --context "$ctx" apply -f -
if [[ "$ctx" == "kind-ai-gw-kuadrant" ]]; then
  kubectl --context "$ctx" apply -k "$ROOT/overlays/keycloak-openshift"
else
  kubectl --context "$ctx" apply -k "$ROOT/keycloak"
fi
# Realm import only runs when Keycloak starts. The development deployment
# intentionally has no persistent database, so restart an existing pod to
# make changes to the declarative realm file take effect. A newly created
# Deployment already starts with the current ConfigMap and needs no second
# ReplicaSet.
if $keycloak_existed && $realm_changed; then
  kubectl --context "$ctx" -n ai-demo rollout restart deployment/keycloak
fi
kubectl --context "$ctx" -n ai-demo rollout status deployment/keycloak --timeout=5m

echo "OK: Keycloak realm 'ai-gateway' ready in $ctx"
