#!/usr/bin/env python3
"""Check the semantic router configuration against the model catalog it routes to.

The router's own manifests are validated against the vendored CRD schemas like
everything else, but the routing rules are a ConfigMap: no schema covers them,
and a decision naming a model this repository does not serve fails at request
time in a cluster, as an ordinary 404 from the runtime.

This closes that gap offline. It checks that every model the router can choose
is a chat model in the mock catalog, that every decision resolves to exactly
one keyword rule, and -- the part that keeps the comparison honest -- that the
three prompts `make compare CLUSTER=<name>` sends actually select the three different models
the comparison reports.

Usage: python3 scripts/validate-router-config.py
"""
import ast
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML ships with the tooling
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
STACK_ROOTS = tuple(
    ROOT / stack / "deploy"
    for stack in ("kuadrant", "envoy-ai-gateway", "agentgateway")
)
CONFIG = STACK_ROOTS[0] / "semantic-router" / "router-config.yaml"
COMPARISON = ROOT / "compare" / "run-gateway.py"
POOL_ROUTE = STACK_ROOTS[0] / "kserve" / "pools" / "route.yaml"
KUADRANT_ROUTER = STACK_ROOTS[0] / "semantic-router" / "kuadrant-extproc.yaml"
AGENT_ROUTER = STACK_ROOTS[2] / "semantic-router" / "agentgateway-extproc.yaml"
AGENT_BBR = STACK_ROOTS[2] / "llm-d" / "agentgateway-extproc.yaml"
EXPECTED_DECISION_TIERS = {
    "big": "big",
    "medium": "medium",
    "small": "small",
}

SHARED_COMPONENTS = (
    "kserve/controller-values.yaml",
    "kserve/base/cpu-presets.yaml",
    "kserve/base/llmisvc.yaml",
    "kserve/base/route.yaml",
    "kserve/pools/route.yaml",
    "kserve/pools/kserve-b300.yaml",
    "kserve/pools/kserve-h200.yaml",
    "kserve/pools/kserve-h100.yaml",
    "kserve/pools/kserve-l40s.yaml",
    "kserve/production/models.yaml",
    "kserve/production/routes.yaml",
    "kserve/production/vllm-config.yaml",
    "llm-d/body-based-router.yaml",
    "keycloak/keycloak.yaml",
    "keycloak/route.yaml",
    "semantic-router/router-config.yaml",
    "semantic-router/semantic-router.yaml",
)

sys.path.insert(0, str(ROOT / "mock-llm"))
from server import CATALOG  # noqa: E402


def keyword_pattern(keyword):
    """Mirror the router's keyword matching: word-bounded, case-insensitive."""
    quoted = re.escape(keyword)
    if any(character.isalnum() or character == "_" for character in keyword):
        quoted = rf"\b{quoted}\b"
    return re.compile(quoted, re.IGNORECASE)


def matching_rules(prompt, rules):
    return {
        name for name, keywords in rules.items()
        if any(keyword_pattern(keyword).search(prompt) for keyword in keywords)
    }


