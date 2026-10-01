#!/usr/bin/env python3
"""Run the shared, portable validation commands for maintainers."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts/validation-commands.json"


def load_manifest():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("domains"), dict):
        raise ValueError("validation command manifest has an unsupported schema")
    return manifest


def run(commands):
    for command in commands:
        executable = shutil.which(command[0])
        if executable is None:
            print(f"Required command is unavailable: {command[0]}", file=sys.stderr)
            return 2
        print("+ " + " ".join(command), flush=True)
        result = subprocess.run([executable, *command[1:]], cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("core", "named-domain", "full", "exported-runtime"), default="core")
    parser.add_argument("--domain", help="domain to run in named-domain mode")
    args = parser.parse_args()
    try:
        manifest = load_manifest()
        domains = manifest["domains"]
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.mode == "exported-runtime":
        runtime = manifest.get("exported_runtime_tree", {})
        commands = runtime.get("commands", [])
        if not isinstance(commands, list) or not commands:
            print("exported runtime command list is missing", file=sys.stderr)
            return 2
        return run(commands)
    if args.mode == "core":
        names = ["core"]
    elif args.mode == "named-domain":
        if args.domain not in domains:
            parser.error("--domain must name a domain in scripts/validation-commands.json")
        names = [args.domain]
    else:
        names = [name for name in domains if name != "core"]
        names.insert(0, "core")
    return run([command for name in names for command in domains[name]])


if __name__ == "__main__":
    raise SystemExit(main())
