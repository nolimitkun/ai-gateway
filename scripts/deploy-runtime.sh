#!/usr/bin/env bash
# Publish the CPU-only multi-task inference runtime source consumed by KServe.
set -euo pipefail
CLUSTER="${1:?usage: deploy-runtime.sh <kind-cluster-name>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/cluster-env.sh"
select_cluster "$CLUSTER"

kubectl --context "$CONTEXT" create namespace ai-demo --dry-run=client -o yaml | kubectl --context "$CONTEXT" apply -f -
kubectl --context "$CONTEXT" -n ai-demo create configmap mock-llm-src --from-file=server.py="$ROOT/mock-llm/server.py" --dry-run=client -o yaml | kubectl --context "$CONTEXT" apply -f -

# ConfigMap volumes update on disk, but the Python processes do not reload
# source. Restart every deployment that mounts this runtime, including the four
# accelerator fixtures, so a retained cluster cannot mix old and new contracts.
deployments=()
while IFS= read -r deployment; do
  [[ -z "$deployment" ]] || deployments+=("$deployment")
done < <(
  kubectl --context "$CONTEXT" -n ai-demo get deployments -o json |
    python3 -c '
import json, sys
for item in json.load(sys.stdin).get("items", []):
    volumes = item.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", [])
    if any(volume.get("configMap", {}).get("name") == "mock-llm-src" for volume in volumes):
        print(item["metadata"]["name"])
'
)
for deployment in "${deployments[@]}"; do
  kubectl --context "$CONTEXT" -n ai-demo rollout restart "deployment/$deployment"
done
for deployment in "${deployments[@]}"; do
  kubectl --context "$CONTEXT" -n ai-demo rollout status \
    "deployment/$deployment" --timeout=10m
done

echo "KServe runtime source ready in $CONTEXT"
