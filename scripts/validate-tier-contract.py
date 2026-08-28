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
    "vllm-kimi-k3": "big",
    "vllm-glm-5-3": "big",
    "vllm-deepseek-v4-pro": "big",
    "vllm-deepseek-v4-flash": "medium",
    "vllm-embedding": "small",
    "vllm-rerank": "small",
    "vllm-transcription": "small",
}
PRODUCTION_MODELS = {
    "vllm-chat": "qwen3.8-27b",
    "vllm-kimi-k3": "kimi-k3",
    "vllm-glm-5-3": "glm-5.3",
    "vllm-deepseek-v4-pro": "deepseek-v4-pro",
    "vllm-deepseek-v4-flash": "deepseek-v4-flash",
    "vllm-embedding": "qwen3-embedding-8b",
    "vllm-rerank": "bge-reranker-v2-m3",
    "vllm-transcription": "whisper-large-v3",
}
# The chat services that share /v1/chat/completions and are separated only by
# the header BBR writes. Each needs its rule covered by the stack's ext_proc
# attachment, including the unheadered fallback.
PRODUCTION_CHAT_SECTIONS = {
    "vllm-chat": "vllm-chat",
    "vllm-kimi-k3": "vllm-chat-kimi-k3",
    "vllm-glm-5-3": "vllm-chat-glm-5-3",
    "vllm-deepseek-v4-pro": "vllm-chat-deepseek-v4-pro",
    "vllm-deepseek-v4-flash": "vllm-chat-deepseek-v4-flash",
}
# What a route's own policy must admit, given its tier label.
TIER_CEILING = {
    "big": {"big"},
    "medium": {"big", "medium"},
    "small": {"big", "medium", "small"},
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

    # Production serves all three tiers. Each route's label, its service's
    # label, the model it serves, the header that selects it, and the policy
    # attached to it must all agree, and `auto` must stay on the small model.
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
        served_by_route = {
            document["metadata"]["name"]: document["spec"]["model"]["name"]
            for document in model_docs
        }
        if served_by_route != PRODUCTION_MODELS:
            problems.append(
                f"{stack} production served models are {served_by_route}, expected "
                f"{PRODUCTION_MODELS}"
            )

        # Every service's tier label must be the tier its model actually has.
        mislabelled = {
            route: (served_by_route.get(route), tier)
            for route, tier in PRODUCTION_TIERS.items()
            if by_name.get(served_by_route.get(route), {}).get("tier") != tier
        }
        if mislabelled:
            problems.append(
                f"{stack} production labels disagree with the catalog tier: {mislabelled}"
            )

        # The five chat services share /v1/chat/completions. Every one but the
        # unheadered fallback must select on the header BBR writes, matching the
        # model it serves -- otherwise two routes tie and one silently wins.
        rules_by_route = {
            doc["metadata"]["name"]: doc["spec"]["rules"] for doc in route_docs
        }
        for route, section in PRODUCTION_CHAT_SECTIONS.items():
            rule = next(
                (item for item in rules_by_route.get(route, []) if item.get("name") == section),
                None,
            )
            if rule is None:
                problems.append(f"{stack} production route '{route}' has no '{section}' section")
                continue
            headers = {
                header["name"]: header["value"]
                for match in rule["matches"]
                for header in match.get("headers", [])
            }
            expected = {} if route == "vllm-chat" else {"x-gateway-model-name": PRODUCTION_MODELS[route]}
            if headers != expected:
                problems.append(
                    f"{stack} production section '{section}' matches {headers}, expected {expected}"
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

    def expected_ceiling(routes: set[str]) -> set[str]:
        """The tiers a policy covering exactly these routes must admit."""
        return set().union(*(TIER_CEILING[PRODUCTION_TIERS[route]] for route in routes))

    def routes_at(tier: str) -> set[str]:
        return {route for route, value in PRODUCTION_TIERS.items() if value == tier}

    # Each stack spells the ceiling differently, but all three must end up with
    # exactly one policy per tier, covering exactly that tier's routes and
    # admitting exactly the claims that tier permits. A policy that covered two
    # tiers at once would silently grant the lower one access to the higher.
    agent_production = documents(
        DEPLOY["agentgateway"] / "kserve" / "production" / "policies.yaml"
    )
    agent_by_name = {doc["metadata"]["name"]: doc for doc in agent_production}
    if target_names(agent_by_name.get("vllm-jwt", {})) != all_routes:
        problems.append("agentgateway production JWT policy does not cover every route")
    for tier in sorted(TIERS):
        policy = agent_by_name.get(f"vllm-{tier}-tier")
        if policy is None:
            problems.append(f"agentgateway production has no vllm-{tier}-tier policy")
            continue
        if target_names(policy) != routes_at(tier):
            problems.append(
                f"agentgateway production vllm-{tier}-tier covers {sorted(target_names(policy))}, "
                f"expected {sorted(routes_at(tier))}"
            )
        expression = policy["spec"]["traffic"]["authorization"]["policy"]["matchExpressions"][0]
        admitted = {value for value in re.findall(r'"([^"]+)"', expression) if value in TIERS}
        if admitted != TIER_CEILING[tier]:
            problems.append(
                f"agentgateway production vllm-{tier}-tier admits {sorted(admitted)}, "
                f"expected {sorted(TIER_CEILING[tier])}"
            )

    envoy_production = documents(
        DEPLOY["envoy-ai-gateway"] / "kserve" / "production" / "policies.yaml"
    )
    envoy_security = [doc for doc in envoy_production if doc["kind"] == "SecurityPolicy"]
    covered: list[str] = []
    for document in envoy_security:
        routes = target_names(document)
        covered.extend(routes)
        rules = document["spec"]["authorization"]["rules"]
        admitted = {
            value
            for rule in rules
            for claim in rule["principal"]["jwt"]["claims"]
            if claim["name"] == "tier"
            for value in claim["values"]
        }
        if admitted != expected_ceiling(routes):
            problems.append(
                f"Envoy production '{document['metadata']['name']}' admits {sorted(admitted)} "
                f"for {sorted(routes)}, expected {sorted(expected_ceiling(routes))}"
            )
    if sorted(covered) != sorted(all_routes):
        problems.append(
            f"Envoy production SecurityPolicies cover {sorted(covered)}, expected each of "
            f"{sorted(all_routes)} exactly once"
        )

    # Envoy attaches BBR per route section; the mock's policy names
    # HTTPRoute/kserve-mock only, so production carries its own.
    envoy_extproc = next(
        (doc for doc in envoy_production if doc["kind"] == "EnvoyExtensionPolicy"), None
    )
    if envoy_extproc is None:
        problems.append("Envoy production has no ext_proc policy for the shared chat path")
    else:
        attached = {
            (ref["name"], ref.get("sectionName"))
            for ref in envoy_extproc["spec"]["targetRefs"]
        }
        if attached != set(PRODUCTION_CHAT_SECTIONS.items()):
            problems.append(
                f"Envoy production BBR attaches to {sorted(attached)}, expected "
                f"{sorted(PRODUCTION_CHAT_SECTIONS.items())}"
            )

    kuadrant_production = documents(
        DEPLOY["kuadrant"] / "kserve" / "production" / "policies.yaml"
    )
    kuadrant_auth = [doc for doc in kuadrant_production if doc["kind"] == "AuthPolicy"]
    kuadrant_by_target = {next(iter(target_names(doc))): doc for doc in kuadrant_auth}
    if set(kuadrant_by_target) != all_routes:
        problems.append("Kuadrant production AuthPolicies do not cover every route")
    for route, document in kuadrant_by_target.items():
        rules = document["spec"]["rules"]["authorization"]
        tier = PRODUCTION_TIERS[route]
        if set(rules) != {f"{tier}-tier"}:
            problems.append(
                f"Kuadrant production route '{route}' is labelled {tier} but its rules are "
                f"{sorted(rules)}"
            )
            continue
        admitted = {
            pattern["value"]
            for pattern in rules[f"{tier}-tier"]["patternMatching"]["patterns"][0]["any"]
        }
        if admitted != TIER_CEILING[tier]:
            problems.append(
                f"Kuadrant production route '{route}' admits {sorted(admitted)}, expected "
                f"{sorted(TIER_CEILING[tier])}"
            )

    # Istio disables BBR at the virtual host and re-enables it by Envoy route
    # name. Production's sections are not in the fixture's list, so a missing
    # filter here leaves every chat request on the unheadered fallback.
    kuadrant_filter = next(
        (doc for doc in kuadrant_production if doc["kind"] == "EnvoyFilter"), None
    )
    if kuadrant_filter is None:
        problems.append("Kuadrant production has no EnvoyFilter enabling BBR on its chat sections")
    else:
        enabled = {
            patch["match"]["routeConfiguration"]["vhost"]["route"]["name"]
            for patch in kuadrant_filter["spec"]["configPatches"]
            if patch["applyTo"] == "HTTP_ROUTE"
        }
        if enabled != set(PRODUCTION_CHAT_SECTIONS.values()):
            problems.append(
                f"Kuadrant production BBR is enabled on {sorted(enabled)}, expected "
                f"{sorted(PRODUCTION_CHAT_SECTIONS.values())}"
            )

    for problem in problems:
        print(problem, file=sys.stderr)
    print(
        f"{len(expected_big_json)} big models, 1 medium model, 11 small accelerator models, "
        f"auto default {next(iter(defaults), 'missing')}, {len(all_routes)} protected "
        f"production routes ("
        + ", ".join(
            f"{len(routes_at(tier))} {tier}" for tier in ("big", "medium", "small")
        )
        + f") across {len(STACKS)} gateways, {len(problems)} problems"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
