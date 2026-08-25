#!/usr/bin/env python3
"""Probe one running gateway cluster and persist one comparison column."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "compare" / "results"
CONFIG = {
    "ai-gw-kuadrant": {
        "context": "kind-ai-gw-kuadrant", "base": "http://localhost:8082",
        "gateway": "openshift-ai-inference", "gateway_ns": "openshift-ingress",
        "policy_condition": "Enforced",
        "policies": ["authpolicy/kserve-mock", "ratelimitpolicy/kserve-mock", "tokenratelimitpolicy/kserve-mock"],
        "router": ("openshift-ingress", "envoyfilter/semantic-router", None),
    },
    "ai-gw-envoy": {
        "context": "kind-ai-gw-envoy", "base": "http://localhost:8080",
        "gateway": "ai-gateway", "gateway_ns": "ai-demo",
        "policy_condition": "Accepted",
        "policies": ["securitypolicy/kserve-mock", "backendtrafficpolicy/kserve-mock", "aigatewayroute/kserve-mock-ai"],
        "router": ("ai-demo", "envoyextensionpolicy/semantic-router", "Accepted"),
    },
    "ai-gw-agent": {
        "context": "kind-ai-gw-agent", "base": "http://localhost:8081",
        "gateway": "ai-gateway", "gateway_ns": "ai-demo",
        "policy_condition": "Accepted",
        "policies": ["agentgatewaypolicy/kserve-mock-jwt", "agentgatewaypolicy/kserve-mock-members", "agentgatewaypolicy/kserve-mock-big-tier", "agentgatewaypolicy/kserve-mock-rate-limit", "agentgatewaypolicy/kserve-mock-cors"],
        "router": ("ai-demo", "agentgatewaypolicy/kserve-mock-semantic-router", "Accepted"),
    },
}

CHAT = {"model": "mock-kserve", "messages": [{"role": "user", "content": "hello through KServe"}]}
BIG_CHAT = {"model": "kimi-k3", "messages": [{"role": "user", "content": "hello through KServe"}]}
AUTO_PROMPTS = [
    "prove that the square root of two is irrational",
    "refactor this python function",
    "hello there, good morning",
]


def kubectl(cfg: dict, *args: str, allow_error: bool = False) -> str:
    proc = subprocess.run(
        ["kubectl", "--context", cfg["context"], *args], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL if allow_error else None,
    )
    if proc.returncode and not allow_error:
        raise RuntimeError(f"kubectl {' '.join(args)} failed")
    return proc.stdout if proc.returncode == 0 else ""


def kube_json(cfg: dict, namespace: str, object_name: str) -> dict:
    raw = kubectl(cfg, "-n", namespace, "get", object_name, "-o", "json", allow_error=True)
    try:
        return json.loads(raw)
    except Exception:
        return {}


def conditions(obj: dict) -> list[dict]:
    status = obj.get("status", {})
    found = list(status.get("conditions", []))
    for key in ("parents", "ancestors"):
        for parent in status.get(key, []):
            found.extend(parent.get("conditions", []))
    return found


def has_condition(obj: dict, wanted: str) -> bool:
    return any(c.get("type") == wanted and c.get("status") == "True" for c in conditions(obj))


def request(url: str, *, method: str = "GET", headers: dict | None = None,
            json_body: dict | None = None, form: dict | None = None,
            multipart: dict | None = None, timeout: int = 25) -> tuple[int, bytes, dict]:
    hdrs = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs.setdefault("content-type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs.setdefault("content-type", "application/x-www-form-urlencoded")
    elif multipart is not None:
        boundary = "----ai-gateway-" + uuid.uuid4().hex
        chunks: list[bytes] = []
        for name, value in multipart.items():
            chunks.append(f"--{boundary}\r\n".encode())
            if isinstance(value, tuple):
                filename, content, mime = value
                chunks.append(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
                chunks.append(f"Content-Type: {mime}\r\n\r\n".encode())
                chunks.append(content)
            else:
                chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}'.encode())
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        data = b"".join(chunks)
        hdrs["content-type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers.items())
    except Exception:
        return 0, b"", {}


def body_json(raw: bytes) -> dict:
    try:
        return json.loads(raw)
    except Exception:
        return {}


def token(cfg: dict, user: str = "alice") -> str:
    status, raw, _ = request(
        cfg["base"] + "/realms/ai-gateway/protocol/openid-connect/token",
        method="POST", form={"grant_type": "password", "client_id": "ai-gateway-cli", "username": user, "password": user},
    )
    return body_json(raw).get("access_token", "") if status == 200 else ""


def auth_headers(access_token: str) -> dict:
    return {"authorization": f"Bearer {access_token}"} if access_token else {}


def json_call(cfg: dict, path: str, access_token: str, payload: dict | None = None,
              extra_headers: dict | None = None) -> tuple[int, dict, dict]:
    headers = auth_headers(access_token)
    headers.update(extra_headers or {})
    status, raw, response_headers = request(
        cfg["base"] + path, method="POST" if payload is not None else "GET",
        headers=headers, json_body=payload,
    )
    return status, body_json(raw), {k.lower(): v for k, v in response_headers.items()}


def probe_limit(cfg: dict, access_token: str, header: tuple[str, str], count: int,
                payload: dict = CHAT, host: str | None = None) -> str:
    for index in range(1, count + 1):
        extra = {header[0]: header[1]}
        if host:
            extra["host"] = host
        code, _, _ = json_call(cfg, "/v1/chat/completions", access_token, payload, extra)
        if code == 429:
            return f"429 on request {index} of {count}"
    return f"no 429 in {count}"


def policy_summary(cfg: dict) -> tuple[bool, str]:
    ready = 0
    present = False
    for index, object_name in enumerate(cfg["policies"]):
        obj = kube_json(cfg, "ai-demo", object_name)
        if index == 0:
            present = bool(obj)
        ready += int(has_condition(obj, cfg["policy_condition"]))
    return present, f"{ready}/{len(cfg['policies'])}"


def router_summary(cfg: dict) -> tuple[bool, str]:
    namespace, object_name, condition = cfg["router"]
    obj = kube_json(cfg, namespace, object_name)
    if not obj:
        return False, "Absent"
    if condition is None:
        return True, "Present, no status"
    return True, condition if has_condition(obj, condition) else f"Present, not {condition}"


def wait_for_gateway(cfg: dict, timeout: int = 420) -> None:
    """Wait through a retained Kind node's control-plane and proxy restart."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _, _ = request(cfg["base"] + "/v1/models", timeout=5)
        # An installed auth policy proves reachability with 401/403; an open
        # comparison path proves it with 200.
        if status in (200, 401, 403):
            return
        time.sleep(3)
    raise RuntimeError(f"gateway at {cfg['base']} did not become reachable within {timeout}s")


