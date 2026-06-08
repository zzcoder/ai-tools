#!/usr/bin/env python3
"""Remove redundant WeChat photo files from a folder.

Rules:

- Delete files whose names end with ".pic_thumb.jpg".
- If a non-HD file has a sibling HD version with "_hd" before the extension,
  delete the non-HD file. For example, when both of these exist:

      401491780799346_.pic.jpg
      401491780799346_.pic_hd.jpg

  the tool deletes 401491780799346_.pic.jpg.

Examples:

    ./tools/clean_wechat_photos.py /path/to/wechat/photos --dry-run
    ./tools/clean_wechat_photos.py /path/to/wechat/photos
    ./tools/clean_wechat_photos.py /path/to/wechat/photos --recursive
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


THUMB_SUFFIX = ".pic_thumb.jpg"


@dataclass(frozen=True)
class DeletePlan:
    path: Path
    reason: str


def iter_files(folder: Path, recursive: bool) -> list[Path]:
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(
        path
        for path in iterator
        if path.is_file()
    )


def hd_sibling(path: Path) -> Path | None:
    if "_hd" in path.stem:
        return None
    candidate = path.with_name(f"{path.stem}_hd{path.suffix}")
    return candidate if candidate.exists() and candidate.is_file() else None


def build_delete_plan(folder: Path, recursive: bool) -> list[DeletePlan]:
    plans_by_path: dict[Path, DeletePlan] = {}
    for path in iter_files(folder, recursive):
        if path.name.endswith(THUMB_SUFFIX):
            plans_by_path[path] = DeletePlan(path, "thumbnail")
            continue

        candidate = hd_sibling(path)
        if candidate is not None:
            plans_by_path[path] = DeletePlan(path, f"HD version exists: {candidate.name}")

    return list(plans_by_path.values())


def remove_files(plans: list[DeletePlan], dry_run: bool) -> None:
    for plan in plans:
        action = "would remove" if dry_run else "removing"
        print(f"{action}: {plan.path} ({plan.reason})")
        if not dry_run:
            plan.path.unlink()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Folder containing WeChat photo files.")
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Scan subfolders recursively. By default only the top-level folder is scanned.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print files that would be removed without deleting anything.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.folder.is_dir():
        print(f"error: folder not found: {args.folder}", file=sys.stderr)
        return 2

    plans = build_delete_plan(args.folder, args.recursive)
    remove_files(plans, args.dry_run)
    mode = "would remove" if args.dry_run else "removed"
    print(f"Summary: {mode} {len(plans)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
