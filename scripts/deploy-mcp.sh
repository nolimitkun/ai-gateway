#!/usr/bin/env bash
# Deploy the two mock MCP servers into the given kind cluster.
# Usage: scripts/deploy-mcp.sh <kind-cluster-name>
set -euo pipefail
CLUSTER="${1:?usage: deploy-mcp.sh <kind-cluster-name>}"
CTX="kind-${CLUSTER}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

kubectl --context "$CTX" apply -f "$ROOT/mcp-server/manifest.yaml"
kubectl --context "$CTX" -n ai-demo create configmap mcp-src \
  --from-file=server.py="$ROOT/mcp-server/server.py" \
  --dry-run=client -o yaml | kubectl --context "$CTX" apply -f -
kubectl --context "$CTX" -n ai-demo rollout restart deploy/mcp-clock deploy/mcp-math
kubectl --context "$CTX" -n ai-demo rollout status deploy/mcp-clock --timeout=180s
kubectl --context "$CTX" -n ai-demo rollout status deploy/mcp-math  --timeout=180s
echo "mcp servers ready in $CTX"
