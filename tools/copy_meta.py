#!/usr/bin/env python3
"""Copy missing GPS and create-time metadata from one photo to another.

Given source photo A and target photo B:

- If B does not have GPS metadata, copy GPS metadata from A.
- If B does not have create-time metadata, copy create time from A.

Examples:

    ./tools/copy_meta.py source.jpg target.dng --dry-run
    ./tools/copy_meta.py source.jpg target.jpg
    ./tools/copy_meta.py source.jpg target.jpg --overwrite-original

ExifTool is required:

    sudo apt install libimage-exiftool-perl
    brew install exiftool
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GPS_TAGS = (
    "GPSLatitude",
    "GPSLatitudeRef",
    "GPSLongitude",
    "GPSLongitudeRef",
    "GPSAltitude",
    "GPSAltitudeRef",
    "GPSDateStamp",
    "GPSTimeStamp",
    "GPSMapDatum",
)
CREATE_TIME_TAGS = (
    "DateTimeOriginal",
    "CreateDate",
    "ModifyDate",
    "SubSecDateTimeOriginal",
    "SubSecCreateDate",
    "OffsetTime",
    "OffsetTimeOriginal",
    "OffsetTimeDigitized",
)


@dataclass(frozen=True)
class CopyPlan:
    gps: bool
    create_time: bool


def check_exiftool(path: str) -> str:
    resolved = shutil.which(path)
    if resolved:
        return resolved
    raise FileNotFoundError(
        "ExifTool is required but was not found on PATH. Install it with "
        "`sudo apt install libimage-exiftool-perl`, `brew install exiftool`, "
        "or pass --exiftool /path/to/exiftool."
    )


def run_exiftool_json(exiftool: str, path: Path) -> dict[str, Any]:
    tags = [
        "-SourceFile",
        "-GPSLatitude",
        "-GPSLongitude",
        "-GPSPosition",
        "-DateTimeOriginal",
        "-CreateDate",
        "-SubSecDateTimeOriginal",
        "-SubSecCreateDate",
    ]
    completed = subprocess.run(
        [exiftool, "-j", "-n", *tags, str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ExifTool metadata read failed")
    rows = json.loads(completed.stdout)
    if not rows:
        raise RuntimeError(f"ExifTool returned no metadata for {path}")
    return rows[0]


def has_gps(metadata: dict[str, Any]) -> bool:
    return metadata.get("GPSLatitude") is not None and metadata.get("GPSLongitude") is not None


def has_create_time(metadata: dict[str, Any]) -> bool:
    return any(metadata.get(tag) for tag in CREATE_TIME_TAGS[:4])


def build_plan(source: dict[str, Any], target: dict[str, Any], overwrite: bool) -> CopyPlan:
    copy_gps = overwrite or not has_gps(target)
    copy_create_time = overwrite or not has_create_time(target)
    if copy_gps and not has_gps(source):
        copy_gps = False
    if copy_create_time and not has_create_time(source):
        copy_create_time = False
    return CopyPlan(gps=copy_gps, create_time=copy_create_time)


def copy_metadata(
    exiftool: str,
    source: Path,
    target: Path,
    plan: CopyPlan,
    overwrite_original: bool,
) -> None:
    args = [exiftool, "-P", "-TagsFromFile", str(source)]

    if plan.gps:
        args.extend(f"-{tag}" for tag in GPS_TAGS)
    if plan.create_time:
        args.extend(f"-{tag}" for tag in CREATE_TIME_TAGS)

    # Keep common XMP timestamps aligned for PNG and other XMP-heavy formats.
    if plan.create_time:
        args.extend(
            [
                "-XMP:DateCreated<DateTimeOriginal",
                "-XMP:CreateDate<CreateDate",
                "-XMP:ModifyDate<ModifyDate",
            ]
        )

    if overwrite_original:
        args.append("-overwrite_original")
    args.append(str(target))

    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())


def print_plan(source: Path, target: Path, plan: CopyPlan, source_meta: dict[str, Any]) -> None:
    print(f"Source: {source}")
    print(f"Target: {target}")
    print()
    if plan.gps:
        print(
            "Will copy GPS: "
            f"{source_meta.get('GPSLatitude')}, {source_meta.get('GPSLongitude')}"
        )
    else:
        print("Will not copy GPS.")

    if plan.create_time:
        create = (
            source_meta.get("DateTimeOriginal")
            or source_meta.get("SubSecDateTimeOriginal")
            or source_meta.get("CreateDate")
            or source_meta.get("SubSecCreateDate")
        )
        print(f"Will copy create time: {create}")
    else:
        print("Will not copy create time.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", type=Path, help="Photo A: metadata source.")
    parser.add_argument("target", type=Path, help="Photo B: metadata target.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be copied without writing target metadata.",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Copy GPS and create-time metadata even when target already has them.",
    )
    parser.add_argument(
        "--overwrite-original",
        action="store_true",
        help="Tell ExifTool not to keep a *_original backup file.",
    )
    parser.add_argument(
        "--exiftool",
        default="exiftool",
        help="ExifTool executable path. Default: exiftool.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if not args.source.is_file():
            raise ValueError(f"Source file not found: {args.source}")
        if not args.target.is_file():
            raise ValueError(f"Target file not found: {args.target}")

        exiftool = check_exiftool(args.exiftool)
        source_meta = run_exiftool_json(exiftool, args.source)
        target_meta = run_exiftool_json(exiftool, args.target)
        plan = build_plan(source_meta, target_meta, overwrite=args.overwrite_existing)
        print_plan(args.source, args.target, plan, source_meta)

        if not plan.gps and not plan.create_time:
            print("\nNothing to copy.")
            return 0
        if args.dry_run:
            print("\nDry run only; target was not modified.")
            return 0

        copy_metadata(
            exiftool,
            args.source,
            args.target,
            plan,
            overwrite_original=args.overwrite_original,
        )
        copied = []
        if plan.gps:
            copied.append("GPS")
        if plan.create_time:
            copied.append("create time")
        print(f"\nCopied {', '.join(copied)} metadata.")
        return 0
    except (FileNotFoundError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
