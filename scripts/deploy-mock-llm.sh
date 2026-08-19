#!/usr/bin/env bash
# Deploy the shared mock LLM backends into the given kind cluster.
# Usage: scripts/deploy-mock-llm.sh <kind-cluster-name>
set -euo pipefail
CLUSTER="${1:?usage: deploy-mock-llm.sh <kind-cluster-name>}"
CTX="kind-${CLUSTER}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

kubectl --context "$CTX" apply -f "$ROOT/mock-llm/manifest.yaml"
# server.py is the source of truth; regenerate the ConfigMap from it every time.
kubectl --context "$CTX" -n ai-demo create configmap mock-llm-src \
  --from-file=server.py="$ROOT/mock-llm/server.py" \
  --dry-run=client -o yaml | kubectl --context "$CTX" apply -f -
# Pick up a changed ConfigMap.
kubectl --context "$CTX" -n ai-demo rollout restart deploy/mock-llm-alpha deploy/mock-llm-beta
kubectl --context "$CTX" -n ai-demo rollout status deploy/mock-llm-alpha --timeout=180s
kubectl --context "$CTX" -n ai-demo rollout status deploy/mock-llm-beta  --timeout=180s
echo "mock-llm ready in $CTX"
