#!/usr/bin/env python3
"""Check the native body-model routing overlays offline.

Two of the three stacks can route `body.model` to an accelerator pool without
the external body-based router the third one needs. The overlays that show
this run beside the BBR path rather than replacing it, which is what makes
them safe to install -- and also what makes them easy to break quietly: the
comparison sends the same request down both paths and reports what each did,
so an overlay that drifted from the BBR route reads as a gateway difference
rather than as an editing mistake.

So this validates what the rows assume and cannot check for themselves:

  * both overlays map every model to the same pool the BBR route does, or the
    two paths are not comparable and a "wrong pool" row means nothing;
  * the native hostname is authenticated, because a policy attaches to a route
    or a listener by name and the native path has new names -- the same trap
    the AIGatewayRoute for token accounting already hit once, when it reached
    the InferencePool with no token at all;
  * the native tier rules read a header the native path actually writes.
    Copying the BBR rule verbatim leaves it gating on X-Gateway-Model-Name,
    which nothing sets here, so every request would fall through to the
    small-tier Allow and the ceiling would stop binding;
  * both stacks restrict the same models the BBR path does, at the same tier.
    The ceiling is hierarchical -- a medium model admits big and medium -- so
    a rule that names the wrong tier is a silent over- or under-grant;
  * authorization reads the scalar tier claim and nothing else, since org and
    team are tenancy metadata and cannot grant model access;
  * the agentgateway models parent to the dedicated listener, since a model
    attached to the whole Gateway would take over the BBR path too;
  * the hostname is the one the harness sends.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared in requirements-dev.txt
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "compare" / "run-gateway.py"
ENVOY = ROOT / "envoy-ai-gateway" / "deploy"
AGENT = ROOT / "agentgateway" / "deploy"

BBR_HEADER = "x-gateway-model-name"
NATIVE_HEADER = "x-ai-eg-model"
LISTENER = "native"
# AIGatewayRoute.spec.rules carries maxItems: 15 in the vendored CRD.
MAX_AI_RULES = 15


def documents(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def harness_host() -> str:
    """Read the hostname the comparison sends, without importing the harness."""
    tree = ast.parse(HARNESS.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "NATIVE_HOST" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    return ""


def bbr_mapping(stack: Path) -> dict[str, str]:
    """model -> InferencePool, as the body-based router path expresses it."""
    mapping = {}
    for doc in documents(stack / "kserve" / "pools" / "route.yaml"):
        for rule in doc.get("spec", {}).get("rules", []):
            for match in rule.get("matches", []):
                for header in match.get("headers", []):
                    if header.get("name") == BBR_HEADER:
                        mapping[header["value"]] = rule["backendRefs"][0]["name"]
    return mapping


def envoy_native(path: Path) -> tuple[dict[str, str], list[dict], str]:
    """model -> InferencePool from the AIGatewayRoute, plus its rules and host."""
    mapping, rules, hostnames = {}, [], []
    for doc in documents(path):
        if doc.get("kind") != "AIGatewayRoute":
            continue
        hostnames = doc["spec"].get("hostnames", [])
        rules = doc["spec"].get("rules", [])
        for rule in rules:
            for match in rule.get("matches", []):
                for header in match.get("headers", []):
                    if header.get("name") == NATIVE_HEADER and header.get("type") == "Exact":
                        mapping[header["value"]] = rule["backendRefs"][0]["name"]
    return mapping, rules, hostnames[0] if hostnames else ""


def agent_native(path: Path) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """model -> pool, model -> parent listener, and the models carrying a rule."""
    mapping, listeners, restricted = {}, {}, set()
    for doc in documents(path):
        if doc.get("kind") != "AgentgatewayModel":
            continue
        spec = doc["spec"]
        name = spec.get("match", {}).get("model") or doc["metadata"]["name"]
        mapping[name] = spec["custom"]["backendRef"]["name"]
        sections = {ref.get("sectionName", "") for ref in spec.get("parentRefs", [])}
        listeners[name] = ",".join(sorted(sections))
        if "authorization" in spec.get("policies", {}):
            restricted.add(name)
    return mapping, listeners, restricted


def deny_models(path: Path, header: str) -> set[str]:
    """Models a SecurityPolicy denies to non-admins, read off the Deny rule."""
    denied = set()
    for doc in documents(path):
        if doc.get("kind") != "SecurityPolicy":
            continue
        for rule in doc["spec"].get("authorization", {}).get("rules", []):
            if rule.get("action") != "Deny":
                continue
            for entry in rule.get("principal", {}).get("headers", []):
                if entry.get("name") == header:
                    denied.update(entry.get("values", []))
    return denied


def tier_classes(path: Path, header: str) -> dict[str, tuple[str, ...]]:
    """model -> the tiers its Allow rule admits, read off a SecurityPolicy.

    The BBR policy is the reference: whatever ceiling it puts a model behind is
    the one the native path has to reproduce, or the same token is judged
    differently depending on which hostname it used.
    """
    classes: dict[str, tuple[str, ...]] = {}
    for doc in documents(path):
        if doc.get("kind") != "SecurityPolicy":
            continue
        for rule in doc["spec"].get("authorization", {}).get("rules", []):
            if rule.get("action") != "Allow":
                continue
            principal = rule.get("principal", {})
            tiers = tuple(
                value
                for claim in principal.get("jwt", {}).get("claims", [])
                if claim.get("name") == "tier"
                for value in claim.get("values", [])
            )
            for entry in principal.get("headers", []):
                if entry.get("name") == header:
                    for model in entry.get("values", []):
                        classes[model] = tiers
    return classes


def agent_tiers(path: Path, model: str) -> set[str]:
    """The tiers an AgentgatewayModel's own authorization rule names."""
    named = set()
    for doc in documents(path):
        if doc.get("metadata", {}).get("name") != model:
            continue
        rule = doc["spec"].get("policies", {}).get("authorization", {})
        for expression in rule.get("policy", {}).get("matchExpressions", []):
            named |= set(re.findall(r'"(big|medium|small)"', expression))
    return named


