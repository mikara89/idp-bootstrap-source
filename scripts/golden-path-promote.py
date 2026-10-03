#!/usr/bin/env python3
"""Validate a service promotion request while preserving accepted runtime settings."""

import json
import importlib.util
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml


NAME = re.compile(r"^[a-z0-9-]+$")
ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED_ENV = {"PORT", "HEALTH_PATH", "READINESS_PATH", "METRICS_PATH"}
APPROVED_SECRET = re.compile(r"^(?:database-url|api-key|oauth-client-secret)(?:-v[1-9][0-9]*)?$")
INVENTORY_PATH = Path("environments/prod/apps/secret-inventory.yml")
POLICY_PATH = Path("environments/prod/apps/secret-inventory-policy.yml")
INVENTORY_EPOCH = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
INVENTORY_RECORD_FIELDS = {
    "openbaoKVVersion", "dockerSecretName", "dockerSecretId", "verifiedAt",
}

RENDERER_PATH = Path(__file__).with_name("render-apps-stack.py")
RENDERER_SPEC = importlib.util.spec_from_file_location("idp_apps_renderer", RENDERER_PATH)
RENDERER = importlib.util.module_from_spec(RENDERER_SPEC)
RENDERER_SPEC.loader.exec_module(RENDERER)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def optional_json(name, default):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return json.loads(value)


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate inventory keys instead of silently keeping the last."""


def construct_unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "found an unhashable key", key_node.start_mark,
            ) from error
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "found duplicate key", key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping
)


def require_exact_keys(value, expected, message):
    require(isinstance(value, dict) and set(value) == set(expected), message)


def validate_inventory(document):
    require_exact_keys(
        document, {"apiVersion", "kind", "metadata", "services"},
        "runtime secret inventory has an invalid document shape",
    )
    require(document["apiVersion"] == "idp.platform/v1" and document["kind"] == "SecretInventory",
            "runtime secret inventory has an invalid apiVersion or kind")

    metadata = document["metadata"]
    require_exact_keys(
        metadata, {"deployment", "swarmClusterId", "inventoryEpoch"},
        "runtime secret inventory has invalid deployment metadata",
    )
    require(metadata["deployment"] == "production", "runtime secret inventory deployment must be production")
    require(isinstance(metadata["swarmClusterId"], str)
            and re.fullmatch(r"[a-f0-9]{64}", metadata["swarmClusterId"]),
            "runtime secret inventory has an invalid Swarm incarnation fingerprint")
    require(isinstance(metadata["inventoryEpoch"], str)
            and INVENTORY_EPOCH.fullmatch(metadata["inventoryEpoch"]),
            "runtime secret inventory has an invalid inventory epoch")

    services = document["services"]
    require(isinstance(services, dict), "runtime secret inventory services must be a mapping")
    for service, references in services.items():
        require(isinstance(service, str) and NAME.fullmatch(service),
                "runtime secret inventory contains an invalid service name")
        require(isinstance(references, dict), "runtime secret inventory references must be a mapping")
        for reference, record in references.items():
            require(isinstance(reference, str) and APPROVED_SECRET.fullmatch(reference),
                    "runtime secret inventory contains an unapproved reference")
            require_exact_keys(record, INVENTORY_RECORD_FIELDS,
                               "runtime secret inventory record has an invalid shape")
            require(type(record["openbaoKVVersion"]) is int and record["openbaoKVVersion"] > 0,
                    "runtime secret inventory record has an invalid OpenBao KV version")
            expected_name = f"idp-app-{service}-{reference}"
            require(record["dockerSecretName"] == expected_name,
                    "runtime secret inventory record has a mismatched Docker secret name")
            require(isinstance(record["dockerSecretId"], str)
                    and re.fullmatch(r"[a-z0-9]{12,64}", record["dockerSecretId"]),
                    "runtime secret inventory record has an invalid Docker secret object ID")
            verified_at = record["verifiedAt"]
            require(isinstance(verified_at, str)
                    and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", verified_at),
                    "runtime secret inventory record has an invalid verification time")
            try:
                datetime.fromisoformat(verified_at[:-1] + "+00:00")
            except ValueError as error:
                raise ValueError("runtime secret inventory record has an invalid verification time") from error
    return services


def load_committed_yaml(repository_root, relative_path, description):
    worktree_path = repository_root / relative_path
    repository = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=repository_root,
        capture_output=True, text=True, check=False,
    )
    if repository.returncode != 0:
        if worktree_path.exists():
            raise ValueError(f"{description} must be in the current Git checkout")
        return None
    committed = subprocess.run(
        ["git", "show", f"HEAD:{relative_path.as_posix()}"], cwd=repository_root,
        capture_output=True, text=True, check=False,
    )
    if committed.returncode != 0:
        if worktree_path.exists():
            raise ValueError(f"{description} must be in the current Git checkout")
        return None
    try:
        return yaml.load(committed.stdout, Loader=UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"{description} is malformed") from error


def load_inventory(repository_root):
    document = load_committed_yaml(
        repository_root, INVENTORY_PATH, "runtime secret inventory"
    )
    if document is None:
        return {}
    validate_inventory(document)
    return document


def validate_policy(document):
    require_exact_keys(
        document, {"apiVersion", "kind", "metadata"},
        "production secret inventory policy has an invalid document shape",
    )
    require(document["apiVersion"] == "idp.platform/v1" and document["kind"] == "SecretInventoryPolicy",
            "production secret inventory policy has an invalid apiVersion or kind")
    metadata = document["metadata"]
    require_exact_keys(
        metadata, {"deployment", "swarmClusterId", "inventoryEpoch"},
        "production secret inventory policy has invalid metadata",
    )
    require(metadata["deployment"] == "production",
            "production secret inventory policy deployment must be production")
    cluster_id = metadata["swarmClusterId"]
    require(isinstance(cluster_id, str)
            and (cluster_id == "UNCONFIGURED" or re.fullmatch(r"[a-f0-9]{64}", cluster_id)),
            "production secret inventory policy has an invalid Swarm incarnation fingerprint")
    require(isinstance(metadata["inventoryEpoch"], str)
            and INVENTORY_EPOCH.fullmatch(metadata["inventoryEpoch"]),
            "production secret inventory policy has an invalid inventory epoch")
    return metadata


def load_policy(repository_root):
    document = load_committed_yaml(
        repository_root, POLICY_PATH, "production secret inventory policy"
    )
    require(document is not None,
            "production secret inventory policy is missing from the current Git checkout")
    validate_policy(document)
    return document


def require_provisioned_secrets(service, references, inventory):
    provisioned = inventory.get(service, {})
    for reference in references:
        if reference not in provisioned:
            raise ValueError(f"required runtime secret is not provisioned: {service}/{reference}")


def collect_definition_secret_refs(definitions_directory, current_path, current_definition, registry):
    references = []
    for path in sorted(definitions_directory.glob("*.yaml")):
        definition = current_definition if path == current_path else RENDERER.load_definition(path, registry)
        service = definition["metadata"]["name"]
        for reference in definition["spec"]["runtime"]["secrets"]:
            references.append((service, reference))
    if not current_path.exists():
        service = current_definition["metadata"]["name"]
        for reference in current_definition["spec"]["runtime"]["secrets"]:
            references.append((service, reference))
    return references


def require_policy_matches_inventory(references, repository_root, inventory):
    if not references:
        return
    policy = load_policy(repository_root)
    policy_metadata = policy["metadata"]
    require(policy_metadata["swarmClusterId"] != "UNCONFIGURED",
            "production secret inventory policy has not been configured with the current Swarm fingerprint")
    require(inventory.get("metadata") == policy_metadata,
            "runtime secret inventory does not match the committed production inventory policy")


def main():
    service = os.environ.get("GOLDEN_PATH_SERVICE", "")
    project = os.environ.get("GOLDEN_PATH_PROJECT", "")
    image = os.environ.get("GOLDEN_PATH_IMAGE", "")
    source_sha = os.environ.get("GOLDEN_PATH_SOURCE_SHA", "")
    registry = os.environ.get("TRUSTED_REGISTRY", "")
    require(NAME.fullmatch(service), "invalid service name")
    require(project == f"platform/services/{service}", "service project path mismatch")
    require(re.fullmatch(r"registry\.[a-z0-9.-]+", registry), "invalid trusted registry")
    require(re.fullmatch(re.escape(registry) + r"/platform/services/" + service + r"@sha256:[a-f0-9]{64}", image), "image must be digest-pinned in the trusted service repository")
    require(re.fullmatch(r"[a-f0-9]{40,64}", source_sha), "source revision must be a full commit hash")
    destination = Path("environments/prod/apps/definitions") / f"{service}.yaml"
    existing = yaml.safe_load(destination.read_text(encoding="utf-8")) if destination.exists() else None
    requested_secrets = optional_json("GOLDEN_PATH_SECRET_REFS_JSON", [])
    require(isinstance(requested_secrets, list)
            and all(isinstance(ref, str) and APPROVED_SECRET.fullmatch(ref) for ref in requested_secrets),
            "runtime contains an unapproved secret reference")
    require(len(requested_secrets) == len(set(requested_secrets)),
            "runtime contains a duplicate secret reference")

    if existing is None:
        owner = os.environ.get("GOLDEN_PATH_OWNER", "group:default/platform-team")
        profile = os.environ.get("GOLDEN_PATH_PROFILE", "small")
        exposure = os.environ.get("GOLDEN_PATH_EXPOSURE", "internal")
        port = int(os.environ.get("GOLDEN_PATH_PORT", "3000"))
        environment = optional_json("GOLDEN_PATH_ENV_JSON", {})
        secrets = requested_secrets
        require(re.fullmatch(r"(?:group|user):[a-z0-9][a-z0-9._/-]*", owner), "invalid owner")
        require(profile in ("small", "medium"), "invalid resource profile")
        require(exposure in ("internal", "authenticated-web"), "invalid exposure mode")
        require(1 <= port <= 65535, "invalid service port")
        require(isinstance(environment, dict), "runtime environment must be an object")
        for key, value in environment.items():
            require(ENV.fullmatch(key) and key not in RESERVED_ENV and not re.search(r"(SECRET|PASSWORD|TOKEN|PRIVATE_KEY|API_KEY)", key), "runtime environment contains a secret-like key or reserved key")
            require(isinstance(value, (str, int, float, bool)) and not re.search(r"[\x00-\x1f\x7f$`'\"\\]", str(value)), "runtime environment values contain unsupported characters")
        existing = {
            "apiVersion": "idp.platform/v1", "kind": "SwarmService",
            "metadata": {"name": service, "owner": owner},
            "spec": {
                "source": {"projectPath": project},
                "runtime": {"port": port, "environment": environment, "secrets": secrets},
                "exposure": {"mode": exposure}, "resources": {"profile": profile},
                "health": {"path": "/healthz"}, "readiness": {"path": "/readyz"},
                "metrics": {"enabled": True, "path": "/metrics"},
            },
        }
    require(isinstance(existing, dict) and existing.get("apiVersion") == "idp.platform/v1" and existing.get("kind") == "SwarmService", "existing service definition is invalid")
    spec = existing.setdefault("spec", {})
    require(spec.setdefault("source", {}).get("projectPath") == project, "existing service project path mismatch")
    spec["source"]["revision"] = source_sha
    spec["image"] = {"repository": image}
    RENDERER.validate_definition(existing, destination, registry)

    # Read only committed inventory and policy metadata. Request variables never
    # supply or modify either file, and the stage helper allowlists neither.
    inventory_document = load_inventory(Path.cwd())
    inventory_services = inventory_document.get("services", {})
    all_references = collect_definition_secret_refs(
        destination.parent, destination, existing, registry
    )
    # The trusted platform definition is authoritative for runtime requirements.
    # Trigger references seed a new definition only; generated service pipelines
    # may keep sending scaffold-time values after the definition has rotated.
    for reference_service, reference in all_references:
        require_provisioned_secrets(reference_service, [reference], inventory_services)
    require_policy_matches_inventory(all_references, Path.cwd(), inventory_document)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, yaml.YAMLError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
