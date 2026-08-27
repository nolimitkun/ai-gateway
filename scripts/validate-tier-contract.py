#!/usr/bin/env python3
"""Validate the model-tier contract shared by all three gateway stacks.

The gateway APIs encode the same decision differently, so schema validation
alone cannot detect drift. This check binds the runtime catalog, auto default,
route sections, gateway-specific authorization sets, and fixed-tier production
routes into one offline contract.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared by the repo
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
REALM = ROOT / "keycloak" / "realm" / "ai-gateway-realm.json"
STACKS = ("kuadrant", "envoy-ai-gateway", "agentgateway")
DEPLOY = {stack: ROOT / stack / "deploy" for stack in STACKS}
TIERS = {"big", "medium", "small"}
TRANSCRIPTION_PATH = "/v1/audio/transcriptions"
TIER_LABEL = "ai-gateway.openai/model-tier"
PRODUCTION_TIERS = {
    "vllm-chat": "small",
    "vllm-embedding": "small",
    "vllm-rerank": "small",
    "vllm-transcription": "small",
}
EXPECTED_TIER_MODELS = {
    "big": {"kimi-k3", "glm-5.3", "deepseek-v4-pro"},
    "medium": {"deepseek-v4-flash"},
    "small": {
        "qwen3.8-27b",
        "qwen3-embedding-8b",
        "e5-mistral-7b-instruct",
        "voxtral-small-24b",
        "bge-m3",
        "jina-embeddings-v3",
        "nomic-embed-text-v2-moe",
        "bge-reranker-v2-m3",
        "jina-reranker-v2-base-multilingual",
        "whisper-large-v3",
        "voxtral-mini-3b",
    },
}

sys.path.insert(0, str(ROOT / "mock-llm"))
from server import CATALOG, TIER_MODELS  # noqa: E402


def documents(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def target_names(document: dict) -> set[str]:
    spec = document.get("spec", {})
    refs = spec.get("targetRefs", [])
    if spec.get("targetRef"):
        refs = [spec["targetRef"]]
    return {ref.get("name") for ref in refs if ref.get("name")}


def stack_gateways(stack: str) -> set[tuple[str, str]]:
    """Every Gateway a stack's profile provisions, as (name, namespace)."""
    return {
        (doc["metadata"]["name"], doc["metadata"].get("namespace", "ai-demo"))
        for doc in documents(DEPLOY[stack] / "gateway" / "gateway.yaml")
        if doc.get("kind") == "Gateway"
    }


