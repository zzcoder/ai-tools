#!/usr/bin/env python3
"""Geotag photos from a GPX or KML track.

The tool reads capture timestamps from photos, estimates a location from the
track at that time, and writes GPS metadata back into photos that do not
already have GPS tags.

Examples:

    python3 tools/geolocate.py hike.gpx ./photos --dry-run
    python3 tools/geolocate.py hike.kml ./photos --photo-timezone America/New_York
    python3 tools/geolocate.py hike.gpx ./photos --photo-time-diff +1:00:00
    python3 tools/geolocate.py hike.gpx ./photos --overwrite-original

ExifTool is required for metadata reading and writing:

    sudo apt install libimage-exiftool-perl
    brew install exiftool
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".nef", ".dng"}
CAPTURE_TIME_TAGS = (
    "SubSecDateTimeOriginal",
    "DateTimeOriginal",
    "SubSecCreateDate",
    "CreateDate",
    "DateTimeCreated",
)


@dataclass(frozen=True)
class TrackPoint:
    time: datetime
    lat: float
    lon: float
    ele: float | None = None


@dataclass(frozen=True)
class EstimatedLocation:
    time: datetime
    lat: float
    lon: float
    ele: float | None
    nearest_delta_seconds: float
    method: str


@dataclass
class PhotoResult:
    path: Path
    status: str
    capture_time: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    ele: float | None = None
    delta_seconds: float | None = None
    method: str | None = None
    message: str = ""


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def child_text(element: ET.Element, name: str) -> str | None:
    for child in list(element):
        if local_name(child.tag) == name and child.text:
            return child.text.strip()
    return None


def descendant_text(element: ET.Element, name: str) -> str | None:
    for child in element.iter():
        if local_name(child.tag) == name and child.text:
            return child.text.strip()
    return None


def parse_xml_time(value: str, default_tz: timezone = timezone.utc) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    if len(text) >= 19 and text[4] == ":" and text[7] == ":":
        text = f"{text[:4]}-{text[5:7]}-{text[8:]}"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(timezone.utc)


def parse_exif_datetime(
    value: str,
    default_tz: timezone,
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


def parse_time_delta_seconds(value: str) -> float:
    """Parse user-facing time deltas such as +1:30:00, -45m, or 3600."""
    text = str(value).strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("time difference cannot be empty")

    sign = 1.0
    if text[0] in "+-":
        if text[0] == "-":
            sign = -1.0
        text = text[1:].strip()
    if not text:
        raise argparse.ArgumentTypeError(f"invalid time difference: {value!r}")
    text = re.sub(r"[\s,_]+", "", text)
    if not text:
        raise argparse.ArgumentTypeError(f"invalid time difference: {value!r}")

    if ":" in text:
        parts = text.split(":")
        if len(parts) not in {2, 3}:
            raise argparse.ArgumentTypeError(
                "colon time differences must be HH:MM or HH:MM:SS"
            )
        try:
            numbers = [float(part) for part in parts]
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid time difference: {value!r}") from exc
        if len(numbers) == 2:
            hours, minutes = numbers
            seconds = 0.0
        else:
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
            "time difference must be seconds, HH:MM[:SS], or unit form like 1h30m"
        ) from exc


def normalize_time_delta_args(argv: list[str]) -> list[str]:
    """Allow `--photo-time-diff -30m` without requiring `--photo-time-diff=-30m`."""
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


def parse_gpx(path: Path) -> list[TrackPoint]:
    root = ET.parse(path).getroot()
    points: list[TrackPoint] = []
    for element in root.iter():
        if local_name(element.tag) not in {"trkpt", "rtept", "wpt"}:
            continue
        lat = element.attrib.get("lat")
        lon = element.attrib.get("lon")
        time_text = child_text(element, "time")
        if not lat or not lon or not time_text:
            continue
        ele_text = child_text(element, "ele")
        points.append(
            TrackPoint(
                time=parse_xml_time(time_text),
                lat=float(lat),
                lon=float(lon),
                ele=float(ele_text) if ele_text else None,
            )
        )
    return sorted(points, key=lambda point: point.time)


def parse_kml_coordinates(value: str) -> tuple[float, float, float | None] | None:
    first_coord = value.strip().split()[0]
    parts = first_coord.split(",")
    if len(parts) < 2:
        return None
    lon = float(parts[0])
    lat = float(parts[1])
    ele = float(parts[2]) if len(parts) > 2 and parts[2] else None
    return lat, lon, ele


def parse_gx_coord(value: str) -> tuple[float, float, float | None] | None:
    parts = value.strip().split()
    if len(parts) < 2:
        return None
    lon = float(parts[0])
    lat = float(parts[1])
    ele = float(parts[2]) if len(parts) > 2 else None
    return lat, lon, ele


def parse_kml(path: Path) -> list[TrackPoint]:
    root = ET.parse(path).getroot()
    points: list[TrackPoint] = []

    for track in root.iter():
        if local_name(track.tag) != "Track":
            continue
        whens = [
            child.text.strip()
            for child in list(track)
            if local_name(child.tag) == "when" and child.text
        ]
        coords = [
            child.text.strip()
            for child in list(track)
            if local_name(child.tag) == "coord" and child.text
        ]
        for when, coord in zip(whens, coords, strict=False):
            parsed = parse_gx_coord(coord)
            if parsed is None:
                continue
            lat, lon, ele = parsed
            points.append(TrackPoint(parse_xml_time(when), lat, lon, ele))

    for placemark in root.iter():
        if local_name(placemark.tag) != "Placemark":
            continue
        if any(local_name(child.tag) == "Track" for child in placemark.iter()):
            continue
        when = descendant_text(placemark, "when")
        coords = descendant_text(placemark, "coordinates")
        if not when or not coords:
            continue
        parsed = parse_kml_coordinates(coords)
        if parsed is None:
            continue
        lat, lon, ele = parsed
        points.append(TrackPoint(parse_xml_time(when), lat, lon, ele))

    return sorted(points, key=lambda point: point.time)


def parse_track(path: Path) -> list[TrackPoint]:
    suffix = path.suffix.lower()
    if suffix == ".gpx":
        points = parse_gpx(path)
    elif suffix == ".kml":
        points = parse_kml(path)
    else:
        raise ValueError(f"Unsupported track file extension: {path.suffix}")

    deduped: list[TrackPoint] = []
    for point in points:
        if deduped and point.time == deduped[-1].time:
            continue
        deduped.append(point)
    return deduped


def interpolate_location(
    points: list[TrackPoint],
    capture_time: datetime,
    max_time_delta: float,
    max_interpolation_gap: float,
) -> EstimatedLocation | None:
    times = [point.time for point in points]
    index = bisect.bisect_left(times, capture_time)

    before = points[index - 1] if index > 0 else None
    after = points[index] if index < len(points) else None

    candidates = [point for point in (before, after) if point is not None]
    nearest = min(
        candidates,
        key=lambda point: abs((capture_time - point.time).total_seconds()),
        default=None,
    )
    if nearest is None:
        return None

    nearest_delta = abs((capture_time - nearest.time).total_seconds())
    if nearest_delta > max_time_delta:
        return None

    if before and after and before.time <= capture_time <= after.time:
        gap = (after.time - before.time).total_seconds()
        if 0 < gap <= max_interpolation_gap:
            fraction = (capture_time - before.time).total_seconds() / gap
            ele = None
            if before.ele is not None and after.ele is not None:
                ele = before.ele + (after.ele - before.ele) * fraction
            return EstimatedLocation(
                time=capture_time,
                lat=before.lat + (after.lat - before.lat) * fraction,
                lon=before.lon + (after.lon - before.lon) * fraction,
                ele=ele,
                nearest_delta_seconds=nearest_delta,
                method="interpolated",
            )

    return EstimatedLocation(
        time=nearest.time,
        lat=nearest.lat,
        lon=nearest.lon,
        ele=nearest.ele,
        nearest_delta_seconds=nearest_delta,
        method="nearest",
    )


def collect_photo_paths(photo_dir: Path, recursive: bool) -> list[Path]:
    iterator = photo_dir.rglob("*") if recursive else photo_dir.glob("*")
    return sorted(
        path
        for path in iterator
        if path.is_file() and path.suffix.lower() in PHOTO_EXTENSIONS
    )


def run_exiftool_json(exiftool: str, paths: list[Path]) -> list[dict[str, Any]]:
    tags = [
        "-SourceFile",
        "-GPSLatitude",
        "-GPSLongitude",
        "-SubSecDateTimeOriginal",
        "-DateTimeOriginal",
        "-SubSecCreateDate",
        "-CreateDate",
        "-DateTimeCreated",
        "-FileModifyDate",
    ]
    results: list[dict[str, Any]] = []
    for start in range(0, len(paths), 100):
        chunk = paths[start : start + 100]
        command = [exiftool, "-j", "-n", *tags, *map(str, chunk)]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "ExifTool failed")
        results.extend(json.loads(completed.stdout))
    return results


def has_gps(metadata: dict[str, Any]) -> bool:
    return metadata.get("GPSLatitude") is not None and metadata.get("GPSLongitude") is not None


def capture_time_from_metadata(
    metadata: dict[str, Any],
    default_tz: timezone,
    offset_seconds: float,
    use_file_mtime: bool,
) -> datetime | None:
    tags = list(CAPTURE_TIME_TAGS)
    if use_file_mtime:
        tags.append("FileModifyDate")
    for tag in tags:
        value = metadata.get(tag)
        if value:
            parsed = parse_exif_datetime(str(value), default_tz, offset_seconds)
            if parsed is not None:
                return parsed
    return None


def latitude_ref(lat: float) -> str:
    return "N" if lat >= 0 else "S"


def longitude_ref(lon: float) -> str:
    return "E" if lon >= 0 else "W"


def write_gps(
    exiftool: str,
    path: Path,
    location: EstimatedLocation,
    overwrite_original: bool,
) -> None:
    gps_time = location.time.astimezone(timezone.utc)
    command = [
        exiftool,
        "-P",
        f"-GPSLatitude={abs(location.lat):.8f}",
        f"-GPSLatitudeRef={latitude_ref(location.lat)}",
        f"-GPSLongitude={abs(location.lon):.8f}",
        f"-GPSLongitudeRef={longitude_ref(location.lon)}",
        "-GPSMapDatum=WGS-84",
        f"-GPSDateStamp={gps_time:%Y:%m:%d}",
        f"-GPSTimeStamp={gps_time:%H:%M:%S}",
    ]
    if location.ele is not None:
        command.extend(
            [
                f"-GPSAltitude={abs(location.ele):.3f}",
                f"-GPSAltitudeRef={'0' if location.ele >= 0 else '1'}",
            ]
        )
    if overwrite_original:
        command.append("-overwrite_original")
    command.append(str(path))
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())


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


def check_exiftool(path: str) -> str:
    resolved = shutil.which(path)
    if resolved:
        return resolved
    raise FileNotFoundError(
        "ExifTool is required but was not found on PATH. Install it with "
        "`sudo apt install libimage-exiftool-perl`, `brew install exiftool`, "
        "or pass --exiftool /path/to/exiftool."
    )


def process_photos(args: argparse.Namespace) -> list[PhotoResult]:
    if not args.track.is_file():
        raise ValueError(f"Track file not found: {args.track}")
    if not args.photo_dir.is_dir():
        raise ValueError(f"Photo directory not found: {args.photo_dir}")

    exiftool = check_exiftool(args.exiftool)
    default_tz = timezone_from_name(args.photo_timezone)
    track_points = parse_track(args.track)
    if not track_points:
        raise ValueError(f"No timestamped track points found in {args.track}")
    photo_time_offset_seconds = args.photo_time_offset + args.photo_time_diff

    photos = collect_photo_paths(args.photo_dir, recursive=args.recursive)
    if not photos:
        raise ValueError(f"No supported photos found in {args.photo_dir}")

    metadata_rows = run_exiftool_json(exiftool, photos)
    metadata_by_path = {
        Path(row["SourceFile"]).resolve(): row
        for row in metadata_rows
        if row.get("SourceFile")
    }

    results: list[PhotoResult] = []
    for photo in photos:
        metadata = metadata_by_path.get(photo.resolve())
        if metadata is None:
            results.append(PhotoResult(photo, "error", message="No ExifTool metadata row"))
            continue

        if has_gps(metadata) and not args.overwrite_existing:
            results.append(PhotoResult(photo, "skipped-existing-gps"))
            continue

        capture_time = capture_time_from_metadata(
            metadata,
            default_tz=default_tz,
            offset_seconds=photo_time_offset_seconds,
            use_file_mtime=args.use_file_mtime,
        )
        if capture_time is None:
            results.append(PhotoResult(photo, "skipped-no-timestamp"))
            continue

        location = interpolate_location(
            track_points,
            capture_time,
            max_time_delta=args.max_time_delta,
            max_interpolation_gap=args.max_interpolation_gap,
        )
        if location is None:
            results.append(
                PhotoResult(
                    photo,
                    "skipped-no-track-match",
                    capture_time=capture_time,
                    message=f"No track point within {args.max_time_delta:.0f}s",
                )
            )
            continue

        status = "dry-run" if args.dry_run else "updated"
        message = ""
        if not args.dry_run:
            try:
                write_gps(
                    exiftool,
                    photo,
                    location,
                    overwrite_original=args.overwrite_original,
                )
            except RuntimeError as exc:
                status = "error"
                message = str(exc)

        results.append(
            PhotoResult(
                path=photo,
                status=status,
                capture_time=capture_time,
                lat=location.lat,
                lon=location.lon,
                ele=location.ele,
                delta_seconds=location.nearest_delta_seconds,
                method=location.method,
                message=message,
            )
        )

    return results


def write_report(path: Path, results: list[PhotoResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "path",
                "status",
                "capture_time_utc",
                "latitude",
                "longitude",
                "elevation_m",
                "nearest_delta_seconds",
                "method",
                "message",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    result.path,
                    result.status,
                    result.capture_time.isoformat() if result.capture_time else "",
                    f"{result.lat:.8f}" if result.lat is not None else "",
                    f"{result.lon:.8f}" if result.lon is not None else "",
                    f"{result.ele:.3f}" if result.ele is not None else "",
                    f"{result.delta_seconds:.1f}" if result.delta_seconds is not None else "",
                    result.method or "",
                    result.message,
                ]
            )


def print_summary(results: list[PhotoResult]) -> None:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print("Summary:")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")

    actionable = [
        result
        for result in results
        if result.status in {"updated", "dry-run", "error", "skipped-no-track-match"}
    ]
    if actionable:
        print()
        for result in actionable[:25]:
            if result.lat is not None and result.lon is not None:
                print(
                    f"{result.status:22} {result.path} "
                    f"{result.lat:.6f},{result.lon:.6f} "
                    f"delta={result.delta_seconds:.1f}s {result.method}"
                )
            else:
                print(f"{result.status:22} {result.path} {result.message}")
        if len(actionable) > 25:
            print(f"... {len(actionable) - 25} more rows omitted; use --report for all rows.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("track", type=Path, help="GPX or KML file with timestamped points.")
    parser.add_argument("photo_dir", type=Path, help="Folder containing photos to geotag.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Estimate locations and report changes without writing photo metadata.",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Replace GPS tags even when a photo already has GPS metadata.",
    )
    parser.add_argument(
        "--overwrite-original",
        action="store_true",
        help="Tell ExifTool not to keep *_original backup files. By default backups are kept.",
    )
    parser.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        default=True,
        help="Only scan the top level of photo_dir.",
    )
    parser.add_argument(
        "--photo-timezone",
        help=(
            "Timezone for photo timestamps that do not include an offset, e.g. "
            "America/New_York. Defaults to the local machine timezone."
        ),
    )
    parser.add_argument(
        "--photo-time-offset",
        type=float,
        default=0.0,
        help=(
            "Seconds to add to each photo timestamp before matching the track. "
            "Kept for scripts; --photo-time-diff is friendlier for manual use."
        ),
    )
    parser.add_argument(
        "--photo-time-diff",
        "--camera-time-offset",
        dest="photo_time_diff",
        type=parse_time_delta_seconds,
        default=0.0,
        metavar="DELTA",
        help=(
            "Time to add to each photo timestamp before matching the track, e.g. "
            "+1:00:00, -30m, 1h15m, or 5400. Use this when the camera clock "
            "differs from GPS track time."
        ),
    )
    parser.add_argument(
        "--max-time-delta",
        type=float,
        default=300.0,
        help="Maximum seconds from photo time to nearest track point. Default: 300.",
    )
    parser.add_argument(
        "--max-interpolation-gap",
        type=float,
        default=900.0,
        help="Do not interpolate across track gaps larger than this many seconds. Default: 900.",
    )
    parser.add_argument(
        "--use-file-mtime",
        action="store_true",
        help="Fallback to file modification time when no capture timestamp exists.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Write a CSV report with one row per photo.",
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
        results = process_photos(args)
    except (FileNotFoundError, ValueError, RuntimeError, ET.ParseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print_summary(results)
    if args.report:
        write_report(args.report, results)
        print(f"\nWrote report: {args.report}")

    return 1 if any(result.status == "error" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
