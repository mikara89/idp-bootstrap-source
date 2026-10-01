#!/usr/bin/env python3
"""Validate a service promotion request while preserving accepted runtime settings."""

import json
import importlib.util
import os
import re
from pathlib import Path

import yaml


NAME = re.compile(r"^[a-z0-9-]+$")
ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED_ENV = {"PORT", "HEALTH_PATH", "READINESS_PATH", "METRICS_PATH"}
APPROVED_SECRET = re.compile(r"^(?:database-url|api-key|oauth-client-secret)(?:-v[1-9][0-9]*)?$")

RENDERER_PATH = Path(__file__).with_name("render-apps-stack.py")
RENDERER_SPEC = importlib.util.spec_from_file_location("idp_apps_renderer", RENDERER_PATH)
RENDERER = importlib.util.module_from_spec(RENDERER_SPEC)
RENDERER_SPEC.loader.exec_module(RENDERER)


def require(condition, message):
    if not condition:
        raise ValueError(message)


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
    if existing is None:
        owner = os.environ.get("GOLDEN_PATH_OWNER", "group:default/platform-team")
        profile = os.environ.get("GOLDEN_PATH_PROFILE", "small")
        exposure = os.environ.get("GOLDEN_PATH_EXPOSURE", "internal")
        port = int(os.environ.get("GOLDEN_PATH_PORT", "3000"))
        environment = json.loads(os.environ.get("GOLDEN_PATH_ENV_JSON", "{}"))
        secrets = json.loads(os.environ.get("GOLDEN_PATH_SECRET_REFS_JSON", "[]"))
        require(re.fullmatch(r"(?:group|user):[a-z0-9][a-z0-9._/-]*", owner), "invalid owner")
        require(profile in ("small", "medium"), "invalid resource profile")
        require(exposure in ("internal", "authenticated-web"), "invalid exposure mode")
        require(1 <= port <= 65535, "invalid service port")
        require(isinstance(environment, dict), "runtime environment must be an object")
        for key, value in environment.items():
            require(ENV.fullmatch(key) and key not in RESERVED_ENV and not re.search(r"(SECRET|PASSWORD|TOKEN|PRIVATE_KEY|API_KEY)", key), "runtime environment contains a secret-like key or reserved key")
            require(isinstance(value, (str, int, float, bool)) and not re.search(r"[\x00-\x1f\x7f$`'\"\\]", str(value)), "runtime environment values contain unsupported characters")
        require(isinstance(secrets, list) and all(isinstance(ref, str) and APPROVED_SECRET.fullmatch(ref) for ref in secrets), "runtime contains an unapproved secret reference")
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    main()
