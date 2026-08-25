#!/usr/bin/env python3
"""Merge three independently captured gateway result files into README.md."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLUSTERS = ("ai-gw-kuadrant", "ai-gw-envoy", "ai-gw-agent")
ROWS = (
    ("Last isolated comparison (UTC)", "run_label"),
    ("Gateway Programmed", "gateway_programmed"),
    ("`LLMInferenceService` Ready", "llmisvc_ready"),
    ("Route Accepted / ResolvedRefs", "route_ready"),
    ("Workload replicas", "workload_replicas"),
    ("KServe-owned Deployments, Services, and Pool", "owned_resources"),
    ("Latest routing sample", "routing_sample"),
    ("Streaming usage chunk", "streaming_usage"),
    ("Model catalog (`GET /v1/models`)", "model_catalog"),
    ("Tiered chat models", "tiered_chat"),
    ("Embeddings API", "embeddings"),
    ("RAG embedding models", "rag_embeddings"),
    ("Reranking API", "reranking"),
    ("Speech-to-text API", "stt"),
    ("Speaker diarization", "diarization"),
    ("Local chat p50", "chat_p50"),
    ("Policy objects reporting ready", "policy_ready"),
    ("Semantic router ext_proc attachment", "router_attachment"),
    ("Auto model selection: reasoning / code / chat", "auto_models"),
    ("Model and prompt the runtime received", "auto_upstream"),
    ("Decision headers returned to the client", "auto_decision"),
    ("Auto-routed chat p50", "auto_p50"),
    ("Keycloak token issuance", "token_issuance"),
    ("Authentication: anonymous / forged / valid", "authentication"),
    ("Authorization: guest / non-admin B300 / admin B300", "authorization"),
    ("Request rate limit (5 per minute)", "request_limit"),
    ("Quota limit (3 per window)", "quota_limit"),
    ("Token limit (100 tokens per minute)", "token_limit"),
    ("CORS preflight answered", "cors"),
)


def load_results() -> list[dict]:
    loaded = []
    missing = []
    for cluster in CLUSTERS:
        path = ROOT / "compare" / "results" / f"{cluster}.json"
        if not path.exists():
            missing.append(str(path.relative_to(ROOT)))
            continue
        data = json.loads(path.read_text())
        data["run_label"] = f"{data['timestamp']} ({data['requests']} requests)"
        loaded.append(data)
    if missing:
        raise SystemExit("missing per-cluster results; run make compare for each cluster:\n  " + "\n  ".join(missing))
    return loaded


def main() -> None:
    readme = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "README.md"
    results = load_results()
    lines = [
        "| Check | OpenShift profile (Kuadrant) | Envoy AI Gateway | agentgateway |",
        "|---|---|---|---|",
    ]
    for label, key in ROWS:
        values = [str(result.get(key, "Not recorded")).replace("|", "\\|") for result in results]
        lines.append(f"| {label} | " + " | ".join(values) + " |")
    table = "\n".join(lines)
    text = readme.read_text()
    start_marker = "<!-- comparison-results:start -->"
    end_marker = "<!-- comparison-results:end -->"
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker, start)
    readme.write_text(text[:start] + "\n\n" + table + "\n\n" + text[end:])
    print(f"updated {readme}")


if __name__ == "__main__":
    main()
