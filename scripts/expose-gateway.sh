#!/usr/bin/env bash
# Pin a provisioned Gateway Service's HTTP port to nodePort 30080 for kind.
set -euo pipefail
CLUSTER="${1:?usage: expose-gateway.sh <cluster> <namespace> [gateway-name]}"
NS="${2:?usage: expose-gateway.sh <cluster> <namespace> [gateway-name]}"
GW="${3:-}"
CTX="kind-${CLUSTER}"

SVC=""
for LBL in gateway.envoyproxy.io/owning-gateway-name gateway.networking.k8s.io/gateway-name gateway.networking.k8s.io/owning-gateway-name; do
  SEL="$LBL"
  [[ -n "$GW" ]] && SEL="$LBL=$GW"
  SVC=$(kubectl --context "$CTX" -n "$NS" get svc -l "$SEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [[ -n "$SVC" ]] && break
done

if [[ -z "$SVC" ]]; then
  SVC=$(kubectl --context "$CTX" -n "$NS" get svc -o json 2>/dev/null | python3 -c 'import json,sys; items=[s["metadata"]["name"] for s in json.load(sys.stdin)["items"] if s["spec"]["type"] in ("LoadBalancer","NodePort")]; print(items[0] if items else "")')
fi
[[ -n "$SVC" ]] || { echo "no gateway Service found in $CTX/$NS" >&2; exit 1; }

kubectl --context "$CTX" -n "$NS" patch svc "$SVC" --type=merge -p '{"spec":{"type":"NodePort"}}' >/dev/null
PATCH=$(kubectl --context "$CTX" -n "$NS" get svc "$SVC" -o json | python3 -c '
import json, sys
ports = json.load(sys.stdin)["spec"]["ports"]
target = next((i for i, p in enumerate(ports) if p.get("port") == 80), 0)
current = next((i for i, p in enumerate(ports) if p.get("nodePort") == 30080), None)
ops = []
if current is not None and current != target:
    ops.append({"op": "replace", "path": f"/spec/ports/{current}/nodePort",
                "value": ports[target]["nodePort"]})
if ports[target].get("nodePort") != 30080:
    ops.append({"op": "replace", "path": f"/spec/ports/{target}/nodePort",
                "value": 30080})
print(json.dumps(ops))')
[[ "$PATCH" == "[]" ]] || kubectl --context "$CTX" -n "$NS" patch svc "$SVC" --type=json -p "$PATCH" >/dev/null

kubectl --context "$CTX" -n "$NS" get svc "$SVC" -o jsonpath='{range .spec.ports[*]}{.name}{" port="}{.port}{" nodePort="}{.nodePort}{"\n"}{end}'
