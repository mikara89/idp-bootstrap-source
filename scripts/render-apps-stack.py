#!/usr/bin/env python3
"""Render validated IDP service definitions into Swarm runtime files."""

import argparse
import pathlib
import re
import sys

import yaml


NAME = re.compile(r"^[a-z0-9-]+$")
SECRET_REF = re.compile(r"^(?:database-url|api-key|oauth-client-secret)(?:-v[1-9][0-9]*)?$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED_ENV = {"PORT", "HEALTH_PATH", "READINESS_PATH", "METRICS_PATH"}
PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
PROFILES = {
    "small": {"limits": {"cpus": "0.5", "memory": "256M"}, "reservations": {"cpus": "0.1", "memory": "64M"}},
    "medium": {"limits": {"cpus": "1.0", "memory": "512M"}, "reservations": {"cpus": "0.25", "memory": "128M"}},
}


def fail(message):
    raise ValueError(message)


def valid_path(value, field, filename):
    if not isinstance(value, str) or not PATH.fullmatch(value):
        fail(f"{filename}: invalid {field} path")
    return value


def load_definition(path, registry):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return validate_definition(data, path, registry)


def validate_definition(data, path, registry):
    if not isinstance(data, dict) or data.get("apiVersion") != "idp.platform/v1" or data.get("kind") != "SwarmService":
        fail(f"{path}: invalid apiVersion or kind")
    meta, spec = data.get("metadata"), data.get("spec")
    if not isinstance(meta, dict) or not NAME.fullmatch(str(meta.get("name", ""))):
        fail(f"{path}: invalid service name")
    if not isinstance(spec, dict):
        fail(f"{path}: missing spec")
    name = meta["name"]
    meta.setdefault("owner", "group:default/platform-team")
    if not re.fullmatch(r"(?:group|user):[a-z0-9][a-z0-9._/-]*", str(meta["owner"])):
        fail(f"{path}: invalid owner")
    source, image, runtime = spec.get("source"), spec.get("image"), spec.get("runtime")
    expected_project = f"platform/services/{name}"
    expected_image = re.compile(r"^" + re.escape(registry) + r"/platform/services/" + re.escape(name) + r"@sha256:[a-f0-9]{64}$")
    if not isinstance(source, dict) or source.get("projectPath") != expected_project:
        fail(f"{path}: source project must match service name")
    if not isinstance(source.get("revision", ""), str) or (source.get("revision") and not re.fullmatch(r"[a-f0-9]{40,64}", source["revision"])):
        fail(f"{path}: invalid source revision")
    if not isinstance(image, dict) or not expected_image.fullmatch(str(image.get("repository", ""))):
        fail(f"{path}: image must use the trusted registry and matching digest-pinned service path")
    if not isinstance(runtime, dict) or not isinstance(runtime.get("port"), int) or isinstance(runtime.get("port"), bool) or not 1 <= runtime["port"] <= 65535:
        fail(f"{path}: invalid runtime port")
    runtime.setdefault("environment", {})
    runtime.setdefault("secrets", [])
    if not isinstance(runtime["environment"], dict):
        fail(f"{path}: runtime.environment must be a mapping")
    for key, value in runtime["environment"].items():
        if not isinstance(key, str) or not ENV_NAME.fullmatch(key) or key in RESERVED_ENV or re.search(r"(SECRET|PASSWORD|TOKEN|PRIVATE_KEY|API_KEY)", key):
            fail(f"{path}: runtime environment may contain only non-secret variable names")
        if not isinstance(value, (str, int, float, bool)) or re.search(r"[\x00-\x1f\x7f$`'\"\\]", str(value)):
            fail(f"{path}: runtime environment values must be simple, non-secret scalars")
    secrets = runtime["secrets"]
    if not isinstance(secrets, list) or any(not isinstance(ref, str) or not SECRET_REF.fullmatch(ref) for ref in secrets) or len(secrets) != len(set(secrets)):
        fail(f"{path}: runtime.secrets contains an unapproved or duplicate reference")
    resources = spec.setdefault("resources", {})
    if not isinstance(resources, dict):
        fail(f"{path}: spec.resources must be a mapping")
    profile = resources.setdefault("profile", "small")
    if profile not in PROFILES:
        fail(f"{path}: unsupported resource profile")
    health, readiness = spec.setdefault("health", {}), spec.setdefault("readiness", {})
    if not isinstance(health, dict) or not isinstance(readiness, dict):
        fail(f"{path}: health and readiness must be mappings")
    valid_path(health.setdefault("path", "/healthz"), "health", path)
    valid_path(readiness.setdefault("path", "/readyz"), "readiness", path)
    metrics = spec.setdefault("metrics", {"enabled": False, "path": "/metrics"})
    if not isinstance(metrics, dict) or not isinstance(metrics.get("enabled", False), bool):
        fail(f"{path}: metrics.enabled must be boolean")
    if metrics.get("enabled", False):
        valid_path(metrics.setdefault("path", "/metrics"), "metrics", path)
    exposure_config = spec.setdefault("exposure", {})
    if not isinstance(exposure_config, dict):
        fail(f"{path}: spec.exposure must be a mapping")
    exposure = exposure_config.get("mode", "internal")
    if exposure not in ("internal", "authenticated-web"):
        fail(f"{path}: invalid exposure mode")
    return data


def render(definitions):
    services, metrics, secrets = {}, [], {}
    for definition in definitions:
        name, spec = definition["metadata"]["name"], definition["spec"]
        runtime = spec["runtime"]
        port = runtime["port"]
        exposure = spec.get("exposure", {}).get("mode", "internal")
        health_path = spec["health"]["path"]
        readiness_path = spec["readiness"]["path"]
        networks = ["apps_net", "metrics_net"] + (["public_net"] if exposure == "authenticated-web" else [])
        labels = {"traefik.enable": "false"}
        if exposure == "authenticated-web":
            labels = {
                "traefik.enable": "true",
                "traefik.swarm.network": "public_net",
                f"traefik.http.routers.{name}.rule": f"Host(`{name}.${{DOMAIN}}`)",
                f"traefik.http.routers.{name}.entrypoints": "websecure",
                f"traefik.http.routers.{name}.tls.certresolver": "le",
                f"traefik.http.routers.{name}.middlewares": "oauth-errors@swarm,auth@swarm",
                f"traefik.http.services.{name}.loadbalancer.server.port": str(port),
                f"traefik.http.services.{name}.loadbalancer.healthcheck.path": readiness_path,
                f"traefik.http.services.{name}.loadbalancer.healthcheck.interval": "10s",
                f"traefik.http.services.{name}.loadbalancer.healthcheck.timeout": "3s",
            }
        environment = {"PORT": str(port), "HEALTH_PATH": health_path, "READINESS_PATH": readiness_path}
        if spec["metrics"].get("enabled", False):
            environment["METRICS_PATH"] = spec["metrics"]["path"]
        environment.update({key: str(value) for key, value in runtime["environment"].items()})
        labels["idp.platform.source-revision"] = spec.get("source", {}).get("revision", "unknown")
        labels["idp.platform.port"] = str(port)
        labels["idp.platform.readiness-path"] = readiness_path
        service = {
            "image": spec["image"]["repository"], "networks": networks, "environment": environment,
            "deploy": {
                "placement": {"constraints": ["node.labels.workload == apps"]},
                "resources": PROFILES[spec["resources"]["profile"]],
                "restart_policy": {"condition": "any", "delay": "5s", "max_attempts": 3},
                "update_config": {"parallelism": 1, "delay": "10s", "monitor": "30s", "order": "start-first", "failure_action": "pause"},
                "labels": [f"{key}={value}" for key, value in labels.items()],
            },
        }
        if runtime["secrets"]:
            service["secrets"] = [{"source": f"{name}__{ref}", "target": ref} for ref in runtime["secrets"]]
            for ref in runtime["secrets"]:
                secrets[f"{name}__{ref}"] = {"external": True, "name": f"idp-app-{name}-{ref}"}
        services[name] = service
        if spec["metrics"].get("enabled", False):
            metrics.append({"targets": [f"tasks.platform_apps_{name}:{port}"], "labels": {"service": name, "__metrics_path__": spec["metrics"]["path"]}})
    if not services:
        services["apps-placeholder"] = {"image": "${CONTAINER_REGISTRY}/${IMAGE_PREFIX}/alpine@sha256:43027b43fb785b7c5adc53bd3b5dbc1a258270a2e8aff24f477b45c4e38dac68", "command": ["sh", "-c", "sleep infinity"], "deploy": {"replicas": 0, "resources": {"limits": {"cpus": "0.1", "memory": "64M"}}}}
    return {"version": "3.8", "networks": {"apps_net": {"external": True}, "metrics_net": {"external": True}, "public_net": {"external": True}}, "secrets": secrets, "services": services}, metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True)
    parser.add_argument("--definitions", default="environments/prod/apps/definitions")
    parser.add_argument("--stack", default="environments/prod/apps/stack.yml")
    parser.add_argument("--metrics", default="environments/prod/platform/observability/config/file_sd/apps.yml")
    args = parser.parse_args()
    if not re.fullmatch(r"registry\.[a-z0-9.-]+", args.registry):
        fail("invalid trusted registry")
    definitions = [load_definition(path, args.registry) for path in pathlib.Path(args.definitions).glob("*.yaml")]
    names = [definition["metadata"]["name"] for definition in definitions]
    if len(names) != len(set(names)):
        fail("duplicate service names")
    definitions.sort(key=lambda definition: definition["metadata"]["name"])
    stack, metrics = render(definitions)
    for target, value in ((pathlib.Path(args.stack), stack), (pathlib.Path(args.metrics), metrics)):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# GENERATED FILE — DO NOT EDIT DIRECTLY. Source: definitions/; renderer: scripts/render-apps-stack.py\n" + yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"render-apps-stack: {error}", file=sys.stderr)
        sys.exit(1)
