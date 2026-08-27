#!/usr/bin/env python3
"""Check the org/team tenancy contract offline.

The nested-bucket comparison row is a classifier, not a threshold: it decides
between "nested", "shared" and "unenforced" by *which request number* returns
429. That makes it unusually easy to break silently. Edit a cap in one policy
file and the gateway still behaves correctly, but the row relabels it
"unexpected"; rename a probe user and the row reports a token error that looks
like a broken cluster. Neither failure points at the edit that caused it.

So this validates the things the row assumes and cannot check for itself:

  * the probe users exist, are password-grantable, and carry org/team;
  * two of them share an org and the third does not, or "cross-org isolation"
    proves nothing;
  * the caps in each stack's policy agree with the caps the harness classifies
    against, and with each other, or the two columns are not comparable;
  * the caps are far enough apart for the three outcomes to be distinguishable;
  * `shared: true` is *not* set on the Envoy tenant rules while that policy
    also carries a response-cost rule, because setting it silently disables
    the token budget (see the policy comment for the measurement);
  * no Kuadrant counter expression contains a double quote, which would make
    Limitador reject the whole limit file and crash-loop;
  * the entitled team named in all three authorization policies is a team that
    exists and that the probe user belongs to.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared in requirements-dev.txt
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
REALM = ROOT / "keycloak" / "realm" / "ai-gateway-realm.json"
HARNESS = ROOT / "compare" / "run-gateway.py"
ENVOY_LIMITS = ROOT / "envoy-ai-gateway" / "deploy" / "policies" / "rate-limit.yaml"
ENVOY_SECURITY = ROOT / "envoy-ai-gateway" / "deploy" / "policies" / "security-policy.yaml"
KUADRANT_LIMITS = ROOT / "kuadrant" / "deploy" / "policies" / "rate-limit-policy.yaml"
KUADRANT_AUTH = ROOT / "kuadrant" / "deploy" / "policies" / "auth-policy.yaml"
AGENT_AUTH = ROOT / "agentgateway" / "deploy" / "policies" / "auth-policy.yaml"

PROBE_HEADER = "x-tenant-probe"
ORG_HEADER = "x-org-id"
TEAM_HEADER = "x-team-id"


def harness_constants() -> dict:
    """Read the harness caps without importing it (it shells out to kubectl)."""
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
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def group_paths(groups: list[dict], prefix: str = "") -> set[str]:
    paths = set()
    for group in groups:
        path = f"{prefix}/{group['name']}"
        paths.add(path)
        paths |= group_paths(group.get("subGroups", []), path)
    return paths


def tenant_rules(policy: dict) -> dict:
    """Return the Envoy tenant rules keyed by the identity headers they select."""
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


def kuadrant_binds_org(path: Path, org: str, team: str) -> list[str]:
    """Kuadrant rules that match the entitled team without also matching its org.

    Authorino ANDs the entries of `patterns` and ORs the entries of an `any`,
    so the team test lives inside an `any` (the other arm being the admin
    one). The org has to be bound *inside that same arm* -- as an `all` -- and
    not hoisted up to `patterns`, which would AND it with the admin arm and
    lock admins out, since they carry no org claim.
    """
    unbound = []
    for document in documents(path):
        rules = document.get("spec", {}).get("rules", {}).get("authorization", {})
        for name, rule in rules.items():
            arms = rule.get("patternMatching", {}).get("patterns", [])
            for arm in arms:
                for branch in arm.get("any", []) or [arm]:
                    tests = branch.get("all", [branch])
                    selectors = {
                        (t.get("selector"), str(t.get("value")))
                        for t in tests
                        if isinstance(t, dict)
                    }
                    if ("auth.identity.team", team) not in selectors:
                        continue
                    if ("auth.identity.org", org) not in selectors:
                        unbound.append(name)
    return unbound


def envoy_binds_org(path: Path, org: str, team: str) -> list[str]:
    """Envoy rules whose JWT claims name the entitled team but not its org."""
    unbound = []
    for document in documents(path):
        rules = document.get("spec", {}).get("authorization", {}).get("rules", [])
        for rule in rules:
            claims = rule.get("principal", {}).get("jwt", {}).get("claims", [])
            named = {claim["name"]: claim.get("values", []) for claim in claims}
            if team not in named.get("team", []):
                continue
            if org not in named.get("org", []):
                unbound.append(rule.get("name", "<unnamed>"))
    return unbound


def agent_binds_org(path: Path, org: str, team: str) -> list[str]:
    """agentgateway rules naming the entitled team outside its group path.

    Authorization here is CEL over the verified claims, so there is no
    structure to walk. The binding *is* the path: `/acme/research` names both
    halves at once, and a bare `research` would not.
    """
    unbound = []
    for document in documents(path):
        authorization = (
            document.get("spec", {}).get("traffic", {}).get("authorization", {})
        )
        for expression in authorization.get("policy", {}).get("matchExpressions", []):
            if team not in expression:
                continue
            if f"/{org}/{team}" not in expression:
                unbound.append(document.get("metadata", {}).get("name", "<unnamed>"))
    return unbound


def main() -> int:
    problems: list[str] = []
    realm = json.loads(REALM.read_text())
    constants = harness_constants()

    missing_constants = {"TENANT_ORG_CAP", "TENANT_TEAM_CAP", "TENANT_USERS"} - set(constants)
    if missing_constants:
        print(f"run-gateway.py is missing {', '.join(sorted(missing_constants))}", file=sys.stderr)
        return 1
    org_cap = constants["TENANT_ORG_CAP"]
    team_cap = constants["TENANT_TEAM_CAP"]
    users = constants["TENANT_USERS"]

    # The outcomes are told apart by whether team B stops before its own cap,
    # so the caps have to leave room for that to be unambiguous under either
    # way a limit service can account for a rejected request. Envoy's
    # increments every matching descriptor even when one is already over, so
    # team A's rejected request still spends org budget; Limitador need not.
    # Team B therefore starts against an org bucket already holding either
    # team_cap or team_cap + 1, and it must stop strictly after its first
    # request (or the row reads "shared") and strictly before its own cap (or
    # the row cannot tell a nested org bucket from an absent one).
    if not team_cap < org_cap:
        problems.append(f"team cap {team_cap} must be below the org cap {org_cap}")
    elif org_cap > 2 * team_cap - 1:
        problems.append(
            f"org cap {org_cap} is too high against a team cap of {team_cap}: team B would "
            f"reach its own limit before the shared org limit, so the row could not tell "
            f"a nested org bucket from an absent one (need at most {2 * team_cap - 1})"
        )
    elif org_cap < team_cap + 2:
        problems.append(
            f"org cap {org_cap} is too low against a team cap of {team_cap}: team B's very "
            f"first request could exhaust the org bucket, which the row cannot tell apart "
            f"from one bucket shared by every tenant (need at least {team_cap + 2})"
        )

    known_paths = group_paths(realm.get("groups", []))
    by_name = {user["username"]: user for user in realm.get("users", [])}
    tenancy = {}
    for role, username in users.items():
        user = by_name.get(username)
        if user is None:
            problems.append(f"probe user '{username}' ({role}) is not declared in the realm")
            continue
        # The harness requests tokens with username == password.
        if not any(c.get("type") == "password" and c.get("value") == username
                   for c in user.get("credentials", [])):
            problems.append(f"probe user '{username}' needs a password credential equal to its username")
        attributes = user.get("attributes", {})
        org = (attributes.get("org") or [None])[0]
        team = (attributes.get("team") or [None])[0]
        if not org or not team:
            problems.append(f"probe user '{username}' is missing an org or team attribute")
        tenancy[role] = (org, team)
        for path in user.get("groups", []):
            if path not in known_paths:
                problems.append(f"probe user '{username}' joins '{path}', which is not a realm group")

    if len(tenancy) == len(users):
        (org_a, team_a), (org_b, team_b) = tenancy["team_a"], tenancy["team_b"]
        other_org, _ = tenancy["other_org"]
        if org_a != org_b:
            problems.append(
                f"'{users['team_a']}' and '{users['team_b']}' are in different orgs "
                f"({org_a}/{org_b}); the shared org ceiling cannot be observed"
            )
        if team_a == team_b:
            problems.append(
                f"'{users['team_a']}' and '{users['team_b']}' share team '{team_a}'; "
                f"team isolation cannot be observed"
            )
        if other_org == org_a:
            problems.append(
                f"'{users['other_org']}' is in org '{other_org}' too; "
                f"cross-org isolation cannot be observed"
            )

    mappers = {
        mapper.get("config", {}).get("claim.name")
        for scope in realm.get("clientScopes", [])
        for mapper in scope.get("protocolMappers", [])
    }
    for claim in ("org", "team", "group_paths"):
        if claim not in mappers:
            problems.append(f"the realm emits no '{claim}' claim, which the tenant policies read")

    envoy_policy = next(d for d in documents(ENVOY_LIMITS))
    envoy_rules = tenant_rules(envoy_policy)
    envoy_has_cost_rule = any(
        "cost" in rule
        for rule in envoy_policy["spec"]["rateLimit"]["global"]["rules"]
    )
    for level, expected in (("org", org_cap), ("team", team_cap)):
        rule = envoy_rules.get(level)
        if rule is None:
            problems.append(f"the Envoy rate limit policy has no {level}-level tenant rule")
            continue
        actual = rule["limit"]["requests"]
        if actual != expected:
            problems.append(
                f"Envoy {level} cap is {actual} but the harness classifies against {expected}"
            )
        if rule.get("shared") and envoy_has_cost_rule:
            problems.append(
                f"Envoy {level} tenant rule must not set 'shared: true' while this policy "
                f"also charges a response cost: the flag makes Envoy Gateway emit a second "
                f"rate limit domain, the apply_on_stream_done call lands on the domain with "
                f"no limits registered, and the token budget stops being enforced with no "
                f"visible symptom. The per-route ceiling this trades away is reported by the "
                f"tenant_route_scope comparison row"
            )

    kuadrant_limits = next(d for d in documents(KUADRANT_LIMITS))["spec"]["limits"]
    for name, expected, level in (
        ("tenant-org-probe", org_cap, "org"),
        ("tenant-team-probe", team_cap, "team"),
    ):
        limit = kuadrant_limits.get(name)
        if limit is None:
            problems.append(f"the Kuadrant rate limit policy has no '{name}' limit")
            continue
        actual = limit["rates"][0]["limit"]
        if actual != expected:
            problems.append(
                f"Kuadrant {level} cap is {actual} but Envoy and the harness use {expected}; "
                f"the two columns would not be comparable"
            )
        if not limit.get("counters"):
            problems.append(f"Kuadrant '{name}' has no counters, so it is one bucket for every tenant")
        for counter in limit.get("counters", []):
            expression = counter.get("expression", "")
            if '"' in expression:
                problems.append(
                    f"Kuadrant '{name}' counter {expression!r} contains a double quote: the "
                    f"expression text is interpolated into a CEL string for Limitador's "
                    f"variables, so the inner quotes end that string early, Limitador "
                    f"rejects the entire limit file and crash-loops on a cold start -- "
                    f"taking every limit in this policy with it while the RateLimitPolicy "
                    f"still reports Enforced. Use single quotes"
                )

    # The entitled team has to be named identically by all three authorization
    # policies and has to be a team the probe user is actually in, or the
    # entitlement row measures three different things.
    entitled_org, entitled = tenancy.get("team_a", (None, None))
    if entitled:
        sources = {
            "Kuadrant": KUADRANT_AUTH.read_text(),
            "agentgateway": AGENT_AUTH.read_text(),
            "Envoy Gateway": ENVOY_SECURITY.read_text(),
        }
        for stack, text in sources.items():
            if entitled not in text:
                problems.append(
                    f"the {stack} authorization policy never mentions the entitled team "
                    f"'{entitled}', so its entitlement row cannot pass"
                )

    # A team name is only unique inside its org, so an entitlement that names
    # the team alone is granted to a team of that name in *every* org. That is
    # a tenant boundary crossed by a name collision rather than by a grant, and
    # it is invisible until two orgs happen to use the same team name -- so the
    # realm keeps one that does. Each stack spells the binding differently;
    # what is checked is that all three bind it at all.
    if entitled and entitled_org:
        for stack, path, binds in (
            ("Kuadrant", KUADRANT_AUTH, kuadrant_binds_org),
            ("Envoy Gateway", ENVOY_SECURITY, envoy_binds_org),
            ("agentgateway", AGENT_AUTH, agent_binds_org),
        ):
            unbound = binds(path, entitled_org, entitled)
            for rule in unbound:
                problems.append(
                    f"the {stack} authorization rule '{rule}' entitles team "
                    f"'{entitled}' without binding it to org '{entitled_org}': a "
                    f"'{entitled}' team in any other org inherits the entitlement. "
                    f"The realm carries such a team for exactly this reason"
                )

    for problem in problems:
        print(problem, file=sys.stderr)
    print(
        f"{len(users)} tenant probe users, org cap {org_cap}, team cap {team_cap}, "
        f"entitled team '{entitled}', {len(problems)} problems"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
