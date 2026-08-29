#!/usr/bin/env python3
"""Update the Kustomize image reference used by Calendar GitOps deploys."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "repository",
        help="Image repository without tag, for example evofront/calendar-backend",
    )
    parser.add_argument("tag", help="Git commit SHA image tag")
    parser.add_argument(
        "--image-name",
        default=None,
        help="Kustomize image name to update (defaults to the final path segment)",
    )
    parser.add_argument(
        "--file",
        default="k8s/overlays/staging/kustomization.yaml",
        help="Kustomization file to update",
    )
    args = parser.parse_args()

    image_name = args.image_name or args.repository.rsplit("/", 1)[-1]
    path = Path(args.file)
    if not path.is_file():
        print(f"error: {path} not found", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"(- name: {re.escape(image_name)}\s*\n"
        rf"\s+newName: ).*(\n"
        rf"\s+newTag: ).*",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        print(f"error: image entry for {image_name} not found in {path}", file=sys.stderr)
        return 1

    updated = (
        text[: match.start()]
        + f"{match.group(1)}{args.repository}{match.group(2)}{args.tag}"
        + text[match.end() :]
    )
    path.write_text(updated, encoding="utf-8")
    print(f"Updated {path}: {args.repository}:{args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
