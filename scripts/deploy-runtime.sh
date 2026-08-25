#!/usr/bin/env bash
# Publish the CPU-only multi-task inference runtime source consumed by KServe.
set -euo pipefail
CLUSTER="${1:?usage: deploy-runtime.sh <kind-cluster-name>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/cluster-env.sh"
select_cluster "$CLUSTER"

kubectl --context "$CONTEXT" create namespace ai-demo --dry-run=client -o yaml | kubectl --context "$CONTEXT" apply -f -
kubectl --context "$CONTEXT" -n ai-demo create configmap mock-llm-src --from-file=server.py="$ROOT/mock-llm/server.py" --dry-run=client -o yaml | kubectl --context "$CONTEXT" apply -f -

if kubectl --context "$CONTEXT" -n ai-demo get deployment kserve-mock-kserve >/dev/null 2>&1; then
  kubectl --context "$CONTEXT" -n ai-demo rollout restart deployment/kserve-mock-kserve
  kubectl --context "$CONTEXT" -n ai-demo rollout status \
    deployment/kserve-mock-kserve --timeout=10m
fi

echo "KServe runtime source ready in $CONTEXT"
