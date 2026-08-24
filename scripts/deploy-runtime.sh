#!/usr/bin/env bash
# Publish the CPU-only OpenAI-compatible runtime source consumed by KServe.
set -euo pipefail
CLUSTER="${1:?usage: deploy-runtime.sh <kind-cluster-name>}"
CTX="kind-${CLUSTER}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

kubectl --context "$CTX" create namespace ai-demo --dry-run=client -o yaml | kubectl --context "$CTX" apply -f -
kubectl --context "$CTX" -n ai-demo create configmap mock-llm-src --from-file=server.py="$ROOT/mock-llm/server.py" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f -

echo "KServe runtime source ready in $CTX"
