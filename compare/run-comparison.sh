#!/usr/bin/env bash
# Compatibility entry point for the cluster-by-cluster comparison runner.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/compare/run-gateway.py" "$@"
