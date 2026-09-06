#!/usr/bin/env python3
"""Stage and commit only Golden Path definition and generated files."""
import os
import subprocess
import sys

ALLOWED_GENERATED = (
    "environments/prod/apps/stack.yml",
    "environments/prod/platform/observability/config/file_sd/apps.yml",
)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=check, text=True, capture_output=True)


def porcelain_paths() -> list[str]:
    result = git("status", "--porcelain")
    paths = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip())
    return paths


def main() -> int:
    service = os.environ.get("GOLDEN_PATH_SERVICE", "").strip()
    source_sha = os.environ.get("GOLDEN_PATH_SOURCE_SHA", "unknown").strip()
    if not service:
        print("GOLDEN_PATH_SERVICE is required", file=sys.stderr)
        return 2
    definition = f"environments/prod/apps/definitions/{service}.yaml"
    allowed = {definition, *ALLOWED_GENERATED}
    add = git("add", "--", *sorted(allowed), check=False)
    if add.returncode != 0:
        print(add.stderr, file=sys.stderr)
        return add.returncode
    unexpected = [path for path in porcelain_paths() if path not in allowed]
    if unexpected:
        print("unexpected golden-path paths: " + ", ".join(sorted(unexpected)), file=sys.stderr)
        return 1
    cached = git("diff", "--cached", "--quiet", check=False)
    if cached.returncode == 0:
        return 0
    message = f"feat(apps): promote {service}@{source_sha} [skip ci]"
    commit = git("-c", "user.name=IDP GitLab CI", "-c", "user.email=idp-ci@local", "commit", "-m", message, check=False)
    if commit.returncode != 0:
        print(commit.stderr, file=sys.stderr)
        return commit.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
