#!/usr/bin/env python3
"""Check the semantic router configuration against the model catalog it routes to.

The router's own manifests are validated against the vendored CRD schemas like
everything else, but the routing rules are a ConfigMap: no schema covers them,
and a decision naming a model this repository does not serve fails at request
time in a cluster, as an ordinary 404 from the runtime.

This closes that gap offline. It checks that every model the router can choose
is a chat model in the mock catalog, that every decision resolves to exactly
one keyword rule, and -- the part that keeps the comparison honest -- that the
three prompts `make compare` sends actually select the three different models
the comparison reports.

Usage: python3 scripts/validate-router-config.py
"""
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML ships with the tooling
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "semantic-router" / "config" / "router-config.yaml"
COMPARISON = ROOT / "compare" / "run-comparison.sh"

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
    """The prompts `make compare` sends, read from the comparison itself.

    Validating prompts that live only in this file would prove nothing about
    the ones actually sent, so they are extracted from the script.
    """
    text = COMPARISON.read_text(encoding="utf-8")
    prompts = re.findall(r"""auto_model "\$base" '([^']+)'""", text)
    body = re.search(r"^AUTO_BODY='(.+)'$", text, re.MULTILINE)
    if body:
        prompts.append(yaml.safe_load(body.group(1))["messages"][0]["content"])
    return prompts


def main():
    problems = []
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

    for problem in problems:
        print(problem, file=sys.stderr)
    for prompt, model in selected.items():
        print(f"{prompt!r} -> {model}")
    print(f"{len(declared)} routable models, {len(decisions)} decisions, "
          f"{len(selected)} comparison prompts, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
