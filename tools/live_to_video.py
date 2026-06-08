#!/usr/bin/env python3
"""Copy a media folder while converting Live Photos to video files.

Given source folder A and destination folder B:

- Regular videos are copied to B unchanged.
- Regular non-live photos and other files are copied to B unchanged.
- iPhone Live Photos are detected by a same-stem video sidecar, such as
  IMG_1234.HEIC + IMG_1234.MOV. The sidecar is remuxed to IMG_1234.mp4 in B.
- Android motion photos are detected by embedded video data in the image. The
  embedded video is extracted to a same-stem .mp4 in B.

By default, iPhone sidecar videos consumed by a Live Photo still are not copied
again as separate files. Use --copy-live-sidecars to keep those originals too.

Examples:

    ./tools/live_to_video.py ./wechat_export ./cleaned_export --dry-run
    ./tools/live_to_video.py ./wechat_export ./cleaned_export
    ./tools/live_to_video.py ./wechat_export ./cleaned_export --recursive --overwrite
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".png",
    ".webp",
    ".dng",
    ".tif",
    ".tiff",
}
VIDEO_EXTENSIONS = {
    ".mov",
    ".mp4",
    ".m4v",
    ".3gp",
    ".3g2",
    ".avi",
    ".mkv",
    ".webm",
}
IPHONE_SIDECAR_PRIORITY = {".mov": 0, ".m4v": 1, ".mp4": 2}


@dataclass
class Summary:
    copied: int = 0
    converted_iphone: int = 0
    converted_android: int = 0
    skipped_sidecars: int = 0
    skipped_existing: int = 0
    errors: int = 0


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def iter_files(folder: Path, recursive: bool) -> list[Path]:
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(path for path in iterator if path.is_file())


def media_key(path: Path) -> tuple[Path, str]:
    return path.parent.resolve(), path.stem.lower()


def build_iphone_sidecar_map(files: list[Path]) -> dict[Path, Path]:
    videos_by_key: dict[tuple[Path, str], list[Path]] = {}
    for path in files:
        if is_video(path):
            videos_by_key.setdefault(media_key(path), []).append(path)

    result: dict[Path, Path] = {}
    for path in files:
        if not is_image(path):
            continue
        sidecars = videos_by_key.get(media_key(path), [])
        if not sidecars:
            continue
        result[path] = sorted(
            sidecars,
            key=lambda item: (IPHONE_SIDECAR_PRIORITY.get(item.suffix.lower(), 99), item.name.lower()),
        )[0]
    return result


def target_for(source_root: Path, dest_root: Path, source: Path) -> Path:
    return dest_root / source.relative_to(source_root)


def video_target_for(source_root: Path, dest_root: Path, source: Path) -> Path:
    return target_for(source_root, dest_root, source).with_suffix(".mp4")


def ensure_parent(path: Path, dry_run: bool) -> None:
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)


def target_exists(target: Path, overwrite: bool) -> bool:
    return target.exists() and not overwrite


def copy_file(source: Path, target: Path, args: argparse.Namespace, summary: Summary, reason: str) -> None:
    if target_exists(target, args.overwrite):
        print(f"exists: {target} ({reason})")
        summary.skipped_existing += 1
        return
    action = "would copy" if args.dry_run else "copying"
    print(f"{action}: {source} -> {target} ({reason})")
    ensure_parent(target, args.dry_run)
    if not args.dry_run:
        shutil.copy2(source, target)
    summary.copied += 1


def remux_video(source: Path, target: Path, args: argparse.Namespace) -> None:
    ensure_parent(target, args.dry_run)
    if args.dry_run:
        return
    ffmpeg = shutil.which(args.ffmpeg)
    if not ffmpeg:
        raise RuntimeError(f"ffmpeg not found: {args.ffmpeg}")

    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(target),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode == 0:
        return

    fallback = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(target),
    ]
    completed = subprocess.run(fallback, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "ffmpeg failed"
        raise RuntimeError(message)


def convert_iphone_live_photo(
    still: Path,
    sidecar: Path,
    target: Path,
    args: argparse.Namespace,
    summary: Summary,
) -> None:
    if target_exists(target, args.overwrite):
        print(f"exists: {target} (iPhone Live Photo video)")
        summary.skipped_existing += 1
        return
    action = "would convert" if args.dry_run else "converting"
    print(f"{action}: {still} + {sidecar.name} -> {target} (iPhone Live Photo)")
    remux_video(sidecar, target, args)
    summary.converted_iphone += 1


def exiftool_extract_embedded_video(source: Path, target: Path, args: argparse.Namespace) -> bool:
    exiftool = shutil.which(args.exiftool)
    if not exiftool:
        return False

    with tempfile.NamedTemporaryFile(prefix="live-to-video-", suffix=".mp4", delete=False) as handle:
        tmp = Path(handle.name)
        completed = subprocess.run(
            [exiftool, "-b", "-EmbeddedVideoFile", str(source)],
            check=False,
            stdout=handle,
            stderr=subprocess.PIPE,
            text=False,
        )

    if completed.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        if args.dry_run:
            tmp.unlink(missing_ok=True)
        else:
            ensure_parent(target, args.dry_run)
            shutil.move(str(tmp), target)
        return True

    tmp.unlink(missing_ok=True)
    return False


def find_mp4_payload_offset(source: Path) -> int | None:
    data = source.read_bytes()
    index = data.rfind(b"ftyp")
    if index < 4:
        return None
    start = index - 4
    box_size = int.from_bytes(data[start:index], byteorder="big", signed=False)
    if box_size < 8 or start + box_size > len(data):
        return None
    return start


def trailer_extract_embedded_video(source: Path, target: Path, args: argparse.Namespace) -> bool:
    offset = find_mp4_payload_offset(source)
    if offset is None:
        return False
    if args.dry_run:
        return True
    ensure_parent(target, args.dry_run)
    with source.open("rb") as source_handle, target.open("wb") as target_handle:
        source_handle.seek(offset)
        shutil.copyfileobj(source_handle, target_handle)
    return True


def convert_android_motion_photo(
    source: Path,
    target: Path,
    args: argparse.Namespace,
    summary: Summary,
) -> bool:
    if target_exists(target, args.overwrite):
        print(f"exists: {target} (Android motion photo video)")
        summary.skipped_existing += 1
        return True

    if exiftool_extract_embedded_video(source, target, args):
        action = "would extract" if args.dry_run else "extracted"
        print(f"{action}: {source} -> {target} (Android motion photo)")
        summary.converted_android += 1
        return True

    if trailer_extract_embedded_video(source, target, args):
        action = "would extract" if args.dry_run else "extracted"
        print(f"{action}: {source} -> {target} (Android motion photo trailer)")
        summary.converted_android += 1
        return True

    return False


def process(args: argparse.Namespace) -> Summary:
    source_root = args.source.resolve()
    dest_root = args.dest.resolve()
    if source_root == dest_root:
        raise ValueError("source and destination folders must be different")
    if not source_root.is_dir():
        raise ValueError(f"source folder not found: {args.source}")
    if not args.dry_run:
        dest_root.mkdir(parents=True, exist_ok=True)

    files = iter_files(source_root, args.recursive)
    sidecars_by_still = build_iphone_sidecar_map(files)
    consumed_sidecars = set(sidecars_by_still.values())
    summary = Summary()

    for source in files:
        try:
            target = target_for(source_root, dest_root, source)
            if source in consumed_sidecars and not args.copy_live_sidecars:
                print(f"skipping: {source} (iPhone Live Photo sidecar consumed by still)")
                summary.skipped_sidecars += 1
                continue

            sidecar = sidecars_by_still.get(source)
            if sidecar is not None:
                convert_iphone_live_photo(
                    source,
                    sidecar,
                    video_target_for(source_root, dest_root, source),
                    args,
                    summary,
                )
                continue

            if is_image(source):
                motion_target = video_target_for(source_root, dest_root, source)
                if convert_android_motion_photo(source, motion_target, args, summary):
                    continue

            copy_file(
                source,
                target,
                args,
                summary,
                "video" if is_video(source) else "not live photo",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"error: {source}: {exc}", file=sys.stderr)
            summary.errors += 1

    return summary


def print_summary(summary: Summary, dry_run: bool) -> None:
    prefix = "would " if dry_run else ""
    print("Summary:")
    print(f"  {prefix}copy regular files: {summary.copied}")
    print(f"  {prefix}convert iPhone Live Photos: {summary.converted_iphone}")
    print(f"  {prefix}convert Android motion photos: {summary.converted_android}")
    print(f"  skipped consumed iPhone sidecars: {summary.skipped_sidecars}")
    print(f"  skipped existing outputs: {summary.skipped_existing}")
    print(f"  errors: {summary.errors}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Source folder A.")
    parser.add_argument("dest", type=Path, help="Destination folder B.")
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Scan source subfolders recursively and preserve relative paths in B.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned copies/conversions without writing files.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing destination files.",
    )
    parser.add_argument(
        "--copy-live-sidecars",
        action="store_true",
        help="Also copy iPhone Live Photo sidecar videos after creating same-stem .mp4 outputs.",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable path.")
    parser.add_argument("--exiftool", default="exiftool", help="ExifTool executable path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        summary = process(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print_summary(summary, args.dry_run)
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