def comparison_prompts():
    """The prompts `make compare CLUSTER=<name>` sends, read from the comparison itself.

    Validating prompts that live only in this file would prove nothing about
    the ones actually sent, so they are extracted from the script.
    """
    tree = ast.parse(COMPARISON.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "AUTO_PROMPTS"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    return []


def main():
    problems = []

    # Shared resources are intentionally repeated inside all three deployment
    # trees so each gateway can be understood in isolation. Keep those copies
    # byte-identical; gateway-specific Kustomizations and attachments are not
    # part of this list.
    for relative in SHARED_COMPONENTS:
        contents = [(root / relative).read_bytes() for root in STACK_ROOTS]
        if len(set(contents)) != 1:
            problems.append(
                f"shared component '{relative}' differs between gateway trees"
            )

    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    providers = config["providers"]
    routing = config["routing"]

    chat_models = {entry["id"] for entry in CATALOG if entry["task"] == "chat"}
    tier_by_model = {entry["id"]: entry["tier"] for entry in CATALOG}
    declared = {model["name"] for model in providers["models"]}

    for name in sorted(declared):
        if name not in chat_models:
            problems.append(f"model '{name}' is not a chat model in the mock catalog")

    families = set(providers["defaults"].get("reasoning_families", {}))
    for model in providers["models"]:
        family = model.get("reasoning_family")
        if family and family not in families:
            problems.append(
                f"model '{model['name']}' names reasoning family '{family}', "
                "which is not declared under providers.defaults.reasoning_families"
            )

    reasoning_models = {
        reference["model"]
        for decision in routing["decisions"]
        for reference in decision["modelRefs"]
        if reference.get("use_reasoning")
    }
    for model in providers["models"]:
        if model["name"] in reasoning_models and not model.get("reasoning_family"):
            problems.append(
                f"a decision asks '{model['name']}' to reason, but the model "
                "declares no reasoning_family to carry the request"
            )

    default = providers["defaults"]["default_model"]
    if default not in declared:
        problems.append(f"default_model '{default}' is not declared under providers.models")
    elif tier_by_model.get(default) != "small":
        problems.append(
            f"default_model '{default}' is {tier_by_model.get(default)}, expected small"
        )

    rules = {
        rule["name"]: rule["keywords"]
        for rule in routing.get("signals", {}).get("keywords", [])
    }
    decisions = routing["decisions"]

    priorities = {}
    referenced = set()
    for decision in decisions:
        name = decision["name"]
        priorities.setdefault(decision["priority"], []).append(name)
        for reference in decision["modelRefs"]:
            if reference["model"] not in declared:
                problems.append(
                    f"decision '{name}' selects '{reference['model']}', "
                    "which is not declared under providers.models"
                )
            expected_tier = EXPECTED_DECISION_TIERS.get(name)
            actual_tier = tier_by_model.get(reference["model"])
            if expected_tier and actual_tier != expected_tier:
                problems.append(
                    f"decision '{name}' selects {actual_tier}-tier model "
                    f"'{reference['model']}', expected {expected_tier}"
                )
        for condition in decision["rules"]["conditions"]:
            if condition["type"] != "keyword":
                # A domain condition needs the classifier models this
                # configuration deliberately does not download.
                problems.append(
                    f"decision '{name}' uses a '{condition['type']}' condition, "
                    "which the keyword-only profile cannot evaluate"
                )
                continue
            if condition["name"] not in rules:
                problems.append(
                    f"decision '{name}' matches keyword rule '{condition['name']}', "
                    "which is not defined under routing.signals.keywords"
                )
            referenced.add(condition["name"])

    for priority, names in sorted(priorities.items()):
        if len(names) > 1:
            problems.append(
                f"decisions {', '.join(sorted(names))} share priority {priority}, "
                "so which one wins is not determined by the configuration"
            )

    for name in sorted(set(rules) - referenced):
        problems.append(f"keyword rule '{name}' is not used by any decision")
    if set(EXPECTED_DECISION_TIERS) != {decision["name"] for decision in decisions}:
        problems.append(
            "semantic decisions must be named exactly big, medium, and small, "
            "and each must select a model in the matching tier"
        )

    # The comparison reports one model per prompt and reads the difference
    # between them as the routing decision. A prompt that matches nothing, or
    # matches two rules, would make that column say something untrue.
    selected = {}
    for prompt in comparison_prompts():
        matched = matching_rules(prompt, rules)
        if not matched:
            problems.append(
                f"comparison prompt {prompt!r} matches no keyword rule, so it "
                f"would be answered by the default model '{default}'"
            )
            continue
        if len(matched) > 1:
            problems.append(
                f"comparison prompt {prompt!r} matches {len(matched)} keyword "
                f"rules ({', '.join(sorted(matched))})"
            )
            continue
        rule = matched.pop()
        for decision in decisions:
            if any(condition["name"] == rule
                   for condition in decision["rules"]["conditions"]):
                selected[prompt] = decision["modelRefs"][0]["model"]

    distinct = set(selected.values())
    if selected and len(distinct) != len(selected):
        problems.append(
            "the comparison prompts do not select distinct models "
            f"({', '.join(sorted(selected.values()))}), so the routing row "
            "cannot show a decision being made"
        )

    # BBR turns the public OpenAI body.model field into the internal
    # X-Gateway-Model-Name match. Validate that every non-CPU JSON model lands
    # on the pool associated with its catalog accelerator, and that the
    # obsolete public x-model-class contract cannot creep back into the route.
    pool_route = yaml.safe_load(POOL_ROUTE.read_text(encoding="utf-8"))
    actual_pool_routes = {}
    for rule in pool_route["spec"]["rules"]:
        if not rule["name"].startswith("model-"):
            continue
        for match in rule["matches"]:
            headers = match.get("headers", [])
            if len(headers) != 1 or headers[0].get("name", "").lower() != "x-gateway-model-name":
                problems.append(f"pool rule '{rule['name']}' does not match the internal BBR model header")
                continue
            model = headers[0]["value"]
            actual_pool_routes[model] = rule["backendRefs"][0]["name"]

    expected_pool_routes = {
        entry["id"]: f"kserve-{entry['accelerator']}-inference-pool"
        for entry in CATALOG
        if entry["accelerator"] != "cpu" and entry["task"] != "transcription"
    }
    if actual_pool_routes != expected_pool_routes:
        missing = sorted(expected_pool_routes.keys() - actual_pool_routes.keys())
        extra = sorted(actual_pool_routes.keys() - expected_pool_routes.keys())
        wrong = sorted(
            model for model in expected_pool_routes.keys() & actual_pool_routes.keys()
            if expected_pool_routes[model] != actual_pool_routes[model]
        )
        if missing:
            problems.append(f"pool route is missing body.model mappings: {', '.join(missing)}")
        if extra:
            problems.append(f"pool route has unknown body.model mappings: {', '.join(extra)}")
        for model in wrong:
            problems.append(
                f"body.model '{model}' routes to '{actual_pool_routes[model]}', "
                f"expected '{expected_pool_routes[model]}'"
            )

    route_text = POOL_ROUTE.read_text(encoding="utf-8").lower()
    if "x-model-class" in route_text:
        problems.append("pool route still exposes the obsolete x-model-class contract")

    # Istio chooses a route before listener-level ext_proc filters run. A
    # forged internal header can therefore select any model-specific chat
    # section on the first pass. Semantic routing must be enabled on every one
    # of those sections so model:auto is resolved before BBR overwrites the
    # header and recomputes the route.
    chat_route_names = {
        rule["name"]
        for rule in pool_route["spec"]["rules"]
        if rule["name"] == "chat"
        or (
            rule["name"].startswith("model-")
            and any(
                match.get("path", {}).get("value") == "/v1/chat/completions"
                for match in rule["matches"]
            )
        )
    }
    kuadrant_documents = list(
        yaml.safe_load_all(KUADRANT_ROUTER.read_text(encoding="utf-8"))
    )
    kuadrant_filter = next(
        document
        for document in kuadrant_documents
        if document.get("kind") == "EnvoyFilter"
    )
    semantic_route_names = set()
    for patch in kuadrant_filter["spec"]["configPatches"]:
        if patch.get("applyTo") != "HTTP_ROUTE":
            continue
        filter_config = (
            patch.get("patch", {})
            .get("value", {})
            .get("typed_per_filter_config", {})
            .get("ai.gateway.semantic_router", {})
        )
        if "overrides" not in filter_config or filter_config.get("disabled") is True:
            continue
        semantic_route_names.add(
            patch.get("match", {})
            .get("routeConfiguration", {})
            .get("vhost", {})
            .get("route", {})
            .get("name")
        )
    missing_semantic_routes = sorted(chat_route_names - semantic_route_names)
    if missing_semantic_routes:
        problems.append(
            "Kuadrant semantic routing is missing chat route sections: "
            + ", ".join(missing_semantic_routes)
        )

    # agentgateway has one ext_proc slot per phase per target, so the semantic
    # attachment has to take over the pre-routing slot and hand its choice to
    # the router table through a header. A route-attached router still picks
    # the right model and is served from whatever pool the pre-routing header
    # already selected -- measured as `auto_pools: all / all / all` while every
    # other stack reported the accelerator pools. None of that fails loudly, so
    # it is checked here.
    agent_policies = [
        document
        for document in yaml.safe_load_all(AGENT_ROUTER.read_text(encoding="utf-8"))
        if document and document.get("kind") == "AgentgatewayPolicy"
    ]
    base_names = {
        document["metadata"]["name"]
        for document in yaml.safe_load_all(AGENT_BBR.read_text(encoding="utf-8"))
        if document and document.get("kind") == "AgentgatewayPolicy"
    }
    if len(agent_policies) != 1:
        problems.append(
            "agentgateway semantic attachment must be exactly one policy; "
            f"found {len(agent_policies)}. Two policies setting traffic.extProc "
            "merge field-level, and the loser is dropped without a status condition"
        )
    for policy in agent_policies:
        traffic = policy.get("spec", {}).get("traffic", {})
        name = policy["metadata"]["name"]
        if name not in base_names:
            problems.append(
                f"agentgateway semantic policy '{name}' does not replace the base "
                f"body-based-router policy ({', '.join(sorted(base_names))}); both "
                "would attach and only one would run"
            )
        if traffic.get("phase") != "PreRouting":
            problems.append(
                f"agentgateway semantic policy '{name}' must set phase: PreRouting, "
                "or its model choice lands after the route is already selected"
            )
        if any(
            ref.get("kind") != "Gateway"
            for ref in policy.get("spec", {}).get("targetRefs", [])
        ):
            problems.append(
                f"agentgateway semantic policy '{name}' must target the Gateway; "
                "PreRouting policies may only target a Gateway or Listener"
            )
        written = {
            header.get("name")
            for arm in traffic.get("transformation", {}).get("conditional", [])
            for header in arm.get("policy", {}).get("request", {}).get("set", [])
        }
        if "x-gateway-model-name" not in written:
            problems.append(
                f"agentgateway semantic policy '{name}' must set x-gateway-model-name "
                "from the router's x-selected-model; without it the pool rules "
                "never match and every chat request falls back to the CPU fixture"
            )
        if not traffic.get("transformation", {}).get("conditional"):
            problems.append(
                f"agentgateway semantic policy '{name}' must keep the transformation "
                "conditional; an unconditional rule wipes the header BBR writes for "
                "embeddings and reranking"
            )
        # Every arm that writes the routing header has to be scoped to chat.
        # x-selected-model is a header the router sets, but a client can send
        # it too and BBR does not strip it on the task paths, so an unscoped
        # arm copies a client value over BBR's and the pool rules match it.
        for arm in traffic.get("transformation", {}).get("conditional", []):
            writes_router_header = any(
                header.get("name") == "x-gateway-model-name"
                for header in arm.get("policy", {}).get("request", {}).get("set", [])
            )
            condition = arm.get("condition", "")
            if writes_router_header and "/v1/chat/completions" not in condition:
                problems.append(
                    f"agentgateway semantic policy '{name}' sets x-gateway-model-name "
                    f"under a condition that is not scoped to the chat path "
                    f"({condition!r}); on /v1/embeddings and /v1/rerank that overwrites "
                    "the header BBR derived from the body with one the client sent"
                )

    for problem in problems:
        print(problem, file=sys.stderr)
    for prompt, model in selected.items():
        print(f"{prompt!r} -> {model}")
    print(f"{len(SHARED_COMPONENTS)} synchronized shared manifests, "
          f"{len(declared)} semantic models, {len(actual_pool_routes)} body.model routes, "
          f"{len(decisions)} decisions, {len(selected)} comparison prompts, "
          f"{len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
