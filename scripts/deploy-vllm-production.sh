#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "usage: $0 <kubectl-context> [--delete]" >&2
  exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
context=$1
action=${2:-}
[[ -z "$action" || "$action" == "--delete" ]] || usage

kubectl --context "$context" cluster-info >/dev/null
if [[ "$action" == "--delete" ]]; then
  kubectl --context "$context" delete -k "$ROOT/kserve/production" --ignore-not-found
  exit
fi

gpu_nodes=$(kubectl --context "$context" get nodes \
  -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' | grep -Ec '^[1-9][0-9]*$' || true)
if [[ "$gpu_nodes" -eq 0 ]]; then
  echo "no node in $context advertises allocatable nvidia.com/gpu" >&2
  exit 1
fi

kubectl --context "$context" apply -f "$ROOT/kserve/production/vllm-config.yaml"
kubectl --context "$context" apply -f "$ROOT/kserve/production/routes.yaml"
kubectl --context "$context" apply -f "$ROOT/kserve/production/models.yaml"
kubectl --context "$context" -n ai-demo wait llminferenceservice \
  --for=condition=Ready --timeout=30m \
  vllm-chat vllm-embedding vllm-rerank vllm-transcription
