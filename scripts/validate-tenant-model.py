#!/usr/bin/env python3
"""Validate org/team rate-limit probes without mixing tenancy into model access."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
REALM = ROOT / "keycloak" / "realm" / "ai-gateway-realm.json"
HARNESS = ROOT / "compare" / "run-gateway.py"
ENVOY_LIMITS = ROOT / "envoy-ai-gateway" / "deploy" / "policies" / "rate-limit.yaml"
KUADRANT_LIMITS = ROOT / "kuadrant" / "deploy" / "policies" / "rate-limit-policy.yaml"
PROBE_HEADER = "x-tenant-probe"
ORG_HEADER = "x-org-id"
TEAM_HEADER = "x-team-id"


def harness_constants() -> dict:
    tree = ast.parse(HARNESS.read_text())
    wanted = {"TENANT_ORG_CAP", "TENANT_TEAM_CAP", "TENANT_USERS"}
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                found[target.id] = ast.literal_eval(node.value)
    return found


def documents(path: Path) -> list[dict]:
    return [document for document in yaml.safe_load_all(path.read_text()) if document]


def group_paths(groups: list[dict], prefix: str = "") -> set[str]:
    paths = set()
    for group in groups:
        path = f"{prefix}/{group['name']}"
        paths.add(path)
        paths |= group_paths(group.get("subGroups", []), path)
    return paths


def tenant_rules(policy: dict) -> dict:
    found = {}
    for rule in policy["spec"]["rateLimit"]["global"]["rules"]:
        names = {
            header["name"]
            for selector in rule.get("clientSelectors", [])
            for header in selector.get("headers", [])
        }
        if PROBE_HEADER not in names:
            continue
        if TEAM_HEADER in names:
            found["team"] = rule
        elif ORG_HEADER in names:
            found["org"] = rule
    return found


def main() -> int:
    problems: list[str] = []
    realm = json.loads(REALM.read_text())
    constants = harness_constants()
    missing = {"TENANT_ORG_CAP", "TENANT_TEAM_CAP", "TENANT_USERS"} - set(constants)
    if missing:
        print(f"run-gateway.py is missing {', '.join(sorted(missing))}", file=sys.stderr)
        return 1

    org_cap = constants["TENANT_ORG_CAP"]
    team_cap = constants["TENANT_TEAM_CAP"]
    users = constants["TENANT_USERS"]
    if not team_cap < org_cap <= 2 * team_cap - 1:
        problems.append(
            f"tenant caps must satisfy team < org <= 2*team-1; got org={org_cap}, team={team_cap}"
        )
    elif org_cap < team_cap + 2:
        problems.append(
            f"org cap {org_cap} leaves no unambiguous nested-bucket probe above team cap {team_cap}"
        )

    known_paths = group_paths(realm.get("groups", []))
    by_name = {user["username"]: user for user in realm.get("users", [])}
    tenancy = {}
    for role, username in users.items():
        user = by_name.get(username)
        if not user:
            problems.append(f"probe user '{username}' ({role}) is not declared in the realm")
            continue
        if not any(
            credential.get("type") == "password" and credential.get("value") == username
            for credential in user.get("credentials", [])
        ):
            problems.append(f"probe user '{username}' needs a password equal to its username")
        attributes = user.get("attributes", {})
        org = (attributes.get("org") or [None])[0]
        team = (attributes.get("team") or [None])[0]
        tier = (attributes.get("tier") or [None])[0]
        if not org or not team:
            problems.append(f"probe user '{username}' is missing org or team")
        if tier not in {"big", "medium", "small"}:
            problems.append(f"probe user '{username}' has invalid tier ceiling {tier!r}")
        tenancy[role] = (org, team)
        for path in user.get("groups", []):
            if path not in known_paths:
                problems.append(f"probe user '{username}' joins unknown group '{path}'")

    if len(tenancy) == len(users):
        (org_a, team_a), (org_b, team_b) = tenancy["team_a"], tenancy["team_b"]
        other_org, _ = tenancy["other_org"]
        if org_a != org_b:
            problems.append("tenant probe teams must share an org")
        if team_a == team_b:
            problems.append("tenant probe users must belong to different teams")
        if other_org == org_a:
            problems.append("cross-org probe user must belong to another org")

    mappers = {
        mapper.get("config", {}).get("claim.name")
        for scope in realm.get("clientScopes", [])
        for mapper in scope.get("protocolMappers", [])
    }
    for claim in ("tier", "org", "team", "group_paths"):
        if claim not in mappers:
            problems.append(f"the realm emits no '{claim}' claim")

    envoy_policy = documents(ENVOY_LIMITS)[0]
    envoy_rules = tenant_rules(envoy_policy)
    has_cost = any("cost" in rule for rule in envoy_policy["spec"]["rateLimit"]["global"]["rules"])
    for level, expected in (("org", org_cap), ("team", team_cap)):
        rule = envoy_rules.get(level)
        if not rule:
            problems.append(f"Envoy has no {level}-level tenant rule")
            continue
        if rule["limit"]["requests"] != expected:
            problems.append(f"Envoy {level} cap differs from harness cap {expected}")
        if rule.get("shared") and has_cost:
            problems.append(f"Envoy {level} rule cannot be shared while response cost is enabled")

    kuadrant_limits = documents(KUADRANT_LIMITS)[0]["spec"]["limits"]
    for name, expected in (("tenant-org-probe", org_cap), ("tenant-team-probe", team_cap)):
        limit = kuadrant_limits.get(name)
        if not limit:
            problems.append(f"Kuadrant has no '{name}' limit")
            continue
        if limit["rates"][0]["limit"] != expected:
            problems.append(f"Kuadrant '{name}' cap differs from harness cap {expected}")
        if not limit.get("counters"):
            problems.append(f"Kuadrant '{name}' has no tenant counter")
        for counter in limit.get("counters", []):
            if '"' in counter.get("expression", ""):
                problems.append(f"Kuadrant '{name}' counter contains an unsafe double quote")

    for problem in problems:
        print(problem, file=sys.stderr)
    print(
        f"{len(users)} tenant probe users, org cap {org_cap}, team cap {team_cap}, "
        f"tier-independent tenancy, {len(problems)} problems"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
