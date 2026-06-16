#!/usr/bin/env python3
"""Geotag a folder of photos and videos from nearby geotagged media.

The tool scans a folder, builds a time/GPS table from media that already has
GPS metadata, then assigns the closest timestamped GPS point to media without
GPS. By default it only writes CSV reports. Pass --write to write the matched
GPS metadata back into the missing-GPS files.

Examples:

    ./tools/geotag_folders.py /media/usb/photos
    ./tools/geotag_folders.py /media/usb/photos --max-time-delta 2h
    ./tools/geotag_folders.py /media/usb/photos --write --overwrite-original
    ./tools/geotag_folders.py /media/usb/photos --photo-timezone America/Los_Angeles

ExifTool is required:

    sudo apt install libimage-exiftool-perl
    brew install exiftool
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PHOTO_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".avif",
    ".bmp",
    ".cr2",
    ".cr3",
    ".crw",
    ".dng",
    ".erf",
    ".gif",
    ".heic",
    ".heif",
    ".iiq",
    ".jpeg",
    ".jpg",
    ".jxl",
    ".mef",
    ".mos",
    ".mrw",
    ".nef",
    ".orf",
    ".pef",
    ".png",
    ".raf",
    ".raw",
    ".rw2",
    ".sr2",
    ".srf",
    ".tif",
    ".tiff",
    ".webp",
    ".x3f",
}

VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".avi",
    ".insv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mod",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".tod",
    ".webm",
    ".wmv",
}

MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

CAPTURE_TIME_TAGS = (
    "SubSecDateTimeOriginal",
    "DateTimeOriginal",
    "SubSecCreateDate",
    "CreateDate",
    "CreationDate",
    "MediaCreateDate",
    "TrackCreateDate",
    "DateTimeCreated",
)

GPS_TEXT_TAGS = (
    "GPSCoordinates",
    "GPSPosition",
    "LocationInformation",
)


@dataclass(frozen=True)
class MediaItem:
    path: Path
    is_video: bool
    capture_time: datetime | None
    timestamp_tag: str | None
    lat: float | None
    lon: float | None
    ele: float | None
    gps_source_tag: str | None
    message: str = ""


@dataclass(frozen=True)
class GpsPoint:
    path: Path
    time: datetime
    lat: float
    lon: float
    ele: float | None


@dataclass
class MatchResult:
    path: Path
    status: str
    capture_time: datetime | None = None
    source_path: Path | None = None
    source_time: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    ele: float | None = None
    delta_seconds: float | None = None
    message: str = ""


def parse_time_delta_seconds(value: str) -> float:
    """Parse deltas such as 7200, 2h, 1h30m, or 01:30:00."""
    text = str(value).strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("time delta cannot be empty")

    sign = 1.0
    if text[0] in "+-":
        if text[0] == "-":
            sign = -1.0
        text = text[1:].strip()

    text = re.sub(r"[\s,_]+", "", text)
    if not text:
        raise argparse.ArgumentTypeError(f"invalid time delta: {value!r}")

    if ":" in text:
        parts = text.split(":")
        if len(parts) not in {2, 3}:
            raise argparse.ArgumentTypeError("time delta must be MM:SS or HH:MM:SS")
        try:
            numbers = [float(part) for part in parts]
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid time delta: {value!r}") from exc
        if len(numbers) == 2:
            minutes, seconds = numbers
            return sign * (minutes * 60 + seconds)
        hours, minutes, seconds = numbers
        return sign * (hours * 3600 + minutes * 60 + seconds)

    unit_pattern = (
        r"(\d+(?:\.\d+)?)(hours|hour|hrs|hr|h|minutes|minute|mins|min|m|seconds|second|secs|sec|s)"
    )
    unit_matches = list(re.finditer(unit_pattern, text))
    if unit_matches and "".join(match.group(0) for match in unit_matches) == text:
        total = 0.0
        for match in unit_matches:
            amount = float(match.group(1))
            unit = match.group(2)
            if unit.startswith("h"):
                total += amount * 3600
            elif unit.startswith("m"):
                total += amount * 60
            else:
                total += amount
        return sign * total

    try:
        return sign * float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "time delta must be seconds, MM:SS, HH:MM:SS, or unit form like 2h"
        ) from exc


def normalize_time_delta_args(argv: list[str]) -> list[str]:
    """Allow `--photo-time-diff -30m` without requiring an equals sign."""
    normalized: list[str] = []
    index = 0
    time_delta_flags = {"--photo-time-diff", "--camera-time-offset"}
    while index < len(argv):
        arg = argv[index]
        if (
            arg in time_delta_flags
            and index + 1 < len(argv)
            and argv[index + 1].startswith("-")
            and not argv[index + 1].startswith("--")
        ):
            normalized.append(f"{arg}={argv[index + 1]}")
            index += 2
            continue
        normalized.append(arg)
        index += 1
    return normalized


def timezone_from_name(name: str | None) -> tzinfo:
    if not name:
        tz = datetime.now().astimezone().tzinfo
        return tz if tz is not None else timezone.utc
    if name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {name}") from exc


def parse_exif_datetime(
    value: str,
    default_tz: tzinfo,
    offset_seconds: float,
) -> datetime | None:
    text = str(value).strip()
    if not text:
        return None

    # ExifTool commonly emits "2024:03:04 12:13:14-05:00".
    if len(text) >= 19 and text[4] == ":" and text[7] == ":":
        text = f"{text[:4]}-{text[5:7]}-{text[8:]}"
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    candidates = [text]
    if " " in text:
        candidates.append(text.replace(" ", "T", 1))

    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=default_tz)
            return (dt + timedelta(seconds=offset_seconds)).astimezone(timezone.utc)
        except ValueError:
            continue

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text[:19], fmt).replace(tzinfo=default_tz)
            return (dt + timedelta(seconds=offset_seconds)).astimezone(timezone.utc)
        except ValueError:
            continue

    return None


def number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def coordinates_from_text(value: Any) -> tuple[float, float, float | None] | None:
    if value is None:
        return None
    numbers = re.findall(r"[+-]?\d+(?:\.\d+)?", str(value))
    if len(numbers) < 2:
        return None
    lat = float(numbers[0])
    lon = float(numbers[1])
    ele = float(numbers[2]) if len(numbers) >= 3 else None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon, ele


def gps_from_metadata(metadata: dict[str, Any]) -> tuple[float, float, float | None, str | None]:
    lat = number_or_none(metadata.get("GPSLatitude"))
    lon = number_or_none(metadata.get("GPSLongitude"))
    ele = number_or_none(metadata.get("GPSAltitude"))
    if lat is not None and lon is not None:
        return lat, lon, ele, "GPSLatitude/GPSLongitude"

    for tag in GPS_TEXT_TAGS:
        parsed = coordinates_from_text(metadata.get(tag))
        if parsed is None:
            continue
        lat, lon, parsed_ele = parsed
        return lat, lon, ele if ele is not None else parsed_ele, tag

    return None, None, None, None


def capture_time_from_metadata(
    metadata: dict[str, Any],
    default_tz: tzinfo,
    offset_seconds: float,
    use_file_mtime: bool,
) -> tuple[datetime | None, str | None]:
    tags = list(CAPTURE_TIME_TAGS)
    if use_file_mtime:
        tags.append("FileModifyDate")

    for tag in tags:
        value = metadata.get(tag)
        if not value:
            continue
        parsed = parse_exif_datetime(str(value), default_tz, offset_seconds)
        if parsed is not None:
            return parsed, tag

    return None, None


def check_exiftool(path: str) -> str:
    resolved = shutil.which(path)
    if resolved:
        return resolved
    raise FileNotFoundError(
        "ExifTool is required but was not found on PATH. Install it with "
        "`sudo apt install libimage-exiftool-perl`, `brew install exiftool`, "
        "or pass --exiftool /path/to/exiftool."
    )


def collect_media_paths(folder: Path, recursive: bool, all_files: bool) -> list[Path]:
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    paths: list[Path] = []
    for path in iterator:
        if not path.is_file():
            continue
        if all_files or path.suffix.lower() in MEDIA_EXTENSIONS:
            paths.append(path)
    return sorted(paths)


def run_exiftool_json(exiftool: str, paths: list[Path]) -> list[dict[str, Any]]:
    tags = [
        "-SourceFile",
        "-FileType",
        "-MIMEType",
        "-GPSLatitude",
        "-GPSLongitude",
        "-GPSAltitude",
        "-GPSCoordinates",
        "-GPSPosition",
        "-LocationInformation",
        "-SubSecDateTimeOriginal",
        "-DateTimeOriginal",
        "-SubSecCreateDate",
        "-CreateDate",
        "-CreationDate",
        "-MediaCreateDate",
        "-TrackCreateDate",
        "-DateTimeCreated",
        "-FileModifyDate",
    ]
    rows: list[dict[str, Any]] = []
    for start in range(0, len(paths), 100):
        chunk = paths[start : start + 100]
        command = [exiftool, "-j", "-n", "-charset", "filename=UTF8", *tags, *map(str, chunk)]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "ExifTool metadata read failed")
        rows.extend(json.loads(completed.stdout))
    return rows


def build_media_items(args: argparse.Namespace, paths: list[Path]) -> list[MediaItem]:
    exiftool = check_exiftool(args.exiftool)
    default_tz = timezone_from_name(args.photo_timezone)
    time_offset_seconds = args.photo_time_offset + args.photo_time_diff
    metadata_rows = run_exiftool_json(exiftool, paths)
    metadata_by_path = {
        Path(row["SourceFile"]).resolve(): row
        for row in metadata_rows
        if row.get("SourceFile")
    }

    items: list[MediaItem] = []
    for path in paths:
        metadata = metadata_by_path.get(path.resolve())
        if metadata is None:
            items.append(MediaItem(path, path.suffix.lower() in VIDEO_EXTENSIONS, None, None, None, None, None, None, "No ExifTool metadata row"))
            continue

        capture_time, timestamp_tag = capture_time_from_metadata(
            metadata,
            default_tz=default_tz,
            offset_seconds=time_offset_seconds,
            use_file_mtime=args.use_file_mtime,
        )
        lat, lon, ele, gps_source_tag = gps_from_metadata(metadata)
        items.append(
            MediaItem(
                path=path,
                is_video=path.suffix.lower() in VIDEO_EXTENSIONS,
                capture_time=capture_time,
                timestamp_tag=timestamp_tag,
                lat=lat,
                lon=lon,
                ele=ele,
                gps_source_tag=gps_source_tag,
            )
        )
    return items


def gps_points_from_items(items: list[MediaItem]) -> list[GpsPoint]:
    points = [
        GpsPoint(item.path, item.capture_time, item.lat, item.lon, item.ele)
        for item in items
        if item.capture_time is not None and item.lat is not None and item.lon is not None
    ]
    return sorted(points, key=lambda point: point.time)


def find_nearest_gps(
    points: list[GpsPoint],
    capture_time: datetime,
    max_time_delta: float | None,
) -> tuple[GpsPoint | None, float | None]:
    if not points:
        return None, None

    times = [point.time for point in points]
    index = bisect.bisect_left(times, capture_time)
    candidates = []
    if index > 0:
        candidates.append(points[index - 1])
    if index < len(points):
        candidates.append(points[index])

    nearest = min(
        candidates,
        key=lambda point: abs((capture_time - point.time).total_seconds()),
    )
    delta_seconds = abs((capture_time - nearest.time).total_seconds())
    if max_time_delta is not None and delta_seconds > max_time_delta:
        return None, delta_seconds
    return nearest, delta_seconds


def latitude_ref(lat: float) -> str:
    return "N" if lat >= 0 else "S"


def longitude_ref(lon: float) -> str:
    return "E" if lon >= 0 else "W"


def quicktime_coordinates(lat: float, lon: float, ele: float | None) -> str:
    if ele is None:
        return f"{lat:.8f} {lon:.8f}"
    return f"{lat:.8f} {lon:.8f} {ele:.3f}"


def write_gps(
    exiftool: str,
    item: MediaItem,
    point: GpsPoint,
    overwrite_original: bool,
) -> None:
    gps_time = point.time.astimezone(timezone.utc)
    command = [
        exiftool,
        "-P",
        "-m",
        f"-XMP:GPSLatitude={point.lat:.8f}",
        f"-XMP:GPSLongitude={point.lon:.8f}",
    ]

    if point.ele is not None:
        command.append(f"-XMP:GPSAltitude={point.ele:.3f}")

    if item.is_video:
        coords = quicktime_coordinates(point.lat, point.lon, point.ele)
        command.extend(
            [
                f"-Keys:GPSCoordinates={coords}",
                f"-UserData:GPSCoordinates={coords}",
            ]
        )
    else:
        command.extend(
            [
                f"-GPSLatitude={abs(point.lat):.8f}",
                f"-GPSLatitudeRef={latitude_ref(point.lat)}",
                f"-GPSLongitude={abs(point.lon):.8f}",
                f"-GPSLongitudeRef={longitude_ref(point.lon)}",
                "-GPSMapDatum=WGS-84",
                f"-GPSDateStamp={gps_time:%Y:%m:%d}",
                f"-GPSTimeStamp={gps_time:%H:%M:%S}",
            ]
        )
        if point.ele is not None:
            command.extend(
                [
                    f"-GPSAltitude={abs(point.ele):.3f}",
                    f"-GPSAltitudeRef={'0' if point.ele >= 0 else '1'}",
                ]
            )

    if overwrite_original:
        command.append("-overwrite_original")
    command.append(str(item.path))

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())


def build_matches(args: argparse.Namespace, items: list[MediaItem]) -> list[MatchResult]:
    gps_points = gps_points_from_items(items)
    exiftool = check_exiftool(args.exiftool) if args.write else ""
    results: list[MatchResult] = []

    for item in items:
        if item.lat is not None and item.lon is not None:
            continue

        if item.capture_time is None:
            results.append(
                MatchResult(
                    path=item.path,
                    status="skipped-no-timestamp",
                    message=item.message or "No capture timestamp metadata",
                )
            )
            continue

        if not gps_points:
            results.append(
                MatchResult(
                    path=item.path,
                    status="skipped-no-gps-table",
                    capture_time=item.capture_time,
                    message="No timestamped GPS media found",
                )
            )
            continue

        nearest, delta_seconds = find_nearest_gps(
            gps_points,
            item.capture_time,
            args.max_time_delta,
        )
        if nearest is None:
            results.append(
                MatchResult(
                    path=item.path,
                    status="skipped-too-far",
                    capture_time=item.capture_time,
                    delta_seconds=delta_seconds,
                    message=f"Closest GPS point is farther than {args.max_time_delta:.1f}s",
                )
            )
            continue

        status = "matched"
        message = ""
        if args.write:
            try:
                write_gps(
                    exiftool,
                    item,
                    nearest,
                    overwrite_original=args.overwrite_original,
                )
                status = "written"
            except RuntimeError as exc:
                status = "error"
                message = str(exc)

        results.append(
            MatchResult(
                path=item.path,
                status=status,
                capture_time=item.capture_time,
                source_path=nearest.path,
                source_time=nearest.time,
                lat=nearest.lat,
                lon=nearest.lon,
                ele=nearest.ele,
                delta_seconds=delta_seconds,
                message=message,
            )
        )

    return results


def relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def format_time(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def format_float(value: float | None, precision: int = 8) -> str:
    return f"{value:.{precision}f}" if value is not None else ""


def write_gps_table(path: Path, folder: Path, items: list[MediaItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    gps_items = [item for item in items if item.lat is not None and item.lon is not None]
    gps_items.sort(key=lambda item: (item.capture_time or datetime.max.replace(tzinfo=timezone.utc), item.path.name))

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "path",
                "capture_time_utc",
                "latitude",
                "longitude",
                "elevation_m",
                "timestamp_tag",
                "gps_source_tag",
                "usable_for_matching",
            ]
        )
        for item in gps_items:
            writer.writerow(
                [
                    relative_path(item.path, folder),
                    format_time(item.capture_time),
                    format_float(item.lat),
                    format_float(item.lon),
                    format_float(item.ele, 3),
                    item.timestamp_tag or "",
                    item.gps_source_tag or "",
                    "yes" if item.capture_time is not None else "no",
                ]
            )


def write_match_table(path: Path, folder: Path, results: list[MatchResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "path",
                "status",
                "capture_time_utc",
                "matched_latitude",
                "matched_longitude",
                "matched_elevation_m",
                "source_path",
                "source_time_utc",
                "delta_seconds",
                "message",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    relative_path(result.path, folder),
                    result.status,
                    format_time(result.capture_time),
                    format_float(result.lat),
                    format_float(result.lon),
                    format_float(result.ele, 3),
                    relative_path(result.source_path, folder) if result.source_path else "",
                    format_time(result.source_time),
                    format_float(result.delta_seconds, 1),
                    result.message,
                ]
            )


def default_output_dir(folder: Path) -> Path:
    return folder / "geotag_reports"


def print_summary(
    items: list[MediaItem],
    gps_table_path: Path,
    match_table_path: Path,
    matches: list[MatchResult],
) -> None:
    gps_count = sum(1 for item in items if item.lat is not None and item.lon is not None)
    usable_gps_count = sum(
        1
        for item in items
        if item.lat is not None and item.lon is not None and item.capture_time is not None
    )
    missing_gps_count = len(items) - gps_count

    counts: dict[str, int] = {}
    for result in matches:
        counts[result.status] = counts.get(result.status, 0) + 1

    print("Summary:")
    print(f"  scanned media: {len(items)}")
    print(f"  media with GPS: {gps_count}")
    print(f"  timestamped GPS rows usable for matching: {usable_gps_count}")
    print(f"  media missing GPS: {missing_gps_count}")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")

    print()
    print(f"GPS table: {gps_table_path}")
    print(f"Missing-GPS match table: {match_table_path}")

    preview = [result for result in matches if result.status in {"matched", "written", "error"}]
    if preview:
        print()
        for result in preview[:20]:
            if result.lat is not None and result.lon is not None:
                print(
                    f"{result.status:8} {result.path} -> "
                    f"{result.lat:.6f},{result.lon:.6f} "
                    f"delta={result.delta_seconds:.1f}s"
                )
            else:
                print(f"{result.status:8} {result.path} {result.message}")
        if len(preview) > 20:
            print(f"... {len(preview) - 20} more rows omitted; see CSV for all rows.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("folder", type=Path, help="Folder containing photos and videos.")
    parser.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        default=True,
        help="Only scan the top level of the folder. Default is recursive.",
    )
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="Send every regular file to ExifTool instead of filtering common media extensions.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Folder for CSV reports. Default: <folder>/geotag_reports.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write matched GPS metadata into files that are missing GPS. Default only reports.",
    )
    parser.add_argument(
        "--overwrite-original",
        action="store_true",
        help="Tell ExifTool not to keep *_original backup files when --write is used.",
    )
    parser.add_argument(
        "--photo-timezone",
        help=(
            "Timezone for timestamps without an offset, e.g. America/New_York. "
            "Defaults to the local machine timezone."
        ),
    )
    parser.add_argument(
        "--photo-time-offset",
        type=float,
        default=0.0,
        help="Seconds to add to each media timestamp before matching.",
    )
    parser.add_argument(
        "--photo-time-diff",
        "--camera-time-offset",
        dest="photo_time_diff",
        type=parse_time_delta_seconds,
        default=0.0,
        metavar="DELTA",
        help=(
            "Friendly timestamp offset to add before matching, e.g. +1:00:00, "
            "-30m, 1h15m, or 5400."
        ),
    )
    parser.add_argument(
        "--max-time-delta",
        type=parse_time_delta_seconds,
        default=None,
        metavar="DELTA",
        help=(
            "Skip matches when the closest GPS timestamp is farther away than DELTA. "
            "Examples: 300, 15m, 2h. Default: no limit."
        ),
    )
    parser.add_argument(
        "--use-file-mtime",
        action="store_true",
        help="Fallback to file modification time when no capture timestamp exists.",
    )
    parser.add_argument(
        "--exiftool",
        default="exiftool",
        help="ExifTool executable path. Default: exiftool.",
    )
    return parser.parse_args(normalize_time_delta_args(argv))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if not args.folder.is_dir():
            raise ValueError(f"folder not found: {args.folder}")

        paths = collect_media_paths(args.folder, args.recursive, args.all_files)
        if not paths:
            raise ValueError(f"no media files found in {args.folder}")

        items = build_media_items(args, paths)
        matches = build_matches(args, items)

        output_dir = args.output_dir or default_output_dir(args.folder)
        gps_table_path = output_dir / "gps_table.csv"
        match_table_path = output_dir / "missing_gps_matches.csv"
        write_gps_table(gps_table_path, args.folder, items)
        write_match_table(match_table_path, args.folder, matches)
        print_summary(items, gps_table_path, match_table_path, matches)
        return 0
    except (FileNotFoundError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
