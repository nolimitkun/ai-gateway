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
        # Envoy's semantic processor and BBR share one ordered policy object;
        # the deployment distinguishes that chain from the BBR-only variant.
        "router": ("ai-demo", "envoyextensionpolicy/model-body-router-chat", "Accepted"),
    },
    "ai-gw-agent": {
        "context": "kind-ai-gw-agent", "base": "http://localhost:8081",
        "gateway": "ai-gateway", "gateway_ns": "ai-demo",
        "policy_condition": "Accepted",
        "policies": ["agentgatewaypolicy/kserve-mock-jwt", "agentgatewaypolicy/kserve-mock-small-tier", "agentgatewaypolicy/kserve-mock-medium-tier", "agentgatewaypolicy/kserve-mock-big-tier", "agentgatewaypolicy/kserve-mock-rate-limit", "agentgatewaypolicy/kserve-mock-cors"],
        # The semantic attachment replaces the pre-routing BBR policy under its
        # own name, so presence of the router is read from that object.
        "router": ("ai-demo", "agentgatewaypolicy/model-body-router", "Accepted"),
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


FORGEABLE_ORG_HEADER = {
    "ai-gw-envoy": "x-org-id",
    "ai-gw-kuadrant": "x-auth-org",
    "ai-gw-agent": "x-org-id",
}
# Kept in step with the policy YAML by scripts/validate-tenant-model.py. The
# classification below reads these caps rather than restating them, because a
# cap edited in one place and not the other would not fail loudly -- it would
# just relabel a healthy gateway as "unexpected".
TENANT_ORG_CAP = 5
TENANT_TEAM_CAP = 3
TENANT_USERS = {"team_a": "carol", "team_b": "dave", "other_org": "erin"}

# The hostname the native body-model routing overlay answers for, kept in step
# with both overlays by scripts/validate-native-routing.py. The overlay runs
# beside the body-based router rather than replacing it, so the same cluster
# serves the same request either way and these rows are a comparison rather
# than a before-and-after across two runs.
NATIVE_HOST = "native.local"
# Every model the BBR route maps, not one per accelerator class. The narrower
# sample -- kimi-k3, deepseek-v4-flash, qwen3.8-27b, bge-m3 -- reported "4/4
# pools" while four of the twelve models did not work natively at all on Envoy:
# both h100 embedding models answered 500 and both rerankers 404, on a path
# where the BBR route serves all twelve. One model per pool cannot see that,
# because the four it picked happened to be the working ones.
NATIVE_POOL_SAMPLES = (
    ("/v1/chat/completions", {"model": "kimi-k3"}, "b300"),
    ("/v1/chat/completions", {"model": "glm-5.3"}, "b300"),
    ("/v1/chat/completions", {"model": "deepseek-v4-pro"}, "b300"),
    ("/v1/chat/completions", {"model": "deepseek-v4-flash"}, "h200"),
    ("/v1/chat/completions", {"model": "qwen3.8-27b"}, "h100"),
    ("/v1/embeddings", {"model": "qwen3-embedding-8b", "input": "gateway inference"}, "h100"),
    ("/v1/embeddings", {"model": "e5-mistral-7b-instruct", "input": "gateway inference"}, "h100"),
    ("/v1/embeddings", {"model": "bge-m3", "input": "gateway inference"}, "l40s"),
    ("/v1/embeddings", {"model": "jina-embeddings-v3", "input": "gateway inference"}, "l40s"),
    ("/v1/embeddings", {"model": "nomic-embed-text-v2-moe", "input": "gateway inference"}, "l40s"),
    ("/v1/rerank", {"model": "bge-reranker-v2-m3", "query": "gateway", "documents": ["a", "b"]}, "l40s"),
    ("/v1/rerank", {"model": "jina-reranker-v2-base-multilingual", "query": "gateway", "documents": ["a", "b"]}, "l40s"),
)


def probe_tenants(cfg: dict, cluster: str) -> dict:
    """Measure whether org/team limit buckets nest, share, or go unenforced.

    Two members of two teams in one org, plus a member of a second org, run
    against TENANT_ORG_CAP and TENANT_TEAM_CAP. Each outcome has a different
    signature, so the row reports what happened rather than asserting that the
    intended one did:

      nested      team A stops at its own cap; team B stops *earlier* than its
                  own cap, because it inherits an org bucket team A already
                  spent down; the second org is untouched.
      team-only   both teams stop at their own cap and neither constrains the
                  other -- per-team buckets exist but the org ceiling above
                  them does not bind.
      shared      team B or the second org 429s on request 1, having inherited
                  team A's spending: one bucket for every tenant.
      unenforced  nothing 429s at all.

    Team B is deliberately classified by *whether* it stops early rather than
    by which request number it stops on. Envoy's limit service increments every
    matching descriptor and returns the logical OR of their decisions, so team
    A's rejected request still spends org budget; Limitador need not account
    for a rejected request the same way. The exact index therefore differs by
    one between correct implementations, and pinning it would report a healthy
    gateway as "unexpected". The caps are spaced so that team B runs out of org
    budget before its own cap under either accounting, which
    scripts/validate-tenant-model.py enforces.
    """
    run = f"tenant-{int(time.time())}"
    header = ("x-tenant-probe", run)
    tokens = {role: token(cfg, name) for role, name in TENANT_USERS.items()}
    if not all(tokens.values()):
        return {"tenant_nesting": "Token error", "tenant_header_spoof": "Token error"}

    # One past the team cap, which is where team A is expected to stop.
    team_a = probe_limit(cfg, tokens["team_a"], header, TENANT_TEAM_CAP + 1)
    team_b = probe_limit(cfg, tokens["team_b"], header, TENANT_TEAM_CAP + 1)

    # The org bucket is spent now and the second org is still untouched, which
    # is what makes this the moment to test the forgery: if the gateway lets a
    # client-supplied org header through, carol lands in globex's empty bucket
    # and the request succeeds. Overwritten from the verified claim, she stays
    # in her own exhausted one.
    forged = {header[0]: run, FORGEABLE_ORG_HEADER[cluster]: "globex"}
    forged_code, _, _ = json_call(cfg, "/v1/chat/completions", tokens["team_a"], CHAT, forged)

    other_org = probe_limit(cfg, tokens["other_org"], header, TENANT_TEAM_CAP)

    def stopped_at(outcome: str) -> int | None:
        match = re.match(r"429 on request (\d+)", outcome)
        return int(match.group(1)) if match else None

    at_a, at_b, at_other = (stopped_at(x) for x in (team_a, team_b, other_org))
    own_cap = TENANT_TEAM_CAP + 1
    if at_b == 1 or at_other == 1:
        verdict = "shared"
    elif at_a is None and at_b is None:
        verdict = "unenforced"
    elif at_a == own_cap and at_b is not None and 1 < at_b < own_cap and at_other is None:
        verdict = "nested"
    elif at_a == own_cap and at_b == own_cap and at_other is None:
        verdict = "team-only"
    else:
        verdict = "unexpected"
    return {
        "tenant_nesting": f"{verdict}: team A {team_a}; team B {team_b}; other org {other_org}",
        "tenant_header_spoof": (
            f"Ignored (HTTP {forged_code})" if forged_code == 429
            else f"HONOURED -- bucket escaped (HTTP {forged_code})" if forged_code == 200
            else f"Inconclusive (HTTP {forged_code})"
        ),
    }


def probe_shared_across_routes(cfg: dict, cluster: str) -> str:
    """Check that a tenant bucket spans both inference routes, not one each.

    Only Envoy Gateway has two of them (kserve-mock and the generated
    kserve-mock-ai), and its BackendTrafficPolicy targets the Gateway, where
    each route otherwise keeps its own counters. `shared: true` is what makes
    the org and team buckets span both; without it a tenant quietly gets one
    full budget per route. Nothing about the gateway looks wrong when that
    happens, which is why it is worth a probe of its own rather than trusting
    the field to stay set.

    The team bucket is filled on the first route, then one request goes to the
    second. A 429 means the two routes share the counter.
    """
    if cluster != "ai-gw-envoy":
        return "Single inference route"
    run = f"tenant-route-{int(time.time())}"
    header = ("x-tenant-probe", run)
    access_token = token(cfg, TENANT_USERS["team_a"])
    if not access_token:
        return "Token error"
    filled = probe_limit(cfg, access_token, header, TENANT_TEAM_CAP)
    if not filled.startswith("no 429"):
        return f"Inconclusive: first route stopped early ({filled})"
    # A small payload on the second route deliberately: the AI route matches
    # any x-ai-eg-model, and a big-tier one would spend the token budget the
    # token_limit row measures next.
    code, _, _ = json_call(
        cfg, "/v1/chat/completions", access_token, CHAT,
        {header[0]: run, "x-ai-eg-model": "mock-kserve", "host": "ai.local"},
    )
    return {
        429: "Shared across both routes",
        200: "SEPARATE bucket per route -- ceiling doubled",
    }.get(code, f"Inconclusive (HTTP {code})")


def task_codes(cfg: dict, access_token: str) -> list[int]:
    codes = [json_call(cfg, "/v1/models", access_token)[0]]
    codes.append(json_call(cfg, "/v1/chat/completions", access_token, CHAT)[0])
    codes.append(json_call(
        cfg, "/v1/embeddings", access_token,
        {"model": "mock-embedding", "input": "policy coverage"},
    )[0])
    codes.append(json_call(
        cfg, "/v1/rerank", access_token,
        {"model": "mock-reranker", "query": "gateway", "documents": ["gateway"]},
    )[0])
    code, _, _ = request(
        cfg["base"] + "/v1/audio/transcriptions", method="POST",
        headers=auth_headers(access_token),
        multipart={"file": ("policy.wav", b"", "audio/wav"), "model": "mock-whisper"},
    )
    codes.append(code)
    return codes


def probe_router_fail_open(cfg: dict, access_token: str) -> str:
    deployment = kube_json(cfg, "ai-demo", "deployment/semantic-router")
    replicas = deployment.get("spec", {}).get("replicas", 1)
    kubectl(cfg, "-n", "ai-demo", "scale", "deployment/semantic-router", "--replicas=0")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        pods = json.loads(kubectl(
            cfg, "-n", "ai-demo", "get", "pod", "-l", "app=semantic-router", "-o", "json",
        ))
        if not pods.get("items"):
            break
        time.sleep(2)
    else:
        raise RuntimeError("semantic-router pod did not terminate for the fail-open probe")

    explicit = auto = 0
    try:
        explicit, _, _ = json_call(cfg, "/v1/chat/completions", access_token, CHAT)
        auto, _, _ = json_call(
            cfg, "/v1/chat/completions", access_token,
            {"model": "auto", "messages": CHAT["messages"]},
        )
    finally:
        kubectl(cfg, "-n", "ai-demo", "scale", "deployment/semantic-router", f"--replicas={replicas}")
        kubectl(
            cfg, "-n", "ai-demo", "rollout", "status", "deployment/semantic-router",
            "--timeout=420s",
        )
        wait_for_router(cfg, access_token)
    return f"explicit {explicit} / auto {auto} / restored"


def native_payload(path: str, sample: dict) -> dict:
    return {**sample, "messages": CHAT["messages"]} if path.endswith("completions") else sample


def native_routing_present(cfg: dict, cluster: str) -> bool:
    """Is the no-BBR overlay installed on this cluster?"""
    object_name = {
        "ai-gw-envoy": "aigatewayroute/kserve-mock-native",
        "ai-gw-agent": "agentgatewaymodel/kimi-k3",
    }.get(cluster)
    return bool(object_name) and bool(kube_json(cfg, "ai-demo", object_name))


def native_sample_sweep(cfg: dict, access_token: str) -> list[bool]:
    """Send every model in NATIVE_POOL_SAMPLES and say which reached its pool."""
    reached = []
    for path, sample, accelerator in NATIVE_POOL_SAMPLES:
        # Twelve models is enough traffic that a single cold pool shows up as a
        # miss. One retry separates that from a model the path genuinely cannot
        # serve, which is what this row exists to report.
        for _ in range(2):
            code, body, _ = json_call(
                cfg, path, access_token, native_payload(path, sample), {"host": NATIVE_HOST},
            )
            ok = code == 200 and body.get("mock_accelerator") == accelerator
            if ok:
                break
        reached.append(ok)
    return reached


def probe_native_routing(cfg: dict, access_token: str) -> str:
    """Do the model rules reach their pools with the body-based router gone?

    The whole sweep runs with BBR scaled to zero, and the count comes from
    those requests. Measuring with BBR up and then re-sending a single model
    with it down would let the row say "twelve models, still routed with BBR
    scaled to 0" on the strength of one chat request: embeddings and rerank
    reach their pools through different route and filter attachments, and
    nothing about them can be inferred from kimi-k3. It would also skip the
    scale-down entirely whenever any model failed, which is exactly the case
    -- Envoy's -- where what happens without BBR is the thing in question.

    A pool reached with no BBR pod alive cannot have been reached through BBR,
    which is the only claim this row makes.
    """
    deployment = kube_json(cfg, "ai-demo", "deployment/body-based-router")
    if not deployment:
        reached = native_sample_sweep(cfg, access_token)
        return f"{sum(reached)}/{len(reached)} models; BBR already absent"

    replicas = deployment.get("spec", {}).get("replicas", 1)
    kubectl(cfg, "-n", "ai-demo", "scale", "deployment/body-based-router", "--replicas=0")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        pods = json.loads(kubectl(
            cfg, "-n", "ai-demo", "get", "pod", "-l", "app=body-based-router", "-o", "json",
        ))
        if not pods.get("items"):
            break
        time.sleep(2)
    else:
        raise RuntimeError("body-based-router pod did not terminate for the native probe")
    try:
        reached = native_sample_sweep(cfg, access_token)
    finally:
        kubectl(
            cfg, "-n", "ai-demo", "scale", "deployment/body-based-router",
            f"--replicas={replicas}",
        )
        kubectl(
            cfg, "-n", "ai-demo", "rollout", "status", "deployment/body-based-router",
            "--timeout=420s",
        )

    if all(reached):
        return f"{sum(reached)}/{len(reached)} models with BBR scaled to 0"
    missed = ", ".join(
        sample["model"] for (_, sample, _), ok in zip(NATIVE_POOL_SAMPLES, reached) if not ok
    )
    return f"{sum(reached)}/{len(reached)} models with BBR scaled to 0; missed {missed}"


def probe_native_spoof(cfg: dict, cluster: str, access_token: str) -> str:
    """Can a client pick its own pool by forging the model header?

    On the BBR path the answer is no, because BBR overwrites the header from
    the body. The native path has no such filter: Envoy AI Gateway derives
    `x-ai-eg-model` itself, and whether that derivation overwrites a client
    value or merely fills in a missing one is not something any schema says.
    The body asks for the H100 model and the header for a B300 one, so the
    pool that answers names the winner.
    """
    if cluster != "ai-gw-envoy":
        return "No client-visible model header"
    code, body, _ = json_call(
        cfg, "/v1/chat/completions", access_token,
        {"model": "qwen3.8-27b", "messages": CHAT["messages"]},
        {"host": NATIVE_HOST, "x-ai-eg-model": "kimi-k3"},
    )
    pool = body.get("mock_accelerator")
    if code == 200 and pool == "h100":
        return "Body wins; forged header ignored"
    if code == 200 and pool == "b300":
        return "FORGEABLE -- client header chose the pool"
    return f"Inconclusive (HTTP {code}, pool {pool})"


def probe_native_tier_ceiling(cfg: dict) -> str:
    """Does the tier ceiling still bind with no routing header to gate on?

    This is the question the overlay exists to answer. Envoy re-points the
    same rule at the header its own processor writes, which only binds if that
    processor runs before authorization -- a filter-order question. agentgateway
    attaches the rule to the model itself, where no ordering is involved.

    Both rungs of the ceiling are measured, because a rule that names the wrong
    tier fails in only one direction: Dave is medium, so he must be refused the
    big-tier model and served the medium one, and Carol is big, so she reaches
    both.
    """
    dave, carol = token(cfg, "dave"), token(cfg, "carol")
    dave_big, _, _ = json_call(
        cfg, "/v1/chat/completions", dave, BIG_CHAT, {"host": NATIVE_HOST},
    )
    carol_big, _, _ = json_call(
        cfg, "/v1/chat/completions", carol, BIG_CHAT, {"host": NATIVE_HOST},
    )
    dave_medium, _, _ = json_call(
        cfg, "/v1/chat/completions", dave,
        {"model": "deepseek-v4-flash", "messages": CHAT["messages"]},
        {"host": NATIVE_HOST},
    )
    summary = f"big: medium {dave_big} / big {carol_big}; medium: medium {dave_medium}"
    if dave_big == 403 and carol_big == 200 and dave_medium == 200:
        return f"Held ({summary})"
    if dave_big == 200:
        return f"OPEN -- medium tier reached a big model ({summary})"
    return summary


def probe_native_gaps(cfg: dict, access_token: str) -> str:
    """What does not follow a caller who switches to the native hostname.

    Three things could be left behind, and which ones are is a per-stack
    answer rather than a property of the idea. The model catalog is a bodyless
    GET, so no rule that matches on a model name can match it. `auto` is
    resolved by the semantic router, which is attached to the BBR route on
    Envoy but to the Gateway on agentgateway, so it follows on one and not the
    other. Speech is multipart, which no body-model mechanism here reads, and
    every transcription model is small-tier and served by the shared fixture on
    both paths -- so it should follow, and this records whether it does.

    Rate limiting is deliberately not probed here: it shares one bucket with
    the request_limit row, and spending it would corrupt that measurement. It
    was measured by hand instead and does follow -- see docs/open-questions.md.
    """
    catalog, _, _ = json_call(cfg, "/v1/models", access_token, None, {"host": NATIVE_HOST})
    auto, _, _ = json_call(
        cfg, "/v1/chat/completions", access_token,
        {"model": "auto", "messages": CHAT["messages"]}, {"host": NATIVE_HOST},
    )
    speech, _, _ = request(
        cfg["base"] + "/v1/audio/transcriptions", method="POST",
        headers={"authorization": f"Bearer {access_token}", "host": NATIVE_HOST},
        multipart={"file": ("native.wav", b"", "audio/wav"), "model": "mock-whisper"},
    )
    return f"catalog {catalog} / auto {auto} / speech {speech}"


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
    # Envoy and agentgateway both let the semantic attachment replace their
    # base body-based-router policy under the same name, so the object exists
    # either way and only the processor workload distinguishes them.
    if cfg["context"] in ("kind-ai-gw-envoy", "kind-ai-gw-agent") and not kube_json(
        cfg, "ai-demo", "deployment/semantic-router"
    ):
        return False, "Absent"
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
    route_names = [rule.get("name") for rule in route.get("spec", {}).get("rules", [])]
    base_rules = ["chat", "embeddings", "rerank", "transcription", "inference"]
    model_rules = [
        rule for rule in route.get("spec", {}).get("rules", [])
        if str(rule.get("name", "")).startswith("model-")
    ]
    model_rule_count = len(model_rules)
    model_mapping_count = sum(len(rule.get("matches", [])) for rule in model_rules)
    result["route_rules"] = (
        f"{model_mapping_count} body.model mappings / {model_rule_count} pool rules"
        if model_rule_count and route_names[-5:] == base_rules
        else ("3 JSON tasks + /v1 fallback" if route_names == base_rules else "Unexpected")
    )

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

    pool_names = {
        item.get("metadata", {}).get("name")
        for item in owned.get("items", [])
        if item.get("kind") == "InferencePool"
    }
    if "kserve-b300-inference-pool" in pool_names:
        body_routes = []
        for path, payload, accelerator in (
            ("/v1/chat/completions", {"model": "kimi-k3", "messages": CHAT["messages"]}, "b300"),
            ("/v1/chat/completions", {"model": "deepseek-v4-flash", "messages": CHAT["messages"]}, "h200"),
            ("/v1/chat/completions", {"model": "qwen3.8-27b", "messages": CHAT["messages"]}, "h100"),
            ("/v1/embeddings", {"model": "bge-m3", "input": "gateway inference"}, "l40s"),
        ):
            route_code, route_body, _ = json_call(cfg, path, access_token, payload)
            body_routes.append(
                route_code == 200
                and route_body.get("mock_accelerator") == accelerator
            )
        spoof_code, spoof_body, _ = json_call(
            cfg,
            "/v1/chat/completions",
            access_token,
            {"model": "kimi-k3", "messages": CHAT["messages"]},
            {"x-gateway-model-name": "qwen3.8-27b"},
        )
        spoof_safe = (
            spoof_code == 200
            and spoof_body.get("mock_accelerator") == "b300"
        )
        # The routing header is not the only one a client can forge. Where a
        # stack derives it from a header the semantic router sets, that header
        # has to be trusted on the chat path only: nothing stops a client
        # sending x-selected-model, and BBR does not strip it on the task
        # paths. An unscoped copy sends a task request to a pool that does not
        # serve its body model, which reads as an ordinary 404 or a silent
        # fallback to the CPU fixture rather than as a routing defect.
        selected_code, selected_body, _ = json_call(
            cfg,
            "/v1/rerank",
            access_token,
            {"model": "bge-reranker-v2-m3", "query": "gateway", "documents": ["a", "b"]},
            {"x-selected-model": "qwen3-embedding-8b"},
        )
        selected_safe = (
            selected_code == 200
            and selected_body.get("mock_accelerator") == "l40s"
        )
        if all(body_routes) and spoof_safe and selected_safe:
            result["model_body_routing"] = "4/4 pools; client headers overwritten"
        else:
            failed = []
            if not all(body_routes):
                failed.append("pool routing")
            if not spoof_safe:
                failed.append("x-gateway-model-name forgeable")
            if not selected_safe:
                failed.append(
                    "x-selected-model steers task pools "
                    f"(HTTP {selected_code}, pool {selected_body.get('mock_accelerator')})"
                )
            result["model_body_routing"] = "Failed: " + "; ".join(failed)
    else:
        result["model_body_routing"] = "Pools not installed"

    stream_payload = dict(CHAT, stream=True, stream_options={"include_usage": True})
    code, raw, _ = request(cfg["base"] + "/v1/chat/completions", method="POST", headers=auth_headers(access_token), json_body=stream_payload)
    result["streaming_usage"] = "Yes" if code == 200 and b'"usage"' in raw else "No"
    result["stream_termination"] = "Yes" if code == 200 and raw.rstrip().endswith(b"data: [DONE]") else "No"

    code, models, _ = json_call(cfg, "/v1/models", access_token)
    result["model_catalog"] = f"{len(models.get('data', []))} models" if code == 200 else "No"
    tier_ok = []
    for model, tier in (("kimi-k3", "big"), ("deepseek-v4-flash", "medium"), ("qwen3.8-27b", "small")):
        code, body, _ = json_call(cfg, "/v1/chat/completions", access_token, {"model": model, "messages": CHAT["messages"]})
        tier_ok.append(code == 200 and body.get("model") == model and body.get("mock_tier") == tier)
    result["tiered_chat"] = "big/medium/small" if all(tier_ok) else "No"

    embeddings_code, embeddings, _ = json_call(cfg, "/v1/embeddings", access_token, {"model": "mock-embedding", "input": ["one", "two"]})
    result["embeddings"] = "Yes" if embeddings_code == 200 and len(embeddings.get("data", [])) == 2 else "No"
    rag_ok = True
    for model, dimensions in (("qwen3-embedding-8b", 4096), ("bge-m3", 1024)):
        code, body, _ = json_call(cfg, "/v1/embeddings", access_token, {"model": model, "input": ["RAG"]})
        data = body.get("data", [])
        rag_ok &= code == 200 and bool(data) and len(data[0].get("embedding", [])) == dimensions
    result["rag_embeddings"] = "Yes" if rag_ok else "No"
    accepted, reduced, _ = json_call(cfg, "/v1/embeddings", access_token, {"model": "qwen3-embedding-8b", "input": "chunk", "dimensions": 512})
    rejected, _, _ = json_call(cfg, "/v1/embeddings", access_token, {"model": "bge-m3", "input": "chunk", "dimensions": 512})
    result["embedding_dimensions"] = "512 accepted / fixed-size rejected" if accepted == 200 and len(reduced.get("data", [{}])[0].get("embedding", [])) == 512 and rejected == 400 else "No"
    rerank_code, rerank, _ = json_call(cfg, "/v1/rerank", access_token, {"model": "mock-reranker", "query": "gateway inference", "documents": ["other", "gateway inference"], "top_n": 1})
    rerank_results = rerank.get("results", [])
    result["reranking"] = "Yes" if rerank_code == 200 and rerank_results and rerank_results[0].get("index") == 1 else "No"

    headers = auth_headers(access_token)
    stt_code, raw, _ = request(cfg["base"] + "/v1/audio/transcriptions", method="POST", headers=headers, multipart={"file": ("sample.wav", b"", "audio/wav"), "model": "mock-whisper"})
    stt = body_json(raw)
    result["stt"] = "Yes" if stt_code == 200 and stt.get("model") == "mock-whisper" else "No"
    code, raw, _ = request(cfg["base"] + "/v1/audio/transcriptions", method="POST", headers=headers, multipart={"file": ("meeting.wav", b"", "audio/wav"), "model": "whisper-large-v3", "diarization": "true", "num_speakers": "3"})
    diarization = body_json(raw)
    speakers = {segment.get("speaker") for segment in diarization.get("segments", [])}
    result["diarization"] = f"{len(speakers)} speakers" if code == 200 and diarization.get("diarization") and speakers else "No"
    code, raw, _ = request(cfg["base"] + "/v1/audio/transcriptions", method="POST", headers=headers, multipart={"file": ("asr.wav", b"", "audio/wav"), "model": "voxtral-mini-3b", "diarization": "true"})
    result["stt_validation"] = "ASR-only diarization rejected" if code == 400 and body_json(raw).get("error") else "No"

    negative_codes = [
        json_call(cfg, "/v1/chat/completions", access_token, {"model": "missing-model", "messages": CHAT["messages"]})[0],
        json_call(cfg, "/v1/chat/completions", access_token, {"model": "mock-embedding", "messages": CHAT["messages"]})[0],
        json_call(cfg, "/v1/chat/completions", access_token, {"model": "mock-kserve", "messages": CHAT["messages"], "max_tokens": 1025})[0],
        rejected,
        code,
    ]
    result["negative_contracts"] = "404 / 400 / 400 / 400 / 400" if negative_codes == [404, 400, 400, 400, 400] else " / ".join(map(str, negative_codes))

    task_outcomes = [
        (embeddings_code, embeddings.get("mock_routing_headers", {})),
        (rerank_code, rerank.get("mock_routing_headers", {})),
        (stt_code, stt.get("mock_routing_headers", {})),
    ]
    result["router_scope"] = "3/3 non-chat tasks bypassed" if router_present and all(
        status == 200 and not routing_headers
        for status, routing_headers in task_outcomes
    ) else ("No router" if not router_present else "Failed")

    result["policy_ready"] = policy_ready
    result["router_attachment"] = router_ready
    if router_present:
        selected = []
        upstream = []
        auto_pools = []
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
            auto_pools.append(body.get("mock_accelerator", "unknown") if code == 200 else f"HTTP {code}")
            system_prompts += int(bool(body.get("mock_system_prompt")))
            decisions.append(
                response_headers.get("x-vsr-selected-decision")
                or response_headers.get("x-vsr-selected-model")
                or "No header"
            )
        # Envoy chooses an initial HTTPRoute rule before ext_proc runs. Prove
        # that a forged internal model header cannot make model:auto skip the
        # semantic processor on a model-specific chat section.
        spoof_code, spoof_body, _ = json_call(
            cfg,
            "/v1/chat/completions",
            access_token,
            {
                "model": "auto",
                "messages": [{"role": "user", "content": AUTO_PROMPTS[0]}],
            },
            {"x-gateway-model-name": "qwen3.8-27b"},
        )
        spoof_selected = spoof_body.get("model")
        spoof_upstream = spoof_body.get("mock_routing_headers", {}).get(
            "x-selected-model"
        )
        auto_models = " / ".join(selected)
        if not (
            spoof_code == 200
            and spoof_selected == selected[0]
            and spoof_upstream == selected[0]
        ):
            auto_models += (
                "; forged-header probe failed "
                f"(HTTP {spoof_code}, model {spoof_selected}, upstream {spoof_upstream})"
            )
        result["auto_models"] = auto_models
        result["auto_upstream"] = f"{' / '.join(upstream)}; system prompt {system_prompts}/{len(AUTO_PROMPTS)}"
        result["auto_pools"] = " / ".join(auto_pools)
        result["auto_decision"] = " / ".join(decisions)
        result["auto_p50"] = f"{round(statistics.median(auto_latencies))} ms"
        result["router_fail_open"] = probe_router_fail_open(cfg, access_token)
    else:
        for key in ("auto_models", "auto_upstream", "auto_pools", "auto_decision", "auto_p50", "router_fail_open"):
            result[key] = "No router"

    result["token_issuance"] = "Yes" if access_token else "No"
    if policies_present and access_token:
        anonymous, _, _ = json_call(cfg, "/v1/chat/completions", "", CHAT)
        forged, _, _ = json_call(cfg, "/v1/chat/completions", "not.a.real.token", CHAT)
        valid, _, _ = json_call(cfg, "/v1/chat/completions", access_token, CHAT)
        result["authentication"] = f"{anonymous} / {forged} / {valid}"
        anonymous_tasks = task_codes(cfg, "")
        valid_tasks = task_codes(cfg, access_token)
        result["task_authentication"] = (
            "5/5 denied / 5/5 allowed"
            if anonymous_tasks == [401] * 5 and valid_tasks == [200] * 5
            else f"anonymous {anonymous_tasks} / valid {valid_tasks}"
        )
        _, identity_body, _ = json_call(cfg, "/v1/chat/completions", access_token, CHAT)
        # Report only verified identity evidence. BBR's internal model header
        # may also reach the fixture and is validated by the routing probes;
        # it is not an identity header and should not contaminate this row.
        identity_headers = {
            key: value
            for key, value in identity_body.get("mock_gateway_headers", {}).items()
            if key in {"x-auth-user", "x-auth-tier", "x-user-id"}
        }
        expected_identity = {
            "ai-gw-kuadrant": {"x-auth-user": "alice", "x-auth-tier": "big"},
            "ai-gw-envoy": {"x-user-id": "alice", "x-auth-tier": "big"},
            "ai-gw-agent": {},
        }[cluster]
        if identity_headers == expected_identity:
            result["identity_headers"] = "Not configured" if not expected_identity else ", ".join(f"{key}={value}" for key, value in sorted(identity_headers.items()))
        else:
            result["identity_headers"] = f"Unexpected: {identity_headers}"
        mallory, bob = token(cfg, "mallory"), token(cfg, "bob")
        guest, _, _ = json_call(cfg, "/v1/chat/completions", mallory, CHAT)
        restricted, _, _ = json_call(cfg, "/v1/chat/completions", bob, BIG_CHAT)
        allowed, _, _ = json_call(cfg, "/v1/chat/completions", access_token, BIG_CHAT)
        result["authorization"] = f"{guest} / {restricted} / {allowed}"
        # Tier authorization is model-based: only B300 chat is big. H200 chat
        # is medium, while H100 embeddings and L40S transcription are small.
        # Auto must be judged on the selected model, not the literal alias.
        dave, carol = token(cfg, "dave"), token(cfg, "carol")
        medium_ceiling = [
            json_call(cfg, "/v1/chat/completions", dave, BIG_CHAT)[0],
            json_call(
                cfg, "/v1/chat/completions", dave,
                {"model": "deepseek-v4-flash", "messages": CHAT["messages"]},
            )[0],
            json_call(
                cfg, "/v1/embeddings", dave,
                {"model": "qwen3-embedding-8b", "input": "tier policy"},
            )[0],
            request(
                cfg["base"] + "/v1/audio/transcriptions", method="POST",
                headers=auth_headers(dave),
                multipart={"file": ("tier.wav", b"", "audio/wav"), "model": "whisper-large-v3"},
            )[0],
        ]
        big_ceiling = [
            json_call(cfg, "/v1/chat/completions", carol, BIG_CHAT)[0],
            json_call(
                cfg, "/v1/chat/completions", carol,
                {"model": "deepseek-v4-flash", "messages": CHAT["messages"]},
            )[0],
            json_call(
                cfg, "/v1/embeddings", carol,
                {"model": "qwen3-embedding-8b", "input": "tier policy"},
            )[0],
            request(
                cfg["base"] + "/v1/audio/transcriptions", method="POST",
                headers=auth_headers(carol),
                multipart={"file": ("tier.wav", b"", "audio/wav"), "model": "whisper-large-v3"},
            )[0],
        ]
        if router_present:
            auto_codes = [
                json_call(
                    cfg, "/v1/chat/completions", dave,
                    {"model": "auto", "messages": [{"role": "user", "content": prompt}]},
                )[0]
                for prompt in AUTO_PROMPTS
            ]
            auto_summary = "auto 403/200/200" if auto_codes == [403, 200, 200] else f"auto {auto_codes}"
        else:
            auto_summary = "auto not installed"
        if medium_ceiling == [403, 200, 200, 200] and big_ceiling == [200] * 4:
            result["tier_authorization"] = (
                f"medium ceiling 403/200/200/200; big ceiling 4/4; {auto_summary}"
            )
        else:
            result["tier_authorization"] = (
                f"medium {medium_ceiling} / big {big_ceiling}; {auto_summary}"
            )
        result["request_limit"] = probe_limit(cfg, access_token, ("x-rate-limit-probe", "true"), 8)
        bob_rate, _, _ = json_call(cfg, "/v1/chat/completions", bob, CHAT, {"x-rate-limit-probe": "true"})
        expected_bob = 200 if cluster == "ai-gw-envoy" else 429
        result["rate_scope"] = (
            ("per-user; Bob HTTP 200" if expected_bob == 200 else "shared; Bob HTTP 429")
            if bob_rate == expected_bob else f"Unexpected Bob HTTP {bob_rate}"
        )
        result["quota_limit"] = probe_limit(cfg, access_token, ("x-quota-probe", f"probe-{int(time.time())}"), 6)
        if cluster in ("ai-gw-kuadrant", "ai-gw-agent") and str(result["quota_limit"]).startswith("429"):
            result["quota_limit"] += "; shared bucket"
        # Model access is independent of org and team: only the token's tier
        # ceiling matters. Carol is big and Dave is medium.
        carol_big, _, _ = json_call(cfg, "/v1/chat/completions", token(cfg, "carol"), BIG_CHAT)
        dave_big, _, _ = json_call(cfg, "/v1/chat/completions", token(cfg, "dave"), BIG_CHAT)
        result["tier_ceiling"] = f"medium {dave_big} / big {carol_big}"
        # Frank shares Carol's team name but has a small tier. This proves
        # tenancy metadata cannot accidentally grant model access.
        frank_big, _, _ = json_call(cfg, "/v1/chat/completions", token(cfg, "frank"), BIG_CHAT)
        result["tier_independent_of_tenant"] = {
            403: "Denied by small tier (HTTP 403)",
            200: "GRANTED -- tenant metadata bypassed tier (HTTP 200)",
        }.get(frank_big, f"Inconclusive (HTTP {frank_big})")

        # Native body-model routing: the same request, routed without the
        # external body-based router every stack currently runs. Istio has no
        # body-aware routing API, so that column reports the capability
        # difference rather than a measurement of one.
        if cluster == "ai-gw-kuadrant":
            native = "No body-aware routing API (raw EnvoyFilter drives BBR)"
            result.update({
                "native_routing": native,
                "native_spoof": native,
                "native_tier_ceiling": native,
                "native_gaps": native,
            })
        elif not native_routing_present(cfg, cluster):
            not_installed = "Not installed (make native-routing)"
            result.update({
                "native_routing": not_installed,
                "native_spoof": not_installed,
                "native_tier_ceiling": not_installed,
                "native_gaps": not_installed,
            })
        else:
            result["native_routing"] = probe_native_routing(cfg, access_token)
            result["native_spoof"] = probe_native_spoof(cfg, cluster, access_token)
            result["native_tier_ceiling"] = probe_native_tier_ceiling(cfg)
            result["native_gaps"] = probe_native_gaps(cfg, access_token)
        if cluster == "ai-gw-agent":
            result.update({
                "tenant_nesting": "Needs an external rate limit service",
                "tenant_header_spoof": "Not applicable without tenant buckets",
            })
        else:
            result.update(probe_tenants(cfg, cluster))
        result["tenant_route_scope"] = probe_shared_across_routes(cfg, cluster)
        if cluster == "ai-gw-agent":
            result["token_limit"] = "Not available on KServe InferencePool"
        elif cluster == "ai-gw-envoy":
            result["token_limit"] = probe_limit(cfg, access_token, ("x-ai-eg-model", "kimi-k3"), 6, BIG_CHAT, "ai.local")
        else:
            result["token_limit"] = probe_limit(cfg, access_token, ("x-token-limit-probe", "true"), 6, BIG_CHAT)
    else:
        for key in ("authentication", "task_authentication", "identity_headers", "authorization", "tier_authorization", "tier_ceiling", "tier_independent_of_tenant", "request_limit", "rate_scope", "quota_limit", "tenant_nesting", "tenant_header_spoof", "tenant_route_scope", "token_limit", "native_routing", "native_spoof", "native_tier_ceiling", "native_gaps"):
            result[key] = "No policy" if not policies_present else "Token error"

    code, _, cors_headers = request(
        cfg["base"] + "/v1/chat/completions", method="OPTIONS",
        headers={"origin": "https://console.example.com", "access-control-request-method": "POST", "access-control-request-headers": "authorization"},
    )
    result["cors"] = f"Yes (HTTP {code})" if any(k.lower() == "access-control-allow-origin" for k in cors_headers) else f"No (HTTP {code})"
    denied_code, _, denied_headers = request(
        cfg["base"] + "/v1/chat/completions", method="OPTIONS",
        headers={"origin": "https://evil.example.com", "access-control-request-method": "POST"},
    )
    result["cors_rejection"] = (
        f"No allow-origin (HTTP {denied_code})"
        if not any(k.lower() == "access-control-allow-origin" for k in denied_headers)
        else "Failed"
    )

    if cluster == "ai-gw-kuadrant":
        tls_policy = kube_json(cfg, "ai-demo", "backendtlspolicy/kserve-mock-epp")
        result["epp_transport"] = "TLS policy ready" if any(c.get("status") == "True" for c in conditions(tls_policy)) else "TLS policy not ready"
    else:
        result["epp_transport"] = "Plaintext in local fixture"
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