def rendered(path: Path) -> list[dict]:
    """The overlay as `kubectl apply -k` sends it, patches included.

    Reading the files directly would miss the Kustomization, which is where
    each profile's gateway attachment lives.
    """
    try:
        build = subprocess.run(
            ["kubectl", "kustomize", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError:
        sys.exit("kubectl is required to render overlays offline")
    except subprocess.CalledProcessError as error:
        sys.exit(f"kubectl kustomize {path} failed: {error.stderr.strip()}")
    return [doc for doc in yaml.safe_load_all(build.stdout) if doc]


def gateway_refs(document: dict) -> list[tuple[str, str]]:
    """Gateways an HTTPRoute or LLMInferenceService attaches to."""
    spec = document.get("spec", {})
    if document.get("kind") == "HTTPRoute":
        refs = spec.get("parentRefs", [])
    elif document.get("kind") == "LLMInferenceService":
        refs = spec.get("router", {}).get("gateway", {}).get("refs", [])
    else:
        return []
    own_namespace = document["metadata"].get("namespace", "ai-demo")
    return [(ref["name"], ref.get("namespace", own_namespace)) for ref in refs]


def agent_big_models(path: Path) -> tuple[set[str], str]:
    docs = documents(path)
    policy = next(doc for doc in docs if doc["metadata"]["name"] == "kserve-mock-big-tier")
    expression = policy["spec"]["traffic"]["authorization"]["policy"]["matchExpressions"][0]
    match = re.search(r'x-gateway-model-name"\] in (\[[^]]*\])', expression)
    return (set(ast.literal_eval(match.group(1))) if match else set(), expression)


def envoy_big_models(path: Path) -> tuple[set[str], list[dict]]:
    docs = documents(path)
    base = next(doc for doc in docs if doc["metadata"]["name"] == "kserve-mock")
    rule = next(
        rule for rule in base["spec"]["authorization"]["rules"]
        if rule["name"] == "deny-big-tier"
    )
    values = rule["principal"]["headers"][0]["values"]
    return set(values), docs


def kuadrant_big_models(path: Path) -> tuple[set[str], dict]:
    policy = documents(path)[0]
    found = set()
    for name, rule in policy["spec"]["rules"]["authorization"].items():
        if "big-tier" not in name:
            continue
        for condition in rule.get("when", []):
            if condition.get("selector") == "request.headers.x-gateway-model-name":
                found.add(condition.get("value"))
    return found, policy


def main() -> int:
    problems: list[str] = []
    by_name = {entry["id"]: entry for entry in CATALOG}
    ungoverned = {
        entry["id"]: entry["tier"] for entry in CATALOG if entry["tier"] not in TIERS
    }
    if ungoverned:
        problems.append(f"catalog models outside big/medium/small: {ungoverned}")
    declared_tiers = {tier: set(models) for tier, models in TIER_MODELS.items()}
    if declared_tiers != EXPECTED_TIER_MODELS:
        problems.append(
            f"canonical tier sets are {declared_tiers}, expected {EXPECTED_TIER_MODELS}"
        )
    catalog_tiers = {
        entry["id"]: entry["tier"]
        for entry in CATALOG
        if entry["accelerator"] != "cpu"
    }
    expected_catalog_tiers = {
        model: tier for tier, models in EXPECTED_TIER_MODELS.items() for model in models
    }
    if catalog_tiers != expected_catalog_tiers:
        problems.append(
            f"catalog model tiers are {catalog_tiers}, expected {expected_catalog_tiers}"
        )
    wrong_accelerator_tier = {
        entry["id"]: (entry["accelerator"], entry["tier"])
        for entry in CATALOG
        if (
            (entry["accelerator"] == "b300" and entry["tier"] != "big")
            or (entry["accelerator"] == "h200" and entry["tier"] != "medium")
            or (entry["accelerator"] in {"h100", "l40s", "cpu"} and entry["tier"] != "small")
        )
    }
    if wrong_accelerator_tier:
        problems.append(f"accelerator/tier contract drift: {wrong_accelerator_tier}")
    expected_big_json = EXPECTED_TIER_MODELS["big"]

    # Keycloak exposes one model-access notion only: a scalar tier ceiling.
    realm = json.loads(REALM.read_text())
    expected_user_tiers = {
        "alice": "big",
        "bob": "medium",
        "mallory": None,
        "carol": "big",
        "dave": "medium",
        "erin": "small",
        "frank": "small",
    }
    actual_user_tiers = {
        user["username"]: (user.get("attributes", {}).get("tier") or [None])[0]
        for user in realm.get("users", [])
    }
    if actual_user_tiers != expected_user_tiers:
        problems.append(
            f"Keycloak user tier ceilings are {actual_user_tiers}, expected {expected_user_tiers}"
        )
    forbidden_access_notions = {"plan", "allowed-classes", "platform-admins", "model-users", "guests"}
    realm_text = REALM.read_text()
    leaked = sorted(name for name in forbidden_access_notions if name in realm_text)
    if leaked:
        problems.append(f"Keycloak still carries non-tier model-access notions: {leaked}")
    emitted_claims = {
        mapper.get("config", {}).get("claim.name")
        for scope in realm.get("clientScopes", [])
        for mapper in scope.get("protocolMappers", [])
    }
    if "tier" not in emitted_claims:
        problems.append("Keycloak does not emit the scalar tier claim")
    for user in realm.get("users", []):
        values = user.get("attributes", {}).get("tier", [])
        expected = expected_user_tiers.get(user["username"])
        if (expected is None and values) or (expected is not None and values != [expected]):
            problems.append(
                f"Keycloak user '{user['username']}' tier must be one scalar value; got {values}"
            )

    agent_models, agent_expression = agent_big_models(
        DEPLOY["agentgateway"] / "policies" / "auth-policy.yaml"
    )
    envoy_models, envoy_docs = envoy_big_models(
        DEPLOY["envoy-ai-gateway"] / "policies" / "security-policy.yaml"
    )
    kuadrant_models, kuadrant_policy = kuadrant_big_models(
        DEPLOY["kuadrant"] / "policies" / "auth-policy.yaml"
    )
    for stack, actual in (
        ("agentgateway", agent_models),
        ("Envoy Gateway", envoy_models),
        ("Kuadrant", kuadrant_models),
    ):
        if actual != expected_big_json:
            problems.append(
                f"{stack} big-tier JSON models are {sorted(actual)}, expected "
                f"{sorted(expected_big_json)} from the runtime catalog"
            )

    # The three policy engines spell the same ceiling differently, but none
    # may consult role, group, org, team, plan, or accelerator placement.
    agent_docs = documents(DEPLOY["agentgateway"] / "policies" / "auth-policy.yaml")
    agent_auth = {
        doc["metadata"]["name"]: doc.get("spec", {}).get("traffic", {}).get("authorization")
        for doc in agent_docs
        if doc.get("spec", {}).get("traffic", {}).get("authorization")
    }
    expected_agent_rules = {
        "kserve-mock-small-tier",
        "kserve-mock-medium-tier",
        "kserve-mock-big-tier",
    }
    if set(agent_auth) != expected_agent_rules:
        problems.append(f"agentgateway tier policies are {sorted(agent_auth)}, expected {sorted(expected_agent_rules)}")
    envoy_rules = next(doc for doc in envoy_docs if doc["metadata"]["name"] == "kserve-mock")["spec"]["authorization"]["rules"]
    expected_envoy_order = ["big-tier", "deny-big-tier", "medium-tier", "deny-medium-tier", "small-tier"]
    if [rule["name"] for rule in envoy_rules] != expected_envoy_order:
        problems.append("Envoy tier rule order does not implement big >= medium >= small")
    expected_kuadrant_rules = {
        "small-tier",
        "medium-tier",
        "kimi-k3-big-tier",
        "glm-5-3-big-tier",
        "deepseek-v4-pro-big-tier",
    }
    if set(kuadrant_policy["spec"]["rules"]["authorization"]) != expected_kuadrant_rules:
        problems.append("Kuadrant authorization rules are not the canonical small/medium/big tier ceiling")
    authorization_text = json.dumps(
        {
            "agent": agent_auth,
            "envoy": envoy_rules,
            "kuadrant": kuadrant_policy["spec"]["rules"]["authorization"],
        }
    )
    forbidden_authorization_inputs = (
        "groups",
        "group_paths",
        "auth.identity.org",
        "auth.identity.team",
        "plan",
        "accelerator",
    )
    leaked_inputs = [value for value in forbidden_authorization_inputs if value in authorization_text]
    if leaked_inputs:
        problems.append(f"model authorization still reads non-tier inputs: {leaked_inputs}")

    # Multipart bodies cannot be classified by BBR v1.2.1, but every speech
    # model is small. Keep the endpoint named for routing clarity and ensure no
    # stack accidentally adds a big-tier path restriction around it.
    for stack in STACKS:
        for variant in ("base", "pools"):
            route = yaml.safe_load(
                (DEPLOY[stack] / "kserve" / variant / "route.yaml").read_text()
            )
            transcription = [
                rule for rule in route["spec"]["rules"]
                if rule.get("name") == "transcription"
            ]
            if len(transcription) != 1 or transcription[0]["matches"][0]["path"].get("value") != TRANSCRIPTION_PATH:
                problems.append(f"{stack} {variant} route has no exact named transcription section")
    if TRANSCRIPTION_PATH in agent_expression:
        problems.append("agentgateway incorrectly treats small-tier transcription as big")
    kuadrant_rules = kuadrant_policy["spec"]["rules"]["authorization"]
    if any("transcription" in name for name in kuadrant_rules):
        problems.append("Kuadrant incorrectly treats small-tier transcription as big")
    envoy_transcription = next(
        (doc for doc in envoy_docs if doc["metadata"]["name"] == "kserve-mock-transcription"),
        None,
    )
    if envoy_transcription:
        problems.append("Envoy Gateway incorrectly treats small-tier transcription as big")

    # Auto must always land inside the governed tier set.
    defaults = set()
    for stack in STACKS:
        config = yaml.safe_load(
            (DEPLOY[stack] / "semantic-router" / "router-config.yaml").read_text()
        )
        default = config["providers"]["defaults"]["default_model"]
        defaults.add(default)
        if default not in by_name or by_name[default]["tier"] not in TIERS:
            problems.append(f"{stack} auto default '{default}' is outside big/medium/small")
    if len(defaults) != 1:
        problems.append(f"gateway auto defaults differ: {sorted(defaults)}")

    # Fixed-tier production routes and services must agree, support the auto
    # alias on the single chat model, and carry native policy coverage.
    all_routes = set(PRODUCTION_TIERS)
    for stack in STACKS:
        production = DEPLOY[stack] / "kserve" / "production"
        route_docs = documents(production / "routes.yaml")
        model_docs = documents(production / "models.yaml")
        route_tiers = {
            doc["metadata"]["name"]: doc["metadata"].get("labels", {}).get(TIER_LABEL)
            for doc in route_docs
        }
        model_tiers = {
            doc["metadata"]["name"]: doc["metadata"].get("labels", {}).get(TIER_LABEL)
            for doc in model_docs
        }
        if route_tiers != PRODUCTION_TIERS:
            problems.append(f"{stack} production route tiers are {route_tiers}, expected {PRODUCTION_TIERS}")
        if model_tiers != PRODUCTION_TIERS:
            problems.append(f"{stack} production model tiers are {model_tiers}, expected {PRODUCTION_TIERS}")
        chat = next(doc for doc in model_docs if doc["metadata"]["name"] == "vllm-chat")
        args = chat["spec"]["template"]["containers"][0]["args"]
        try:
            served = args[args.index("--served-model-name") + 1 : args.index("--port")]
        except ValueError:
            served = []
        if served[:2] != ["qwen3.8-27b", "auto"]:
            problems.append(f"{stack} production chat does not map auto to qwen3.8-27b")
        served_by_route = {}
        for document in model_docs:
            model = document["spec"]["model"]["name"]
            served_by_route[document["metadata"]["name"]] = model
        expected_production_models = {
            "vllm-chat": "qwen3.8-27b",
            "vllm-embedding": "qwen3-embedding-8b",
            "vllm-rerank": "bge-reranker-v2-m3",
            "vllm-transcription": "whisper-large-v3",
        }
        if served_by_route != expected_production_models:
            problems.append(
                f"{stack} production served models are {served_by_route}, expected "
                f"{expected_production_models}"
            )
        kustomization = yaml.safe_load((production / "kustomization.yaml").read_text())
        if "policies.yaml" not in kustomization.get("resources", []):
            problems.append(f"{stack} production kustomization omits policies.yaml")

        # routes.yaml and models.yaml are byte-identical across the three trees
        # and all name ai-gateway, so each profile's real attachment only exists
        # once the Kustomization is rendered. Kuadrant is what this catches: it
        # provisions no ai-demo/ai-gateway -- its one Gateway is
        # openshift-ingress/openshift-ai-inference -- and a route left pointing
        # at the shared name never attaches, which surfaces half an hour later
        # as a Ready timeout rather than as an error.
        provisioned = stack_gateways(stack)
        unprovisioned = {
            (doc["metadata"]["name"], ref)
            for doc in rendered(production)
            for ref in gateway_refs(doc)
            if ref not in provisioned
        }
        if unprovisioned:
            problems.append(
                f"{stack} production attaches to gateways the profile does not provision: "
                f"{sorted(unprovisioned)}; provisioned: {sorted(provisioned)}"
            )

    agent_production = documents(
        DEPLOY["agentgateway"] / "kserve" / "production" / "policies.yaml"
    )
    agent_by_name = {doc["metadata"]["name"]: target_names(doc) for doc in agent_production}
    if agent_by_name.get("vllm-jwt") != all_routes or agent_by_name.get("vllm-small-tier") != all_routes:
        problems.append("agentgateway production JWT/tier policies do not cover every route")
    if "vllm-big-tier" in agent_by_name:
        problems.append("agentgateway production has a big-tier policy but every route is small")

    envoy_production = documents(
        DEPLOY["envoy-ai-gateway"] / "kserve" / "production" / "policies.yaml"
    )
    envoy_by_name = {doc["metadata"]["name"]: target_names(doc) for doc in envoy_production}
    if set().union(*envoy_by_name.values()) != all_routes:
        problems.append("Envoy production SecurityPolicies do not cover every route")
    if "vllm-big-tier" in envoy_by_name:
        problems.append("Envoy production has a big-tier policy but every route is small")

    kuadrant_production = documents(
        DEPLOY["kuadrant"] / "kserve" / "production" / "policies.yaml"
    )
    kuadrant_by_target = {next(iter(target_names(doc))): doc for doc in kuadrant_production}
    if set(kuadrant_by_target) != all_routes:
        problems.append("Kuadrant production AuthPolicies do not cover every route")
    for route, document in kuadrant_by_target.items():
        rules = document.get("spec", {}).get("rules", {}).get("authorization", {})
        if "big-tier" in rules:
            problems.append(f"Kuadrant small production route '{route}' has big-tier authorization")

    for problem in problems:
        print(problem, file=sys.stderr)
    print(
        f"{len(expected_big_json)} big models, 1 medium model, 11 small accelerator models, "
        f"auto default {next(iter(defaults), 'missing')}, {len(all_routes)} protected "
        f"production routes across {len(STACKS)} gateways, {len(problems)} problems"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
