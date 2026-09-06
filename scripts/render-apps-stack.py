#!/usr/bin/env python3
"""Render the constrained Phase 11 application intent into a Swarm stack."""
import argparse, pathlib, re, sys
import yaml

NAME = re.compile(r"^[a-z0-9-]+$")
PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")

def fail(message): raise ValueError(message)
def load_definition(path, registry):
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or data.get("apiVersion") != "idp.platform/v1" or data.get("kind") != "SwarmService": fail(f"{path}: invalid apiVersion or kind")
    meta, spec = data.get("metadata"), data.get("spec")
    if not isinstance(meta, dict) or not NAME.fullmatch(str(meta.get("name", ""))): fail(f"{path}: invalid service name")
    if not isinstance(spec, dict): fail(f"{path}: missing spec")
    source, image, runtime = spec.get("source"), spec.get("image"), spec.get("runtime")
    expected_project = f"platform/services/{meta['name']}"
    expected_image = re.compile(r"^" + re.escape(registry) + r"/platform/services/" + re.escape(meta['name']) + r"@sha256:[a-f0-9]{64}$")
    if not isinstance(source, dict) or source.get("projectPath") != expected_project: fail(f"{path}: source project must match service name")
    if not isinstance(image, dict) or not expected_image.fullmatch(str(image.get("repository", ""))): fail(f"{path}: image must use the trusted registry and matching digest-pinned service path")
    if not isinstance(runtime, dict) or not isinstance(runtime.get("port"), int) or not 1 <= runtime["port"] <= 65535: fail(f"{path}: invalid runtime port")
    exposure = spec.get("exposure", {}).get("mode", "internal")
    if exposure not in ("internal", "authenticated-web"): fail(f"{path}: invalid exposure mode")
    for section in ("health", "readiness", "metrics"):
        item = spec.get(section, {})
        if section == "metrics" and item.get("enabled", False) is False: continue
        if not isinstance(item, dict) or not PATH.fullmatch(str(item.get("path", ""))): fail(f"{path}: invalid {section} path")
    return data

def render(definitions):
    services = {}
    metrics = []
    for definition in definitions:
        name, spec = definition["metadata"]["name"], definition["spec"]
        port, exposure = spec["runtime"]["port"], spec.get("exposure",{}).get("mode","internal")
        networks = ["apps_net", "metrics_net"] + (["public_net"] if exposure == "authenticated-web" else [])
        labels = {"traefik.enable": "false"}
        if exposure == "authenticated-web":
            labels = {"traefik.enable":"true", "traefik.swarm.network":"public_net", "traefik.http.routers.%s.rule" % name:"Host(`%s.${DOMAIN}`)" % name, "traefik.http.routers.%s.entrypoints" % name:"websecure", "traefik.http.routers.%s.tls.certresolver" % name:"le", "traefik.http.routers.%s.middlewares" % name:"oauth-errors@swarm,auth@swarm", "traefik.http.services.%s.loadbalancer.server.port" % name:str(port)}
        services[name] = {"image":spec["image"]["repository"], "networks":networks, "deploy":{"placement":{"constraints":["node.labels.workload == apps"]},"resources":{"limits":{"cpus":"0.5","memory":"256M"},"reservations":{"cpus":"0.1","memory":"64M"}},"restart_policy":{"condition":"any","delay":"5s","max_attempts":3},"update_config":{"parallelism":1,"delay":"10s","order":"start-first"},"labels":[f"{k}={v}" for k,v in labels.items()]}}
        if spec.get("metrics",{}).get("enabled",False): metrics.append({"targets":[f"tasks.platform_apps_{name}:{port}"],"labels":{"service":name,"__metrics_path__":spec["metrics"]["path"]}})
    if not services: services["apps-placeholder"]={"image":"${CONTAINER_REGISTRY}/${IMAGE_PREFIX}/alpine@sha256:43027b43fb785b7c5adc53bd3b5dbc1a258270a2e8aff24f477b45c4e38dac68", "command":["sh","-c","sleep infinity"], "deploy":{"replicas":0,"resources":{"limits":{"cpus":"0.1","memory":"64M"}}}}
    return {"version":"3.8", "networks":{"apps_net":{"external":True},"metrics_net":{"external":True},"public_net":{"external":True}}, "services":services}, metrics

def main():
    p=argparse.ArgumentParser(); p.add_argument("--registry",required=True); p.add_argument("--definitions",default="environments/prod/apps/definitions"); p.add_argument("--stack",default="environments/prod/apps/stack.yml"); p.add_argument("--metrics",default="environments/prod/platform/observability/config/file_sd/apps.yml"); a=p.parse_args()
    if not re.fullmatch(r"registry\.[a-z0-9.-]+", a.registry): fail("invalid trusted registry")
    definitions=[load_definition(f, a.registry) for f in pathlib.Path(a.definitions).glob("*.yaml")]
    names=[d["metadata"]["name"] for d in definitions]
    if len(names)!=len(set(names)): fail("duplicate service names")
    definitions.sort(key=lambda d:d["metadata"]["name"])
    stack, metrics=render(definitions)
    for target, value in ((pathlib.Path(a.stack),stack),(pathlib.Path(a.metrics),metrics)):
        target.parent.mkdir(parents=True,exist_ok=True); target.write_text("# GENERATED FILE — DO NOT EDIT DIRECTLY. Source: definitions/; renderer: scripts/render-apps-stack.py\n" + yaml.safe_dump(value, sort_keys=False))
if __name__ == "__main__":
    try: main()
    except ValueError as error: print(f"render-apps-stack: {error}",file=sys.stderr); sys.exit(1)