def wait_for_token(cfg: dict, timeout: int = 420) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        access_token = token(cfg)
        if access_token:
            return access_token
        time.sleep(3)
    raise RuntimeError(f"Keycloak behind {cfg['base']} did not issue a token within {timeout}s")


def wait_for_models(cfg: dict, access_token: str, timeout: int = 420) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body, _ = json_call(cfg, "/v1/models", access_token)
        if status == 200 and body.get("data"):
            return
        time.sleep(3)
    raise RuntimeError(f"KServe runtime behind {cfg['base']} did not become ready within {timeout}s")


def wait_for_router(cfg: dict, access_token: str, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    payload = {"model": "auto", "messages": [{"role": "user", "content": AUTO_PROMPTS[-1]}]}
    while time.monotonic() < deadline:
        status, body, _ = json_call(cfg, "/v1/chat/completions", access_token, payload)
        if status == 200 and body.get("model") not in (None, "auto"):
            return
        time.sleep(3)
    raise RuntimeError(f"semantic router behind {cfg['base']} did not rewrite model=auto within {timeout}s")


def run(cluster: str, sample_count: int) -> dict:
    cfg = CONFIG[cluster]
    wait_for_gateway(cfg)
    result: dict[str, str | int] = {"cluster": cluster, "timestamp": time.strftime("%Y-%m-%d %H:%M", time.gmtime()), "requests": sample_count}
    policies_present, policy_ready = policy_summary(cfg)
    router_present, router_ready = router_summary(cfg)
    access_token = wait_for_token(cfg) if policies_present else token(cfg)
    wait_for_models(cfg, access_token)
    if router_present:
        wait_for_router(cfg, access_token)

    gateway = kube_json(cfg, cfg["gateway_ns"], f"gateway/{cfg['gateway']}")
    route = kube_json(cfg, "ai-demo", "httproute/kserve-mock")
    llmisvc = kube_json(cfg, "ai-demo", "llminferenceservice/kserve-mock")
    result["gateway_programmed"] = "Yes" if has_condition(gateway, "Programmed") else "No"
    result["llmisvc_ready"] = "Yes" if has_condition(llmisvc, "Ready") else "No"
    result["route_ready"] = f"{'Yes' if has_condition(route, 'Accepted') else 'No'} / {'Yes' if has_condition(route, 'ResolvedRefs') else 'No'}"

    deployment = kube_json(cfg, "ai-demo", "deployment/kserve-mock-kserve")
    result["workload_replicas"] = f"{deployment.get('status', {}).get('readyReplicas', 0)}/{deployment.get('spec', {}).get('replicas', 0)}"
    owned = json.loads(kubectl(cfg, "-n", "ai-demo", "get", "deployment,service,inferencepool", "-o", "json"))
    result["owned_resources"] = str(sum(
        any(owner.get("kind") == "LLMInferenceService" and owner.get("name") == "kserve-mock" for owner in item.get("metadata", {}).get("ownerReferences", []))
        for item in owned.get("items", [])
    ))

    latencies: list[float] = []
    pods: set[str] = set()
    ok = 0
    for _ in range(sample_count):
        start = time.monotonic()
        code, body, _ = json_call(cfg, "/v1/chat/completions", access_token, CHAT)
        latencies.append((time.monotonic() - start) * 1000)
        ok += int(code == 200)
        pod = body.get("mock_pod")
        if not pod:
            match = re.search(r"Hello from ([^\s(]+)", str(body))
            pod = match.group(1) if match else None
        if pod:
            pods.add(pod)
    result["routing_sample"] = f"{sample_count}/{sample_count} HTTP 200, {len(pods)} pods" if ok == sample_count else f"{ok}/{sample_count} HTTP 200, {len(pods)} pods"
    result["chat_p50"] = f"{round(statistics.median(latencies))} ms" if latencies else "n/a"

    stream_payload = dict(CHAT, stream=True, stream_options={"include_usage": True})
    code, raw, _ = request(cfg["base"] + "/v1/chat/completions", method="POST", headers=auth_headers(access_token), json_body=stream_payload)
    result["streaming_usage"] = "Yes" if code == 200 and b'"usage"' in raw else "No"

    code, models, _ = json_call(cfg, "/v1/models", access_token)
    result["model_catalog"] = f"{len(models.get('data', []))} models" if code == 200 else "No"
    tier_ok = []
    for model, tier in (("kimi-k3", "big"), ("deepseek-v4-flash", "medium"), ("qwen3.8-27b", "small")):
        code, body, _ = json_call(cfg, "/v1/chat/completions", access_token, {"model": model, "messages": CHAT["messages"]})
        tier_ok.append(code == 200 and body.get("model") == model and body.get("mock_tier") == tier)
    result["tiered_chat"] = "big/medium/small" if all(tier_ok) else "No"

    code, embeddings, _ = json_call(cfg, "/v1/embeddings", access_token, {"model": "mock-embedding", "input": ["one", "two"]})
    result["embeddings"] = "Yes" if code == 200 and len(embeddings.get("data", [])) == 2 else "No"
    rag_ok = True
    for model, dimensions in (("qwen3-embedding-8b", 4096), ("bge-m3", 1024)):
        code, body, _ = json_call(cfg, "/v1/embeddings", access_token, {"model": model, "input": ["RAG"]})
        data = body.get("data", [])
        rag_ok &= code == 200 and bool(data) and len(data[0].get("embedding", [])) == dimensions
    result["rag_embeddings"] = "Yes" if rag_ok else "No"
    code, rerank, _ = json_call(cfg, "/v1/rerank", access_token, {"model": "mock-reranker", "query": "gateway inference", "documents": ["other", "gateway inference"], "top_n": 1})
    rerank_results = rerank.get("results", [])
    result["reranking"] = "Yes" if code == 200 and rerank_results and rerank_results[0].get("index") == 1 else "No"

    headers = auth_headers(access_token)
    code, raw, _ = request(cfg["base"] + "/v1/audio/transcriptions", method="POST", headers=headers, multipart={"file": ("sample.wav", b"", "audio/wav"), "model": "mock-whisper"})
    stt = body_json(raw)
    result["stt"] = "Yes" if code == 200 and stt.get("model") == "mock-whisper" else "No"
    code, raw, _ = request(cfg["base"] + "/v1/audio/transcriptions", method="POST", headers=headers, multipart={"file": ("meeting.wav", b"", "audio/wav"), "model": "whisper-large-v3", "diarization": "true", "num_speakers": "3"})
    diarization = body_json(raw)
    speakers = {segment.get("speaker") for segment in diarization.get("segments", [])}
    result["diarization"] = f"{len(speakers)} speakers" if code == 200 and diarization.get("diarization") and speakers else "No"

    result["policy_ready"] = policy_ready
    result["router_attachment"] = router_ready
    if router_present:
        selected = []
        upstream = []
        decisions = []
        system_prompts = 0
        auto_latencies = []
        for prompt in AUTO_PROMPTS:
            start = time.monotonic()
            code, body, response_headers = json_call(cfg, "/v1/chat/completions", access_token, {"model": "auto", "messages": [{"role": "user", "content": prompt}]})
            auto_latencies.append((time.monotonic() - start) * 1000)
            selected.append(body.get("model", "error") if code == 200 else f"HTTP {code}")
            routing_headers = body.get("mock_routing_headers", {})
            upstream.append(routing_headers.get("x-selected-model", "no header"))
            system_prompts += int(bool(body.get("mock_system_prompt")))
            decisions.append(
                response_headers.get("x-vsr-selected-decision")
                or response_headers.get("x-vsr-selected-model")
                or "No header"
            )
        result["auto_models"] = " / ".join(selected)
        result["auto_upstream"] = f"{' / '.join(upstream)}; system prompt {system_prompts}/{len(AUTO_PROMPTS)}"
        result["auto_decision"] = " / ".join(decisions)
        result["auto_p50"] = f"{round(statistics.median(auto_latencies))} ms"
    else:
        for key in ("auto_models", "auto_upstream", "auto_decision", "auto_p50"):
            result[key] = "No router"

    result["token_issuance"] = "Yes" if access_token else "No"
    if policies_present and access_token:
        anonymous, _, _ = json_call(cfg, "/v1/chat/completions", "", CHAT)
        forged, _, _ = json_call(cfg, "/v1/chat/completions", "not.a.real.token", CHAT)
        valid, _, _ = json_call(cfg, "/v1/chat/completions", access_token, CHAT)
        result["authentication"] = f"{anonymous} / {forged} / {valid}"
        mallory, bob = token(cfg, "mallory"), token(cfg, "bob")
        guest, _, _ = json_call(cfg, "/v1/chat/completions", mallory, CHAT)
        restricted, _, _ = json_call(cfg, "/v1/chat/completions", bob, BIG_CHAT, {"x-model-class": "b300"})
        allowed, _, _ = json_call(cfg, "/v1/chat/completions", access_token, BIG_CHAT, {"x-model-class": "b300"})
        result["authorization"] = f"{guest} / {restricted} / {allowed}"
        result["request_limit"] = probe_limit(cfg, access_token, ("x-rate-limit-probe", "true"), 8)
        result["quota_limit"] = probe_limit(cfg, access_token, ("x-quota-probe", f"probe-{int(time.time())}"), 6)
        if cluster in ("ai-gw-kuadrant", "ai-gw-agent") and str(result["quota_limit"]).startswith("429"):
            result["quota_limit"] += "; shared bucket"
        if cluster == "ai-gw-agent":
            result["token_limit"] = "Not available on KServe InferencePool"
        elif cluster == "ai-gw-envoy":
            result["token_limit"] = probe_limit(cfg, access_token, ("x-ai-eg-model", "kimi-k3"), 6, BIG_CHAT, "ai.local")
        else:
            result["token_limit"] = probe_limit(cfg, access_token, ("x-token-limit-probe", "true"), 6, BIG_CHAT)
    else:
        for key in ("authentication", "authorization", "request_limit", "quota_limit", "token_limit"):
            result[key] = "No policy" if not policies_present else "Token error"

    code, _, cors_headers = request(
        cfg["base"] + "/v1/chat/completions", method="OPTIONS",
        headers={"origin": "https://console.example.com", "access-control-request-method": "POST", "access-control-request-headers": "authorization"},
    )
    result["cors"] = f"Yes (HTTP {code})" if any(k.lower() == "access-control-allow-origin" for k in cors_headers) else f"No (HTTP {code})"
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster", required=True, choices=CONFIG)
    parser.add_argument("--requests", type=int, default=30)
    args = parser.parse_args()
    if args.requests < 1:
        parser.error("--requests must be positive")
    result = run(args.cluster, args.requests)
    RESULTS.mkdir(parents=True, exist_ok=True)
    destination = RESULTS / f"{args.cluster}.json"
    destination.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"saved {destination.relative_to(ROOT)}")
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
