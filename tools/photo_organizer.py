#!/usr/bin/env python3
"""Organize photos and videos by capture date and optional offline reverse geocoding."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from PIL import ExifTags, Image

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    pillow_heif = None


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
    ".webp",
    ".gif",
    ".bmp",
    ".raw",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".dng",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mts",
    ".m2ts",
    ".3gp",
    ".3g2",
    ".mpg",
    ".mpeg",
    ".wmv",
    ".mkv",
    ".webm",
}

DATE_TAGS = (
    (36867, "DateTimeOriginal"),
    (36868, "DateTimeDigitized"),
    (306, "DateTime"),
)

GPS_TAG = 34853


@dataclass
class PhotoInfo:
    path: Path
    media_type: str
    capture_datetime: datetime | None
    date_source: str
    latitude: float | None
    longitude: float | None
    exif_error: str = ""


@dataclass
class LocationInfo:
    name: str = ""
    admin1: str = ""
    admin2: str = ""
    country_code: str = ""

    @property
    def label(self) -> str:
        pieces = [self.name, self.admin1, self.country_code]
        return ", ".join(piece for piece in pieces if piece)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Directory containing photos or videos")
    parser.add_argument("destination", type=Path, help="Organized output directory")
    parser.add_argument(
        "--media",
        choices=("images", "videos", "all"),
        default="images",
        help="Which media type to process. Defaults to images for backwards compatibility.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="CSV manifest path. Defaults to DESTINATION/manifest.csv",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually create organized files. Without this, only a manifest is written.",
    )
    parser.add_argument(
        "--method",
        choices=("copy", "link", "move"),
        default="copy",
        help="How to place files when --execute is set",
    )
    parser.add_argument(
        "--geocode",
        action="store_true",
        help="Use offline reverse_geocoder for photos with GPS EXIF",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit processed image count for testing",
    )
    parser.add_argument(
        "--unknown-location-label",
        default="Unknown Location",
        help="Folder label for photos without GPS or geocode results",
    )
    parser.add_argument(
        "--unknown-date-label",
        default="Unknown Date",
        help="Folder label for photos without EXIF dates",
    )
    return parser.parse_args()


def image_paths(root: Path) -> Iterable[Path]:
    yield from media_paths(root, "images")


def media_paths(root: Path, media: str) -> Iterable[Path]:
    extensions = set()
    if media in {"images", "all"}:
        extensions.update(IMAGE_EXTENSIONS)
    if media in {"videos", "all"}:
        extensions.update(VIDEO_EXTENSIONS)

    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in extensions:
            yield path


def media_type_for_path(path: Path) -> str:
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return "video"
    return "image"


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    return None


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip().replace("\x00", "")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo:
        return parsed.replace(tzinfo=None)
    return parsed


def parse_filename_datetime(path: Path) -> datetime | None:
    patterns = (
        (r"(?P<date>\d{4}-\d{2}-\d{2})[ _-]+(?P<h>\d{2})[.:](?P<m>\d{2})[.:](?P<s>\d{2})", "%Y-%m-%d %H:%M:%S"),
        (r"(?P<date>\d{8})[_-](?P<h>\d{2})(?P<m>\d{2})(?P<s>\d{2})", "%Y%m%d %H:%M:%S"),
    )
    name = path.stem
    for pattern, fmt in patterns:
        match = re.search(pattern, name)
        if not match:
            continue
        date = match.group("date")
        text = f"{date} {match.group('h')}:{match.group('m')}:{match.group('s')}"
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_iso6709_location(value: Any) -> tuple[float | None, float | None]:
    if not value:
        return None, None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip()
    numbers = re.findall(r"[+-]\d+(?:\.\d+)?", text)
    if len(numbers) < 2:
        return None, None
    try:
        return float(numbers[0]), float(numbers[1])
    except ValueError:
        return None, None


def rational_to_float(value: Any) -> float:
    if isinstance(value, tuple) and len(value) == 2:
        numerator, denominator = value
        return float(numerator) / float(denominator)
    return float(value)


def dms_to_decimal(value: Any, ref: Any) -> float | None:
    if value is None or len(value) != 3:
        return None
    degrees = rational_to_float(value[0])
    minutes = rational_to_float(value[1])
    seconds = rational_to_float(value[2])
    decimal = degrees + (minutes / 60.0) + (seconds / 3600.0)
    ref_text = ref.decode("ascii", errors="ignore") if isinstance(ref, bytes) else str(ref)
    if ref_text.upper() in {"S", "W"}:
        decimal *= -1
    return decimal


def get_gps_ifd(exif: Any) -> dict[str, Any]:
    try:
        raw = exif.get_ifd(GPS_TAG)
    except Exception:
        raw = exif.get(GPS_TAG, {})
    if not raw:
        return {}
    return {ExifTags.GPSTAGS.get(key, key): value for key, value in raw.items()}


def extract_photo_info(path: Path) -> PhotoInfo:
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            capture_datetime = None
            date_source = "mtime"
            if exif:
                for tag, label in DATE_TAGS:
                    capture_datetime = parse_datetime(exif.get(tag))
                    if capture_datetime:
                        date_source = label
                        break
            gps = get_gps_ifd(exif) if exif else {}
            latitude = dms_to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
            longitude = dms_to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
    except Exception as exc:
        capture_datetime = None
        date_source = "mtime"
        latitude = None
        longitude = None
        exif_error = f"{type(exc).__name__}: {exc}"
    else:
        exif_error = ""

    if capture_datetime is None:
        capture_datetime = datetime.fromtimestamp(path.stat().st_mtime)
        date_source = "mtime"

    return PhotoInfo(
        path=path,
        media_type="image",
        capture_datetime=capture_datetime,
        date_source=date_source,
        latitude=latitude,
        longitude=longitude,
        exif_error=exif_error,
    )


def ffprobe_video_metadata(
    path: Path,
) -> tuple[datetime | None, float | None, float | None, str]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None, None, None, "ffprobe not found"

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format_tags:stream_tags",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        payload = json.loads(completed.stdout or "{}")
    except Exception as exc:
        return None, None, None, f"{type(exc).__name__}: {exc}"

    all_tags: list[dict[str, Any]] = []
    all_tags.append(payload.get("format", {}).get("tags", {}))
    for stream in payload.get("streams", []):
        all_tags.append(stream.get("tags", {}))

    candidates: list[Any] = []
    for tags in all_tags:
        candidates.append(tags.get("creation_time"))

    creation_time = None
    for candidate in candidates:
        parsed = parse_iso_datetime(candidate)
        if parsed:
            creation_time = parsed
            break

    latitude = None
    longitude = None
    for tags in all_tags:
        for key, value in tags.items():
            if "location" not in str(key).lower():
                continue
            latitude, longitude = parse_iso6709_location(value)
            if latitude is not None and longitude is not None:
                return creation_time, latitude, longitude, ""

    return creation_time, None, None, ""


def extract_video_info(path: Path) -> PhotoInfo:
    filename_datetime = parse_filename_datetime(path)
    metadata_datetime, latitude, longitude, metadata_error = ffprobe_video_metadata(path)
    if filename_datetime:
        capture_datetime = filename_datetime
        date_source = "filename"
    else:
        capture_datetime = metadata_datetime
        date_source = "ffprobe:creation_time" if metadata_datetime else "mtime"

    if capture_datetime is None:
        capture_datetime = datetime.fromtimestamp(path.stat().st_mtime)
        date_source = "mtime"

    return PhotoInfo(
        path=path,
        media_type="video",
        capture_datetime=capture_datetime,
        date_source=date_source,
        latitude=latitude,
        longitude=longitude,
        exif_error=metadata_error,
    )


def extract_media_info(path: Path) -> PhotoInfo:
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return extract_video_info(path)
    return extract_photo_info(path)


def load_reverse_geocoder() -> Any:
    try:
        import reverse_geocoder as rg
    except ImportError as exc:
        raise SystemExit(
            "--geocode requires reverse_geocoder. Install it in the active environment."
        ) from exc
    return rg


def geocode_photo(info: PhotoInfo, rg: Any, cache: dict[str, LocationInfo]) -> LocationInfo:
    if info.latitude is None or info.longitude is None:
        return LocationInfo()
    key = f"{info.latitude:.4f},{info.longitude:.4f}"
    if key in cache:
        return cache[key]
    result = rg.search((info.latitude, info.longitude), mode=1)[0]
    location = LocationInfo(
        name=result.get("name", ""),
        admin1=result.get("admin1", ""),
        admin2=result.get("admin2", ""),
        country_code=result.get("cc", ""),
    )
    cache[key] = location
    return location


def sanitize_path_part(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[\\/:\0]", "-", value)
    value = re.sub(r"\s+", " ", value)
    return value[:120] if value else "Unknown"


def destination_folder(
    info: PhotoInfo,
    location: LocationInfo,
    unknown_date_label: str,
    unknown_location_label: str,
) -> Path:
    if info.capture_datetime:
        year = str(info.capture_datetime.year)
        date_label = info.capture_datetime.strftime("%Y-%m-%d")
    else:
        year = unknown_date_label
        date_label = unknown_date_label

    location_label = location.label or unknown_location_label
    day_folder = f"{date_label} - {location_label}"
    return Path(sanitize_path_part(year)) / sanitize_path_part(day_folder)


def unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = path.with_name(f"{stem}__{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not create unique target for {path}")


def place_file(source: Path, target: Path, method: str) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    final_target = unique_target(target)
    if method == "copy":
        shutil.copy2(source, final_target)
    elif method == "link":
        os.link(source, final_target)
    elif method == "move":
        shutil.move(source, final_target)
    else:
        raise ValueError(f"Unknown method: {method}")
    return str(final_target)


def write_manifest(rows: list[dict[str, str]], manifest: Path) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "source",
        "target",
        "media_type",
        "action",
        "status",
        "capture_datetime",
        "date_source",
        "latitude",
        "longitude",
        "location",
        "admin1",
        "admin2",
        "country_code",
        "exif_error",
    ]
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve()
    manifest = args.manifest.resolve() if args.manifest else destination / "manifest.csv"

    if not source.is_dir():
        print(f"Source directory does not exist: {source}", file=sys.stderr)
        return 2

    rg = load_reverse_geocoder() if args.geocode else None
    geocode_cache: dict[str, LocationInfo] = {}
    rows: list[dict[str, str]] = []

    processed = 0
    for path in media_paths(source, args.media):
        processed += 1
        if args.limit and processed > args.limit:
            break

        info = extract_media_info(path)
        location = geocode_photo(info, rg, geocode_cache) if rg else LocationInfo()
        folder = destination_folder(
            info,
            location,
            args.unknown_date_label,
            args.unknown_location_label,
        )
        target = destination / folder / path.name
        action = args.method if args.execute else "dry-run"
        status = "planned"
        target_text = str(target)

        if args.execute:
            try:
                target_text = place_file(path, target, args.method)
                status = "ok"
            except Exception as exc:
                status = f"error: {type(exc).__name__}: {exc}"

        rows.append(
            {
                "source": str(path),
                "target": target_text,
                "media_type": info.media_type,
                "action": action,
                "status": status,
                "capture_datetime": info.capture_datetime.isoformat(sep=" ")
                if info.capture_datetime
                else "",
                "date_source": info.date_source,
                "latitude": f"{info.latitude:.7f}" if info.latitude is not None else "",
                "longitude": f"{info.longitude:.7f}" if info.longitude is not None else "",
                "location": location.label,
                "admin1": location.admin1,
                "admin2": location.admin2,
                "country_code": location.country_code,
                "exif_error": info.exif_error,
            }
        )

    write_manifest(rows, manifest)

    summary = {
        "source": str(source),
        "destination": str(destination),
        "manifest": str(manifest),
        "processed": len(rows),
        "images": sum(1 for row in rows if row["media_type"] == "image"),
        "videos": sum(1 for row in rows if row["media_type"] == "video"),
        "with_gps": sum(1 for row in rows if row["latitude"] and row["longitude"]),
        "with_location": sum(1 for row in rows if row["location"]),
        "executed": bool(args.execute),
        "method": args.method if args.execute else "dry-run",
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
