#!/usr/bin/env python3
"""Validate this repository's manifests against the vendored CRD schemas.

The gateway policies cannot be smoke-tested without three kind clusters, but
every field they use is described by a CustomResourceDefinition that ships
inside the Helm charts already vendored here. This walks those structural
schemas offline: unknown fields, wrong types, bad enum values, and missing
required fields are all caught before a manifest reaches a cluster.

Usage: python3 scripts/validate-policies.py [manifest ...]
"""
import glob
import os
import sys
import tarfile

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML ships with the tooling
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Charts that carry the CRDs the manifests below are written against.
CHARTS = (
    "kuadrant/charts/kuadrant-operator-1.5.2.tgz",
    "envoy-ai-gateway/charts/ai-gateway-crds-helm-v1.0.0.tgz",
    "envoy-ai-gateway/charts/gateway-helm-v1.8.1.tgz",
    "agentgateway/charts/agentgateway-crds-v1.4.1.tgz",
    "kserve/charts/kserve-llmisvc-crd-v0.20.0.tgz",
    "kserve/charts/cert-manager-v1.17.0.tgz",
)

MANIFESTS = (
    "kuadrant/manifests/*.yaml",
    "kuadrant/policies/*.yaml",
    "kuadrant/pools-overlay/*.yaml",
    "envoy-ai-gateway/manifests/*.yaml",
    "envoy-ai-gateway/policies/*.yaml",
    "agentgateway/manifests/*.yaml",
    "agentgateway/policies/*.yaml",
    "keycloak/manifests/*.yaml",
    "kserve/manifests/*.yaml",
    "kserve/pools/*.yaml",
)


def load_schemas():
    """Map (group, kind, version) -> structural schema from the vendored charts."""
    schemas = {}
    for chart in CHARTS:
        path = os.path.join(ROOT, chart)
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                if not member.name.endswith((".yaml", ".yml")):
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                text = handle.read().decode("utf-8", "replace")
                if "CustomResourceDefinition" not in text:
                    continue
                # Helm templates are not YAML; the CRD files vendored here are
                # plain documents, so anything that fails to parse is skipped.
                try:
                    documents = list(yaml.safe_load_all(text))
                except yaml.YAMLError:
                    continue
                for document in documents:
                    if not isinstance(document, dict):
                        continue
                    if document.get("kind") != "CustomResourceDefinition":
                        continue
                    spec = document.get("spec", {})
                    group = spec.get("group")
                    kind = spec.get("names", {}).get("kind")
                    for version in spec.get("versions", []):
                        schema = version.get("schema", {}).get("openAPIV3Schema")
                        if schema:
                            schemas[(group, kind, version["name"])] = schema
    return schemas


def validate(node, schema, path, errors):
    if not isinstance(schema, dict):
        return
    if schema.get("x-kubernetes-preserve-unknown-fields") and "properties" not in schema:
        return
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        if not isinstance(node, dict):
            errors.append(f"{path}: expected object, got {type(node).__name__}")
            return
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        for name in schema.get("required", []):
            if name not in node:
                errors.append(f"{path}: missing required field '{name}'")
        for name, value in node.items():
            if name in properties:
                validate(value, properties[name], f"{path}.{name}", errors)
            elif isinstance(extra, dict):
                validate(value, extra, f"{path}.{name}", errors)
            elif extra is False or (properties and extra is None):
                errors.append(f"{path}: unknown field '{name}'")
        return
    if kind == "array":
        if not isinstance(node, list):
            errors.append(f"{path}: expected array, got {type(node).__name__}")
            return
        for index, value in enumerate(node):
            validate(value, schema.get("items", {}), f"{path}[{index}]", errors)
        return
    if kind == "string":
        if isinstance(node, bool) or not isinstance(node, str):
            errors.append(f"{path}: expected string, got {type(node).__name__}")
            return
        if schema.get("enum") and node not in schema["enum"]:
            errors.append(f"{path}: '{node}' is not one of {schema['enum']}")
        return
    if kind == "integer":
        if isinstance(node, bool) or not isinstance(node, int):
            errors.append(f"{path}: expected integer, got {type(node).__name__}")
        return
    if kind == "boolean" and not isinstance(node, bool):
        errors.append(f"{path}: expected boolean, got {type(node).__name__}")


def main(argv):
    schemas = load_schemas()
    files = argv or sorted(
        {path for pattern in MANIFESTS
         for path in glob.glob(os.path.join(ROOT, pattern))}
    )
    checked = skipped = 0
    errors = []
    for path in files:
        relative = os.path.relpath(path, ROOT)
        with open(path, encoding="utf-8") as handle:
            documents = list(yaml.safe_load_all(handle))
        for document in documents:
            if not isinstance(document, dict) or "kind" not in document:
                continue
            api_version = document.get("apiVersion", "")
            group, _, version = api_version.rpartition("/")
            key = (group, document["kind"], version)
            schema = schemas.get(key)
            if schema is None:
                skipped += 1
                continue
            name = document.get("metadata", {}).get("name", "?")
            found = []
            validate(document, schema, f"{document['kind']}/{name}", found)
            errors.extend(f"{relative}: {problem}" for problem in found)
            checked += 1
    for problem in errors:
        print(problem, file=sys.stderr)
    print(f"{checked} resources validated against vendored CRD schemas, "
          f"{skipped} skipped (no vendored schema), {len(errors)} problems")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
