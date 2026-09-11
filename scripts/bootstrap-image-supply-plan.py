#!/usr/bin/env python3
"""Plan first-boot Registry image supply from the platform manifest.

Ansible inspects the Registry and passes target *names* only. This helper is
the single source of truth for missing-target decisions, trusted-name
validation, and copy specs. Source image URLs and digests always come from
images/platform-images.yaml. Unknown names fail closed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

TERMINAL_FAIL = frozenset({"failed", "canceled", "skipped", "manual"})
NAME_RE = re.compile(r"^[a-z0-9-]+$")
REPOSITORY_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
REGISTRY_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BOOTSTRAP_JOBS = frozenset(
    {"build-backstage-image", "mirror-platform-images", "update-backstage-digest"}
)


@dataclass(frozen=True)
class Image:
    name: str
    source: str
    target: str
    digest: str


@dataclass(frozen=True)
class Plan:
    backstage_required: bool
    missing_platform: tuple[str, ...]
    trigger_pipeline: bool

    @property
    def missing_platform_csv(self) -> str:
        return ",".join(self.missing_platform)


def source_repository(source: str) -> str:
    """Return the repository portion of a tagged OCI source reference."""
    if not isinstance(source, str) or not source or source != source.strip():
        raise ValueError(f"invalid image source: {source!r}")
    if any(character.isspace() or ord(character) < 0x21 for character in source):
        raise ValueError(f"invalid image source: {source!r}")
    if "@" in source:
        raise ValueError("image source must not contain a digest")

    tag_separator = source.rfind(":")
    last_slash = source.rfind("/")
    if tag_separator <= last_slash:
        raise ValueError(f"image source must contain a final tag: {source!r}")
    repository = source[:tag_separator]
    tag = source[tag_separator + 1 :]
    if not repository or not TAG_RE.fullmatch(tag):
        raise ValueError(f"invalid image source tag: {source!r}")

    components = repository.split("/")
    if any(not component for component in components):
        raise ValueError(f"invalid image repository: {source!r}")

    first = components[0]
    has_registry = len(components) > 1 and (
        "." in first or ":" in first or first == "localhost"
    )
    if has_registry:
        if first.count(":") > 1:
            raise ValueError(f"invalid registry host: {source!r}")
        host, separator, port = first.partition(":")
        if not REGISTRY_HOST_RE.fullmatch(host):
            raise ValueError(f"invalid registry host: {source!r}")
        if separator and (not port.isdigit() or not 1 <= int(port) <= 65535):
            raise ValueError(f"invalid registry port: {source!r}")
        components = components[1:]

    if not components or any(
        not REPOSITORY_COMPONENT_RE.fullmatch(component) for component in components
    ):
        raise ValueError(f"invalid image repository: {source!r}")
    return repository


def pinned_source(source: str, digest: str) -> str:
    """Build the immutable source reference from trusted manifest fields."""
    repository = source_repository(source)
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        raise ValueError(f"invalid image digest for {source!r}")
    return f"{repository}@{digest}"


def load_manifest(path: Path) -> tuple[Image, ...]:
    images: list[Image] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if not current:
            return
        required = ("name", "source", "target", "digest")
        missing = [key for key in required if key not in current]
        if missing:
            raise ValueError(f"incomplete manifest entry missing {missing}: {current}")
        if not NAME_RE.fullmatch(current["name"]):
            raise ValueError(f"invalid manifest image name: {current['name']}")
        pinned_source(current["source"], current["digest"])
        images.append(
            Image(
                name=current["name"],
                source=current["source"],
                target=current["target"],
                digest=current["digest"],
            )
        )

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        stripped = line.strip()
        if stripped.startswith("- name:"):
            flush()
            current = {"name": stripped.split(":", 1)[1].strip()}
            continue
        if not current or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        if key in {"source", "target", "digest"}:
            current[key] = value.strip()
    flush()
    if not images:
        raise ValueError(f"no images found in {path}")
    names = [image.name for image in images]
    if len(names) != len(set(names)):
        raise ValueError("duplicate image names in manifest")
    return tuple(images)


def validate_requested_names(images: tuple[Image, ...], names: list[str]) -> tuple[str, ...]:
    known = {image.name: image for image in images}
    requested: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"untrusted image target name: {raw}")
        if name not in known:
            raise ValueError(f"unknown image target: {name}")
        if name in seen:
            continue
        seen.add(name)
        requested.append(name)
    return tuple(requested)


def copy_specs(images: tuple[Image, ...], names: list[str]) -> tuple[Image, ...]:
    requested = validate_requested_names(images, names)
    if not requested:
        raise ValueError("no trusted platform image names were supplied")
    known = {image.name: image for image in images}
    return tuple(known[name] for name in requested)


def plan(
    images: tuple[Image, ...],
    present_platform: set[str],
    backstage_present: bool,
) -> Plan:
    unknown_present = sorted(name for name in present_platform if name not in {image.name for image in images})
    if unknown_present:
        raise ValueError(f"unknown present image target: {unknown_present[0]}")
    missing = tuple(image.name for image in images if image.name not in present_platform)
    backstage_required = not backstage_present
    return Plan(
        backstage_required=backstage_required,
        missing_platform=missing,
        trigger_pipeline=bool(missing) or backstage_required,
    )


def job_allowed(
    *,
    source: str,
    mode: str,
    job: str,
    backstage_required: bool,
    missing_platform: list[str],
) -> bool:
    if source == "trigger":
        return job == "golden-path-update"
    if source == "api" and mode == "bootstrap-image-supply":
        if job == "build-backstage-image":
            return backstage_required
        if job == "update-backstage-digest":
            return backstage_required
        if job == "mirror-platform-images":
            return bool(missing_platform)
        return False
    if job in BOOTSTRAP_JOBS:
        return False
    return True


def await_pipeline(status: str, max_polls: int = 180) -> str:
    polls = 0
    current = status
    while polls < max_polls:
        polls += 1
        if current in TERMINAL_FAIL:
            raise RuntimeError(current)
        if current == "success":
            return current
        if current not in {"running", "pending", "created", "waiting_for_resource", "preparing"}:
            raise RuntimeError("timeout")
        current = "running"
    raise RuntimeError("timeout")


def require_all_digests(present: dict[str, bool]) -> None:
    missing = [name for name, ok in present.items() if not ok]
    if missing:
        raise RuntimeError("fail-closed")


def _parse_names(raw: str) -> list[str]:
    if not raw.strip():
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "images/platform-images.yaml",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan_cmd = sub.add_parser("plan")
    plan_cmd.add_argument("--present-platform", default="")
    plan_cmd.add_argument("--backstage-present", action="store_true")

    emit_cmd = sub.add_parser("emit-copy")
    emit_cmd.add_argument("--names", required=True)

    validate_cmd = sub.add_parser("validate-names")
    validate_cmd.add_argument("--names", required=True)

    args = parser.parse_args(argv)
    images = load_manifest(args.manifest)
    if args.command == "plan":
        result = plan(
            images,
            set(_parse_names(args.present_platform)),
            bool(args.backstage_present),
        )
        json.dump(asdict(result), sys.stdout)
        sys.stdout.write("\n")
        return 0
    if args.command == "validate-names":
        validate_requested_names(images, _parse_names(args.names))
        return 0
    specs = copy_specs(images, _parse_names(args.names))
    for image in specs:
        sys.stdout.write(f"{pinned_source(image.source, image.digest)} {image.target} {image.digest}\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
