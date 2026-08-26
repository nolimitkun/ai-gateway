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
    "kserve/production/kustomization.yaml",
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
        match = rule["matches"][0]
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
            and rule["matches"][0].get("path", {}).get("value")
            == "/v1/chat/completions"
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
