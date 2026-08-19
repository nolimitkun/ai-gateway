#!/usr/bin/env bash
# Pin a provisioned gateway Service to nodePort 30080 so kind's extraPortMapping
# reaches it. kind has no LoadBalancer controller, so the Service otherwise sits
# at <pending> forever and the Gateway never reports Programmed=True.
#
# The two projects label the Service they provision differently
# (gateway.envoyproxy.io/... vs gateway.networking.k8s.io/...), so try both.
#
# Usage: scripts/expose-gateway.sh <kind-cluster-name> <namespace> [gateway-name]
set -euo pipefail
CLUSTER="${1:?usage: expose-gateway.sh <cluster> <namespace> [gateway-name]}"
NS="${2:?usage: expose-gateway.sh <cluster> <namespace> [gateway-name]}"
GW="${3:-}"
CTX="kind-${CLUSTER}"

SVC=""
for LBL in gateway.envoyproxy.io/owning-gateway-name \
           gateway.networking.k8s.io/gateway-name \
           gateway.networking.k8s.io/owning-gateway-name; do
  SEL="$LBL"; [[ -n "$GW" ]] && SEL="$LBL=$GW"
  SVC=$(kubectl --context "$CTX" -n "$NS" get svc -l "$SEL" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [[ -n "$SVC" ]] && break
done
# Last resort: any LoadBalancer Service in the namespace.
if [[ -z "$SVC" ]]; then
  SVC=$(kubectl --context "$CTX" -n "$NS" get svc -o json 2>/dev/null \
    | python3 -c 'import json,sys;i=[s["metadata"]["name"] for s in json.load(sys.stdin)["items"] if s["spec"]["type"]=="LoadBalancer"];print(i[0] if i else "")')
fi
[[ -n "$SVC" ]] || { echo "no gateway Service found in $CTX/$NS" >&2; exit 1; }

kubectl --context "$CTX" -n "$NS" patch svc "$SVC" --type=json -p \
  '[{"op":"replace","path":"/spec/type","value":"NodePort"},
    {"op":"replace","path":"/spec/ports/0/nodePort","value":30080}]' >/dev/null

kubectl --context "$CTX" -n "$NS" get svc "$SVC" --no-headers \
  -o custom-columns=NAME:.metadata.name,TYPE:.spec.type,PORT:.spec.ports[0].port,NODEPORT:.spec.ports[0].nodePort