def envoy_native_binding(path: Path) -> tuple[bool, set[str], set[str]]:
    """Does the native policy authenticate, and what do its model rules bind?"""
    authenticated, claims, headers = False, set(), set()
    for doc in documents(path):
        if doc.get("kind") != "SecurityPolicy":
            continue
        if doc["spec"].get("jwt", {}).get("providers"):
            authenticated = True
        for rule in doc["spec"].get("authorization", {}).get("rules", []):
            if rule.get("action") != "Allow":
                continue
            principal = rule.get("principal", {})
            entry_headers = {h.get("name") for h in principal.get("headers", [])}
            if not entry_headers:
                continue
            headers |= entry_headers
            claims |= {c.get("name") for c in principal.get("jwt", {}).get("claims", [])}
    return authenticated, claims, headers


def main() -> int:
    problems: list[str] = []
    host = harness_host()
    if not host:
        problems.append(
            "compare/run-gateway.py defines no NATIVE_HOST; the overlays serve a "
            "hostname nothing in the comparison sends"
        )

    envoy_map, envoy_rules, envoy_host = envoy_native(ENVOY / "native-routing" / "ai-route.yaml")
    agent_map, agent_listeners, agent_restricted = agent_native(
        AGENT / "native-routing" / "models.yaml"
    )

    for stack, native in (("Envoy Gateway", envoy_map), ("agentgateway", agent_map)):
        expected = bbr_mapping(ENVOY if stack == "Envoy Gateway" else AGENT)
        for model, pool in sorted(expected.items()):
            if model not in native:
                problems.append(
                    f"{stack}: the BBR route sends '{model}' to {pool}, and the "
                    f"native overlay does not route it at all -- the comparison "
                    f"would read the fallback pool as a native routing failure"
                )
            elif native[model] != pool:
                problems.append(
                    f"{stack}: '{model}' goes to {pool} on the BBR path and "
                    f"{native[model]} natively; the two paths are not comparable"
                )
        for model in sorted(set(native) - set(expected)):
            problems.append(
                f"{stack}: the native overlay routes '{model}', which the BBR "
                f"route does not; the paths have drifted"
            )

    if len(envoy_rules) > MAX_AI_RULES:
        problems.append(
            f"the AIGatewayRoute carries {len(envoy_rules)} rules and the CRD caps "
            f"spec.rules at maxItems: {MAX_AI_RULES}"
        )
    if not any(
        header.get("type") == "RegularExpression"
        for rule in envoy_rules
        for match in rule.get("matches", [])
        for header in match.get("headers", [])
    ):
        problems.append(
            "the AIGatewayRoute has no catch-all rule; a model outside the twelve "
            "would 404 natively while the BBR path serves it from the CPU fixture"
        )
    charges_tokens = any(
        "llmRequestCosts" in doc.get("spec", {})
        for doc in documents(ENVOY / "native-routing" / "ai-route.yaml")
    )
    if charges_tokens:
        problems.append(
            "the native AIGatewayRoute declares llmRequestCosts. The Gateway's "
            "BackendTrafficPolicy charges those tokens and keeps its counters "
            "per route, so this hostname would get its own 100-tokens-a-minute "
            "budget and the routing probes would 429 themselves -- every native "
            "row would report a rate limit instead of where a request was routed"
        )
    if host and envoy_host != host:
        problems.append(
            f"the AIGatewayRoute serves '{envoy_host}' and the comparison sends "
            f"'{host}'"
        )

    authenticated, claims, headers = envoy_native_binding(
        ENVOY / "native-routing" / "security-policy.yaml"
    )
    if not authenticated:
        problems.append(
            "no SecurityPolicy with a JWT provider targets the native Envoy route; "
            "the native hostname would reach the InferencePool with no token"
        )
    if BBR_HEADER in headers:
        problems.append(
            f"the native Envoy tier rules gate on '{BBR_HEADER}', which no "
            f"filter writes on this path -- every request would fall through to "
            f"the small-tier Allow and the ceiling would stop binding"
        )
    if NATIVE_HEADER not in headers:
        problems.append(
            f"the native Envoy tier rules never read '{NATIVE_HEADER}', so they "
            f"do not distinguish a model tier at all"
        )
    if claims and "tier" not in claims:
        problems.append(
            f"the native Envoy model rules bind {sorted(claims)} rather than the "
            f"scalar tier claim; model access would not follow the ceiling"
        )
    if claims & {"org", "team", "groups", "plan"}:
        problems.append(
            f"the native Envoy model rules read {sorted(claims & {'org', 'team', 'groups', 'plan'})}; "
            f"org and team are tenancy metadata and cannot grant model access"
        )

    bbr_denied = deny_models(ENVOY / "policies" / "security-policy.yaml", BBR_HEADER)
    native_denied = deny_models(
        ENVOY / "native-routing" / "security-policy.yaml", NATIVE_HEADER
    )
    if bbr_denied != native_denied:
        problems.append(
            f"the BBR path restricts {sorted(bbr_denied)} and the native path "
            f"{sorted(native_denied)}; the same request would be judged differently "
            f"depending on which hostname it used"
        )
    if bbr_denied != agent_restricted:
        problems.append(
            f"agentgateway attaches a per-model rule to {sorted(agent_restricted)} "
            f"while the restricted class is {sorted(bbr_denied)}"
        )

    bbr_classes = tier_classes(ENVOY / "policies" / "security-policy.yaml", BBR_HEADER)
    native_classes = tier_classes(
        ENVOY / "native-routing" / "security-policy.yaml", NATIVE_HEADER
    )
    for model, tiers in sorted(bbr_classes.items()):
        if native_classes.get(model) != tiers:
            problems.append(
                f"the BBR path admits '{model}' to {list(tiers)} and the native path "
                f"to {list(native_classes.get(model, ()))}; the ceiling differs by "
                f"hostname"
            )
    for model in sorted(agent_restricted):
        named = agent_tiers(AGENT / "native-routing" / "models.yaml", model)
        expected = set(bbr_classes.get(model, ()))
        if named != expected:
            problems.append(
                f"the agentgateway rule on '{model}' admits {sorted(named)} and the "
                f"BBR path admits {sorted(expected)}. A ceiling is hierarchical, so "
                f"naming one tier too few denies a caller who should reach it and "
                f"one too many is a silent grant"
            )

    listeners = {name for doc in documents(AGENT / "native-routing" / "gateway.yaml")
                 if doc.get("kind") == "Gateway"
                 for name in [l["name"] for l in doc["spec"]["listeners"]]}
    if LISTENER not in listeners:
        problems.append(
            f"the agentgateway overlay defines no '{LISTENER}' listener, so its "
            f"models attach to nothing"
        )
    for doc in documents(AGENT / "native-routing" / "gateway.yaml"):
        if doc.get("kind") != "Gateway":
            continue
        for listener in doc["spec"]["listeners"]:
            if listener["name"] == LISTENER and listener.get("hostname") != host:
                answered = listener.get("hostname")
                problems.append(
                    f"the '{LISTENER}' listener answers for '{answered}' and the "
                    f"comparison sends '{host}'"
                )
    for model, section in sorted(agent_listeners.items()):
        if section != LISTENER:
            where = section or "the whole Gateway"
            problems.append(
                f"agentgateway model '{model}' parents to {where} rather than the "
                f"'{LISTENER}' listener; it would take over the BBR path as well"
            )

    jwt_listeners = {
        ref.get("sectionName")
        for doc in documents(AGENT / "native-routing" / "policies.yaml")
        if doc.get("spec", {}).get("traffic", {}).get("jwtAuthentication")
        for ref in doc["spec"].get("targetRefs", [])
    }
    if LISTENER not in jwt_listeners:
        problems.append(
            f"no agentgateway policy authenticates the '{LISTENER}' listener; the "
            f"native hostname would serve the pools with no token"
        )

    for problem in problems:
        print(problem, file=sys.stderr)
    print(
        f"{len(envoy_map)} Envoy and {len(agent_map)} agentgateway native model "
        f"routes on '{host}', {len(agent_restricted)} above small tier, {len(problems)} problems"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
