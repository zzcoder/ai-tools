#!/usr/bin/env python3
"""Render a GPS map-and-photo slideshow from a GPX route and geotagged media."""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
import requests

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001
    pillow_heif = None


def find_workspace_root(path: Path) -> Path:
    start = path if path.is_dir() else path.parent
    for candidate in (start, *start.parents):
        if (candidate / "input").exists() or ((candidate / "tools").exists() and (candidate / "output").exists()):
            return candidate
    return Path.cwd()


ROOT = find_workspace_root(Path(__file__).resolve())
SOURCE_DIR = ROOT / "input" / "dc-fountain-photos"
GPX_PATH = ROOT / "input" / "DC_Fountain_Tour.gpx"
TITLE_VIDEO = ROOT / "output" / "dc-fountain-tour-title-4k.mp4"
MUSIC_PATH = ROOT / "input" / "reivers.mp3"
LOGO_PATH = ROOT / "input" / "hh-logo.png"
WAYPOINT_GPX = ROOT / "output" / "DC_Fountain_Tour_with_union_station.gpx"
DECOR_MANIFEST = ROOT / "output" / "dc-fountain-gpt-branding-batch-v8" / "manifest.json"
BUILD_DIR = ROOT / "build" / "dc-fountain-slideshow"
OUTPUT_PATH = ROOT / "output" / "dc-fountain-tour-slideshow-4k.mp4"
TITLE = "DC Fountain Tour 6/6/2026"
TILE_DIR = ROOT / "build" / "map-slideshow-tiles"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".dng"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}
DATE_TAGS = (36867, 36868, 306)
GPS_IFD = 34853
LOCAL_TZ = ZoneInfo("America/New_York")
EARTH_RADIUS_M = 6_371_000.0
USER_AGENT = "codex-dc-fountain-slideshow/1.0"


@dataclass
class Track:
    latlon: list[tuple[float, float]]
    xy: list[tuple[float, float]]
    cumulative_m: list[float]
    total_m: float
    lat0_rad: float


@dataclass
class MediaItem:
    path: Path
    kind: str
    capture_dt: datetime
    lat: float
    lon: float
    route_s: float
    route_distance_m: float
    duration: float = 0.0
    corrected_time: bool = False
    manifest_name: str | None = None

    @property
    def basename(self) -> str:
        return self.path.name


@dataclass
class StopGroup:
    index: int
    items: list[MediaItem]
    median_s: float
    order_s: float
    label: str
    waypoint: str | None
    waypoint_distance_m: float | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--gpx", type=Path, default=GPX_PATH)
    parser.add_argument("--title-video", type=Path, default=TITLE_VIDEO)
    parser.add_argument("--music", type=Path, default=MUSIC_PATH)
    parser.add_argument("--logo", type=Path, default=LOGO_PATH)
    parser.add_argument("--waypoint-gpx", type=Path, default=WAYPOINT_GPX)
    parser.add_argument("--decor-manifest", type=Path, default=DECOR_MANIFEST)
    parser.add_argument("--tile-dir", type=Path, default=TILE_DIR)
    parser.add_argument("--build-dir", type=Path, default=BUILD_DIR)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--title", default=TITLE)
    parser.add_argument("--width", type=int, default=3840)
    parser.add_argument("--height", type=int, default=2160)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--image-duration", type=float, default=2.0)
    parser.add_argument("--video-max-duration", type=float, default=2.0)
    parser.add_argument("--route-duration", type=float, default=None, help="Fixed map transition duration. Default is dynamic.")
    parser.add_argument("--route-min-duration", type=float, default=10.0)
    parser.add_argument("--route-max-duration", type=float, default=20.0)
    parser.add_argument(
        "--place-name-hold-duration",
        type=float,
        default=4.0,
        help="Seconds to hold the destination place/fountain name after each GPS route animation.",
    )
    parser.add_argument("--route-min-distance-m", type=float, default=150.0)
    parser.add_argument("--route-max-distance-m", type=float, default=1400.0)
    parser.add_argument("--min-map-zoom", type=int, default=15)
    parser.add_argument("--max-map-zoom", type=int, default=18)
    parser.add_argument("--match-distance-m", type=float, default=220.0)
    parser.add_argument("--cluster-gap-m", type=float, default=300.0)
    parser.add_argument("--split-time-gap-min", type=float, default=45.0)
    parser.add_argument("--prefer-live-videos", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--encoder", choices=("auto", "h264_nvenc", "libx264"), default="auto")
    parser.add_argument("--gpu", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--clean", action="store_true", help="Remove rendered clip cache before rendering.")
    parser.add_argument("--limit-groups", type=int, default=0, help="Render only the first N groups for testing.")
    return parser.parse_args()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_gpx_points(path: Path) -> list[tuple[float, float]]:
    root = ET.parse(path).getroot()
    points: list[tuple[float, float]] = []
    for element in root.iter():
        if local_name(element.tag) == "trkpt":
            points.append((float(element.attrib["lat"]), float(element.attrib["lon"])))
    if len(points) < 2:
        raise SystemExit(f"Need at least two GPX track points: {path}")
    return points


def build_track(points: list[tuple[float, float]]) -> Track:
    lat0_rad = math.radians(sum(lat for lat, _ in points) / len(points))
    xy = [latlon_to_xy(lat, lon, lat0_rad) for lat, lon in points]
    cumulative = [0.0]
    total = 0.0
    for a, b in zip(xy, xy[1:]):
        total += math.hypot(b[0] - a[0], b[1] - a[1])
        cumulative.append(total)
    return Track(points, xy, cumulative, total, lat0_rad)


def latlon_to_xy(lat: float, lon: float, lat0_rad: float) -> tuple[float, float]:
    return (
        EARTH_RADIUS_M * math.radians(lon) * math.cos(lat0_rad),
        EARTH_RADIUS_M * math.radians(lat),
    )


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def project_to_track(track: Track, lat: float, lon: float) -> tuple[float, float]:
    px, py = latlon_to_xy(lat, lon, track.lat0_rad)
    best_s = 0.0
    best_dist = float("inf")
    for index, ((ax, ay), (bx, by)) in enumerate(zip(track.xy, track.xy[1:])):
        vx = bx - ax
        vy = by - ay
        seg_len_sq = vx * vx + vy * vy
        if seg_len_sq <= 1e-9:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / seg_len_sq))
        qx = ax + vx * t
        qy = ay + vy * t
        dist = math.hypot(px - qx, py - qy)
        if dist < best_dist:
            best_dist = dist
            best_s = track.cumulative_m[index] + math.sqrt(seg_len_sq) * t
    return best_s, best_dist


def route_point_at(track: Track, s: float) -> tuple[float, float]:
    s = s % track.total_m
    index = max(1, bisect.bisect_right(track.cumulative_m, s))
    if index >= len(track.cumulative_m):
        return track.latlon[-1]
    prev_s = track.cumulative_m[index - 1]
    next_s = track.cumulative_m[index]
    ratio = 0.0 if next_s <= prev_s else (s - prev_s) / (next_s - prev_s)
    lat1, lon1 = track.latlon[index - 1]
    lat2, lon2 = track.latlon[index]
    return lat1 + (lat2 - lat1) * ratio, lon1 + (lon2 - lon1) * ratio


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


def parse_video_datetime(tags: dict[str, Any]) -> datetime | None:
    created = tags.get("com.apple.quicktime.creationdate")
    if created:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
            try:
                return datetime.strptime(str(created), fmt).astimezone(LOCAL_TZ).replace(tzinfo=None)
            except ValueError:
                continue
    created = tags.get("creation_time")
    if created:
        normalized = str(created).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).astimezone(LOCAL_TZ).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def rational_to_float(value: Any) -> float:
    if isinstance(value, tuple) and len(value) == 2 and all(isinstance(v, int) for v in value):
        den = value[1] or 1
        return float(value[0]) / den
    return float(value)


def dms_to_degrees(values: Any, ref: str) -> float | None:
    if not values or len(values) < 3:
        return None
    degrees = rational_to_float(values[0])
    minutes = rational_to_float(values[1])
    seconds = rational_to_float(values[2])
    result = degrees + minutes / 60.0 + seconds / 3600.0
    if ref.upper() in {"S", "W"}:
        result *= -1
    return result


def extract_image_metadata(path: Path) -> tuple[datetime | None, float | None, float | None]:
    try:
        image = Image.open(path)
        exif = image.getexif()
    except Exception as exc:  # noqa: BLE001
        print(f"Skipping unreadable image {path.name}: {exc}", file=sys.stderr)
        return None, None, None

    dt = None
    for tag in DATE_TAGS:
        dt = parse_datetime(exif.get(tag))
        if dt:
            break

    try:
        gps = exif.get_ifd(GPS_IFD)
    except Exception:  # noqa: BLE001
        gps = {}
    lat = dms_to_degrees(gps.get(2), str(gps.get(1, "N"))) if gps else None
    lon = dms_to_degrees(gps.get(4), str(gps.get(3, "E"))) if gps else None
    return dt, lat, lon


def parse_iso6709_location(value: Any) -> tuple[float | None, float | None]:
    if not value:
        return None, None
    match = re.match(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?)?/?$", str(value))
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def probe_video(path: Path) -> tuple[datetime | None, float | None, float | None, float]:
    try:
        raw = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_format", "-of", "json", str(path)],
            text=True,
        )
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"Skipping unreadable video {path.name}: {exc}", file=sys.stderr)
        return None, None, None, 0.0
    fmt = data.get("format", {})
    tags = fmt.get("tags", {})
    dt = parse_video_datetime(tags)
    lat, lon = parse_iso6709_location(tags.get("com.apple.quicktime.location.ISO6709"))
    duration = float(fmt.get("duration") or 0.0)
    return dt, lat, lon, duration


def load_manifest_corrections(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, dict[str, Any]] = {}
    for item in data if isinstance(data, list) else []:
        source = Path(str(item.get("source", "")))
        if not source.name:
            continue
        result[source.name.lower()] = item
        result[source.stem.lower()] = item
    return result


def load_waypoints(path: Path) -> list[tuple[str, float, float]]:
    if not path.exists():
        return []
    root = ET.parse(path).getroot()
    waypoints: list[tuple[str, float, float]] = []
    for element in root.iter():
        if local_name(element.tag) != "wpt":
            continue
        name = None
        for child in element:
            if local_name(child.tag) == "name":
                name = child.text
                break
        if name:
            waypoints.append((name, float(element.attrib["lat"]), float(element.attrib["lon"])))
    return waypoints


def scan_media(args: argparse.Namespace, track: Track) -> list[MediaItem]:
    corrections = load_manifest_corrections(args.decor_manifest)
    selected: list[MediaItem] = []
    for path in sorted(args.source_dir.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        ext = path.suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            dt, lat, lon = extract_image_metadata(path)
            duration = 0.0
            kind = "image"
        elif ext in VIDEO_EXTENSIONS:
            dt, lat, lon, duration = probe_video(path)
            kind = "video"
        else:
            continue

        correction = corrections.get(path.name.lower()) or corrections.get(path.stem.lower())
        corrected_time = False
        manifest_name = None
        if correction:
            manifest_name = correction.get("fountain_name")
            corrected = parse_datetime(correction.get("photo_datetime"))
            if corrected:
                dt = corrected
                corrected_time = True
        if not dt or lat is None or lon is None:
            continue
        if (dt.month, dt.day) not in {(6, 6), (6, 7)} or dt.year != 2026:
            continue

        route_s, distance = project_to_track(track, lat, lon)
        if distance > args.match_distance_m:
            continue
        selected.append(MediaItem(path, kind, dt, lat, lon, route_s, distance, duration, corrected_time, manifest_name))
    if args.prefer_live_videos:
        selected = prefer_live_photo_videos(selected)
    return selected


def prefer_live_photo_videos(items: list[MediaItem]) -> list[MediaItem]:
    """When an iPhone Live Photo sidecar video is present, use it instead of the still."""
    video_keys = {(item.path.parent.resolve(), item.path.stem.lower()) for item in items if item.kind == "video"}
    if not video_keys:
        return items
    result: list[MediaItem] = []
    replaced = 0
    for item in items:
        key = (item.path.parent.resolve(), item.path.stem.lower())
        if item.kind == "image" and key in video_keys:
            replaced += 1
            continue
        result.append(item)
    if replaced:
        print(f"Preferred Live Photo videos over {replaced} matching still image(s).", flush=True)
    return result


def median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def split_by_time(items: list[MediaItem], gap_minutes: float) -> list[list[MediaItem]]:
    ordered = sorted(items, key=lambda item: (item.capture_dt, item.path.name.lower()))
    groups: list[list[MediaItem]] = []
    current: list[MediaItem] = []
    for item in ordered:
        if current:
            gap = (item.capture_dt - current[-1].capture_dt).total_seconds() / 60.0
            unreliable_camera_clock = (
                (item.path.stem.upper().startswith("DSC_") and not item.corrected_time)
                or (current[-1].path.stem.upper().startswith("DSC_") and not current[-1].corrected_time)
            )
            if gap > gap_minutes and not unreliable_camera_clock:
                groups.append(current)
                current = []
        current.append(item)
    if current:
        groups.append(current)
    return groups


def group_media(args: argparse.Namespace, track: Track, items: list[MediaItem]) -> list[StopGroup]:
    if not items:
        return []

    by_route = sorted(items, key=lambda item: item.route_s)
    route_groups: list[list[MediaItem]] = []
    current: list[MediaItem] = []
    for item in by_route:
        if current and item.route_s - current[-1].route_s > args.cluster_gap_m:
            route_groups.append(current)
            current = []
        current.append(item)
    if current:
        route_groups.append(current)

    split_groups: list[list[MediaItem]] = []
    for route_group in route_groups:
        split_groups.extend(split_by_time(route_group, args.split_time_gap_min))

    earliest = min(items, key=lambda item: item.capture_dt)
    earliest_time = earliest.capture_dt
    start_s = earliest.route_s
    waypoints = load_waypoints(args.waypoint_gpx)
    groups: list[StopGroup] = []
    for raw_group in split_groups:
        group_s = median([item.route_s for item in raw_group])
        order_s = (group_s - start_s) % track.total_m
        group_first_time = min(item.capture_dt for item in raw_group)
        if order_s < args.cluster_gap_m and (group_first_time - earliest_time).total_seconds() > 60 * 60:
            order_s += track.total_m
        group_lat, group_lon = route_point_at(track, group_s)
        waypoint_name = None
        waypoint_dist = None
        if waypoints:
            waypoint_name, wlat, wlon = min(
                waypoints,
                key=lambda waypoint: haversine_m((group_lat, group_lon), (waypoint[1], waypoint[2])),
            )
            waypoint_dist = haversine_m((group_lat, group_lon), (wlat, wlon))
            if waypoint_dist > 260:
                waypoint_name = None
        groups.append(StopGroup(0, sorted(raw_group, key=lambda item: (item.capture_dt, item.path.name.lower())), group_s, order_s, "", waypoint_name, waypoint_dist))

    groups.sort(key=lambda group: group.order_s)
    for index, group in enumerate(groups, start=1):
        group.index = index
        group.label = f"Stop {index:02d}"
    if args.limit_groups:
        groups = groups[: args.limit_groups]
    return groups


def clean_place_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = re.sub(r"^\s*\d+\s*[.)-]\s*", "", str(name)).strip()
    return cleaned or None


def place_name_for_group(group: StopGroup) -> str:
    waypoint = clean_place_name(group.waypoint)
    if waypoint:
        return waypoint

    counts: dict[str, int] = {}
    for item in group.items:
        name = clean_place_name(item.manifest_name)
        if name:
            counts[name] = counts.get(name, 0) + 1
    if counts:
        return sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))[0][0]
    return group.label


def write_reports(args: argparse.Namespace, groups: list[StopGroup], selected: list[MediaItem], track: Track) -> None:
    args.build_dir.mkdir(parents=True, exist_ok=True)
    media_csv = args.build_dir / "selected-media.csv"
    with media_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "group",
                "kind",
                "filename",
                "capture_dt",
                "lat",
                "lon",
                "route_s_m",
                "route_distance_m",
                "duration",
                "corrected_time",
                "manifest_name",
            ]
        )
        by_id = {item.path: group.index for group in groups for item in group.items}
        for item in sorted(selected, key=lambda media: (by_id.get(media.path, 999), media.capture_dt, media.path.name.lower())):
            writer.writerow(
                [
                    by_id.get(item.path, ""),
                    item.kind,
                    item.path.name,
                    item.capture_dt.isoformat(sep=" "),
                    f"{item.lat:.7f}",
                    f"{item.lon:.7f}",
                    f"{item.route_s:.1f}",
                    f"{item.route_distance_m:.1f}",
                    f"{item.duration:.3f}",
                    item.corrected_time,
                    item.manifest_name or "",
                ]
            )
    summary = {
        "title": args.title,
        "selected_media": len(selected),
        "images": sum(1 for item in selected if item.kind == "image"),
        "videos": sum(1 for item in selected if item.kind == "video"),
        "track_length_m": round(track.total_m, 1),
        "group_count": len(groups),
        "groups": [
            {
                "index": group.index,
                "item_count": len(group.items),
                "images": sum(1 for item in group.items if item.kind == "image"),
                "videos": sum(1 for item in group.items if item.kind == "video"),
                "first_time": min(item.capture_dt for item in group.items).isoformat(sep=" "),
                "last_time": max(item.capture_dt for item in group.items).isoformat(sep=" "),
                "median_route_s_m": round(group.median_s, 1),
                "order_s_m": round(group.order_s, 1),
                "waypoint": group.waypoint,
                "place_name": place_name_for_group(group),
                "waypoint_distance_m": None if group.waypoint_distance_m is None else round(group.waypoint_distance_m, 1),
                "sample_files": [item.path.name for item in group.items[:8]],
            }
            for group in groups
        ],
    }
    (args.build_dir / "render-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Wrote media report: {media_csv}")


def choose_encoder(args: argparse.Namespace) -> str:
    if args.encoder != "auto":
        return args.encoder
    try:
        encoders = subprocess.check_output(["ffmpeg", "-hide_banner", "-encoders"], text=True, stderr=subprocess.STDOUT)
        if "h264_nvenc" in encoders:
            return "h264_nvenc"
    except Exception:  # noqa: BLE001
        pass
    return "libx264"


def encoder_args(encoder: str, gpu: int) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-gpu", str(gpu), "-preset", "p4", "-cq", "23", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "21"]


def run(command: list[str]) -> None:
    print(" ".join(command[:5]) + (" ..." if len(command) > 5 else ""), flush=True)
    subprocess.run(command, check=True)


def ffprobe_duration(path: Path) -> float:
    value = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    ).strip()
    return float(value)


def stage_image(source: Path, staged_dir: Path) -> Path:
    staged_dir.mkdir(parents=True, exist_ok=True)
    safe_suffix = source.suffix.lower().lstrip(".") or "image"
    target = staged_dir / f"{source.stem}_{safe_suffix}.jpg"
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    image = Image.open(source)
    image = ImageOps.exif_transpose(image)
    if image.mode not in {"RGB", "L"}:
        bg = Image.new("RGB", image.size, "white")
        if "A" in image.getbands():
            bg.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
            image = bg
        else:
            image = image.convert("RGB")
    else:
        image = image.convert("RGB")
    image.thumbnail((7680, 4320), Image.Resampling.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, quality=94, optimize=True)
    return target


def compose_image_frame(args: argparse.Namespace, source: Path) -> Path:
    frames_dir = args.build_dir / "staged-frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    safe_suffix = source.suffix.lower().lstrip(".") or "image"
    target = frames_dir / f"{source.stem}_{safe_suffix}_{args.width}x{args.height}.jpg"
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target

    image = Image.open(source)
    image = ImageOps.exif_transpose(image)
    if image.mode not in {"RGB", "L"}:
        bg = Image.new("RGB", image.size, "white")
        if "A" in image.getbands():
            rgba = image.convert("RGBA")
            bg.paste(rgba, mask=rgba.getchannel("A"))
            image = bg
        else:
            image = image.convert("RGB")
    else:
        image = image.convert("RGB")

    background = ImageOps.fit(image, (args.width, args.height), method=Image.Resampling.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(36))
    background = ImageEnhance.Color(background).enhance(0.75)
    background = ImageEnhance.Brightness(background).enhance(0.86)

    foreground = image.copy()
    foreground.thumbnail((args.width, args.height), Image.Resampling.LANCZOS)
    x = (args.width - foreground.width) // 2
    y = (args.height - foreground.height) // 2

    shadow = Image.new("RGBA", (args.width, args.height), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle(
        (x + 18, y + 22, x + foreground.width + 18, y + foreground.height + 22),
        radius=10,
        fill=(0, 0, 0, 92),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(22))
    frame = Image.alpha_composite(background.convert("RGBA"), shadow)
    frame.alpha_composite(foreground.convert("RGBA"), (x, y))
    frame.convert("RGB").save(target, quality=94, optimize=True)
    return target


def clip_path_for_media(args: argparse.Namespace, group: StopGroup, item: MediaItem, index: int) -> Path:
    clips_dir = args.build_dir / "clips"
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", item.path.stem)
    return clips_dir / f"g{group.index:02d}_{index:03d}_{item.kind}_{safe_stem}.mp4"


def media_filter(width: int, height: int, fps: int, fade_duration: float | None = None) -> str:
    base = (
        f"[0:v]split=2[base][fg];"
        f"[base]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=32:2,eq=brightness=-0.07:saturation=0.76[bg];"
        f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
        f"[bg][fgs]overlay=(W-w)/2:(H-h)/2,fps={fps},setsar=1,format=yuv420p"
    )
    if fade_duration and fade_duration > 0.3:
        return base + f",fade=t=in:st=0:d=0.12,fade=t=out:st={fade_duration - 0.18:.3f}:d=0.18"
    return base


def render_image_clip(args: argparse.Namespace, source: Path, target: Path, encoder: str) -> None:
    if target.exists():
        return
    staged = compose_image_frame(args, source)
    target.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(args.image_duration * args.fps))
    run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(args.fps),
            "-i",
            str(staged),
            "-frames:v",
            str(frames),
            "-vf",
            f"fps={args.fps},setsar=1,format=yuv420p,fade=t=in:st=0:d=0.12,fade=t=out:st={args.image_duration - 0.18:.3f}:d=0.18",
            "-an",
            *encoder_args(encoder, args.gpu),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ]
    )


def render_video_clip(args: argparse.Namespace, source: Path, target: Path, source_duration: float, encoder: str) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    duration = min(max(0.3, source_duration), args.video_max_duration)
    run(
        [
            "ffmpeg",
            "-y",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source),
            "-vf",
            media_filter(args.width, args.height, args.fps, duration),
            "-an",
            *encoder_args(encoder, args.gpu),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ]
    )


def normalize_title(args: argparse.Namespace, encoder: str) -> Path:
    target = args.build_dir / "clips" / "0000_title.mp4"
    if target.exists() and target.stat().st_mtime >= args.title_video.stat().st_mtime:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(args.title_video),
            "-vf",
            f"fps={args.fps},scale={args.width}:{args.height},setsar=1,format=yuv420p",
            "-an",
            *encoder_args(encoder, args.gpu),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ]
    )
    return target


def mercator_px(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    scale = 256 * 2**zoom
    x = (lon + 180.0) / 360.0 * scale
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * scale
    return x, y


def tile_path(tile_dir: Path, zoom: int, x: int, y: int) -> Path:
    return tile_dir / str(zoom) / str(x) / f"{y}.png"


def fetch_tile(tile_dir: Path, zoom: int, x: int, y: int) -> Image.Image:
    path = tile_path(tile_dir, zoom, x, y)
    if path.exists():
        return Image.open(path).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            response = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            path.write_bytes(response.content)
            return Image.open(path).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch OSM tile {zoom}/{x}/{y}: {last_error}")


def map_bounds_for_points(
    latlon: list[tuple[float, float]],
    width: int,
    height: int,
    zoom: int,
    pad_x_ratio: float = 0.22,
    pad_y_ratio: float = 0.28,
) -> tuple[float, float, float, float]:
    merc_points = [mercator_px(lat, lon, zoom) for lat, lon in latlon]
    min_x = min(x for x, _ in merc_points)
    max_x = max(x for x, _ in merc_points)
    min_y = min(y for _, y in merc_points)
    max_y = max(y for _, y in merc_points)
    w = max(max_x - min_x, 96.0)
    h = max(max_y - min_y, 96.0)
    center_x = (min_x + max_x) / 2
    center_y = (min_y + max_y) / 2
    min_x = center_x - w / 2
    max_x = center_x + w / 2
    min_y = center_y - h / 2
    max_y = center_y + h / 2
    min_x -= w * pad_x_ratio
    max_x += w * pad_x_ratio
    min_y -= h * pad_y_ratio
    max_y += h * pad_y_ratio
    target_aspect = width / height
    w = max_x - min_x
    h = max_y - min_y
    if w / h > target_aspect:
        new_h = w / target_aspect
        pad = (new_h - h) / 2
        min_y -= pad
        max_y += pad
    else:
        new_w = h * target_aspect
        pad = (new_w - w) / 2
        min_x -= pad
        max_x += pad
    return min_x, min_y, max_x, max_y


def choose_map_zoom(args: argparse.Namespace, walk_m: float | None) -> int:
    if walk_m is None:
        return max(args.min_map_zoom, min(args.max_map_zoom, 16))
    if walk_m <= 220:
        zoom = args.max_map_zoom
    elif walk_m <= 550:
        zoom = args.max_map_zoom - 1
    elif walk_m <= 1100:
        zoom = args.max_map_zoom - 2
    else:
        zoom = args.min_map_zoom
    return max(args.min_map_zoom, min(args.max_map_zoom, zoom))


def segment_latlon_points(track: Track, start_s: float, end_s: float, steps: int = 180) -> list[tuple[float, float]]:
    if end_s < start_s:
        end_s += track.total_m
    points: list[tuple[float, float]] = []
    for i in range(max(2, steps)):
        s = start_s + (end_s - start_s) * i / (steps - 1)
        points.append(route_point_at(track, s))
    return points


def build_map_image(
    args: argparse.Namespace,
    track: Track,
    zoom: int,
    focus_points: list[tuple[float, float]] | None = None,
) -> tuple[Image.Image, tuple[float, float, float, float]]:
    points = focus_points or track.latlon
    bounds = map_bounds_for_points(points, args.width, args.height, zoom)
    min_x, min_y, max_x, max_y = bounds
    tile_min_x = math.floor(min_x / 256)
    tile_max_x = math.floor(max_x / 256)
    tile_min_y = math.floor(min_y / 256)
    tile_max_y = math.floor(max_y / 256)
    mosaic = Image.new(
        "RGB",
        ((tile_max_x - tile_min_x + 1) * 256, (tile_max_y - tile_min_y + 1) * 256),
        "#edf0e9",
    )
    for tx in range(tile_min_x, tile_max_x + 1):
        for ty in range(tile_min_y, tile_max_y + 1):
            tile = fetch_tile(args.tile_dir, zoom, tx, ty)
            mosaic.paste(tile, ((tx - tile_min_x) * 256, (ty - tile_min_y) * 256))

    crop = mosaic.crop(
        (
            int(round(min_x - tile_min_x * 256)),
            int(round(min_y - tile_min_y * 256)),
            int(round(max_x - tile_min_x * 256)),
            int(round(max_y - tile_min_y * 256)),
        )
    )
    image = crop.resize((args.width, args.height), Image.Resampling.LANCZOS)
    image = ImageEnhance.Color(image).enhance(0.78)
    image = ImageEnhance.Contrast(image).enhance(1.08)
    image = ImageEnhance.Brightness(image).enhance(0.88)
    tint = Image.new("RGB", (args.width, args.height), (14, 56, 48))
    image = Image.blend(image, tint, 0.16)
    return image, bounds


def point_to_canvas(args: argparse.Namespace, bounds: tuple[float, float, float, float], lat: float, lon: float, zoom: int) -> tuple[int, int]:
    min_x, min_y, max_x, max_y = bounds
    x, y = mercator_px(lat, lon, zoom)
    return int((x - min_x) / (max_x - min_x) * args.width), int((y - min_y) / (max_y - min_y) * args.height)


def route_canvas_points(args: argparse.Namespace, track: Track, bounds: tuple[float, float, float, float], zoom: int) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    last: tuple[int, int] | None = None
    for lat, lon in track.latlon:
        point = point_to_canvas(args, bounds, lat, lon, zoom)
        if last is None or abs(point[0] - last[0]) + abs(point[1] - last[1]) >= 3:
            points.append(point)
            last = point
    return points


def sample_route_segment(
    args: argparse.Namespace,
    track: Track,
    bounds: tuple[float, float, float, float],
    zoom: int,
    start_s: float,
    end_s: float,
    steps: int = 260,
) -> list[tuple[int, int]]:
    if end_s < start_s:
        end_s += track.total_m
    points: list[tuple[int, int]] = []
    for i in range(max(2, steps)):
        s = start_s + (end_s - start_s) * i / (steps - 1)
        lat, lon = route_point_at(track, s)
        point = point_to_canvas(args, bounds, lat, lon, zoom)
        if not points or abs(point[0] - points[-1][0]) + abs(point[1] - points[-1][1]) >= 2:
            points.append(point)
    return points


def smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}", size)


def scale_px(args: argparse.Namespace, value: int, minimum: int = 1) -> int:
    scale = min(args.width / 3840, args.height / 2160)
    return max(minimum, int(round(value * scale)))


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def wrap_text_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if text_size(draw, candidate, font)[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def add_logo(args: argparse.Namespace, frame: Image.Image) -> None:
    logo = Image.open(args.logo).convert("RGBA")
    logo.thumbnail((scale_px(args, 150, 48), scale_px(args, 150, 48)), Image.Resampling.LANCZOS)
    badge_size = scale_px(args, 188, 64)
    badge = Image.new("RGBA", (badge_size, badge_size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(badge)
    inset = scale_px(args, 6, 2)
    draw.ellipse(
        (inset, inset, badge_size - inset, badge_size - inset),
        fill=(255, 255, 250, 230),
        outline=(21, 86, 56, 220),
        width=scale_px(args, 4, 1),
    )
    badge.alpha_composite(logo, ((badge.width - logo.width) // 2, (badge.height - logo.height) // 2))
    frame.alpha_composite(badge, (scale_px(args, 54, 10), scale_px(args, 44, 8)))


def draw_place_name_card(
    args: argparse.Namespace,
    overlay: Image.Image,
    place_name: str,
    progress: float,
) -> None:
    draw = ImageDraw.Draw(overlay)
    eased = smoothstep(min(1.0, progress * 2.2))
    if eased <= 0:
        return

    eyebrow_font = load_font(scale_px(args, 34, 12), bold=True)
    name_font = load_font(scale_px(args, 76, 22), bold=True)
    margin = scale_px(args, 220, 24)
    pad_x = scale_px(args, 74, 18)
    pad_y = scale_px(args, 46, 12)
    gap = scale_px(args, 14, 5)
    line_gap = scale_px(args, 10, 3)
    max_text_w = args.width - margin * 2 - pad_x * 2
    name_lines = wrap_text_to_width(draw, place_name, name_font, max_text_w)
    eyebrow = "Arriving at"

    eyebrow_w, eyebrow_h = text_size(draw, eyebrow, eyebrow_font)
    line_sizes = [text_size(draw, line, name_font) for line in name_lines]
    text_w = max([eyebrow_w, *(width for width, _ in line_sizes)])
    name_h = sum(height for _, height in line_sizes) + max(0, len(line_sizes) - 1) * line_gap
    box_w = min(args.width - margin * 2, max(text_w + pad_x * 2, scale_px(args, 1500, 240)))
    box_h = pad_y * 2 + eyebrow_h + gap + name_h
    box_x = (args.width - box_w) // 2
    box_y = args.height - box_h - scale_px(args, 190, 48)
    radius = scale_px(args, 28, 8)

    draw.rounded_rectangle(
        (box_x, box_y, box_x + box_w, box_y + box_h),
        radius=radius,
        fill=(5, 28, 25, int(224 * eased)),
        outline=(213, 231, 184, int(178 * eased)),
        width=scale_px(args, 4, 1),
    )
    draw.rounded_rectangle(
        (
            box_x + scale_px(args, 10, 2),
            box_y + scale_px(args, 10, 2),
            box_x + box_w - scale_px(args, 10, 2),
            box_y + box_h - scale_px(args, 10, 2),
        ),
        radius=radius,
        outline=(34, 194, 111, int(116 * eased)),
        width=scale_px(args, 2, 1),
    )

    y = box_y + pad_y
    draw.text(
        (box_x + (box_w - eyebrow_w) // 2, y),
        eyebrow,
        font=eyebrow_font,
        fill=(202, 239, 134, int(224 * eased)),
    )
    y += eyebrow_h + gap
    for line, (line_w, line_h) in zip(name_lines, line_sizes, strict=False):
        draw.text(
            (box_x + (box_w - line_w) // 2, y),
            line,
            font=name_font,
            fill=(247, 250, 236, int(242 * eased)),
        )
        y += line_h + line_gap


def build_route_base(
    args: argparse.Namespace,
    track: Track,
    focus_start_s: float | None = None,
    focus_end_s: float | None = None,
    walk_m: float | None = None,
) -> tuple[Image.Image, tuple[float, float, float, float], list[tuple[int, int]], int]:
    zoom = choose_map_zoom(args, walk_m)
    focus_points = None
    if focus_start_s is not None and focus_end_s is not None:
        focus_points = segment_latlon_points(track, focus_start_s, focus_end_s)
    map_image, bounds = build_map_image(args, track, zoom, focus_points)
    base = map_image.convert("RGBA")
    overlay = Image.new("RGBA", (args.width, args.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle((0, 0, args.width, scale_px(args, 300, 56)), fill=(5, 24, 25, 66))
    draw.rectangle((0, args.height - scale_px(args, 130, 28), args.width, args.height), fill=(5, 24, 25, 68))
    add_logo(args, overlay)
    font = load_font(scale_px(args, 31, 10))
    attribution = "Map tiles (c) OpenStreetMap contributors"
    bbox = draw.textbbox((0, 0), attribution, font=font)
    draw.text(
        (args.width - (bbox[2] - bbox[0]) - scale_px(args, 54, 10), args.height - scale_px(args, 70, 16)),
        attribution,
        font=font,
        fill=(232, 239, 231, 185),
    )
    base = Image.alpha_composite(base, overlay)
    return base, bounds, route_canvas_points(args, track, bounds, zoom), zoom


def draw_route_transition_frame(
    args: argparse.Namespace,
    base: Image.Image,
    track: Track,
    bounds: tuple[float, float, float, float],
    zoom: int,
    full_route: list[tuple[int, int]],
    journey_start_s: float,
    start_s: float,
    end_s: float,
    progress: float,
    label: str,
    place_name: str | None = None,
    place_hold_progress: float = 0.0,
) -> Image.Image:
    if end_s < start_s:
        end_s += track.total_m
    current_s = start_s + (end_s - start_s) * smoothstep(progress)
    frame = base.copy()
    overlay = Image.new("RGBA", (args.width, args.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.line(full_route, fill=(218, 230, 218, 82), width=scale_px(args, 8, 2), joint="curve")
    draw.line(full_route, fill=(16, 59, 44, 115), width=scale_px(args, 3, 1), joint="curve")

    past_steps = max(80, min(700, int(max(1.0, current_s - journey_start_s) / 8)))
    past_segment = sample_route_segment(args, track, bounds, zoom, journey_start_s, current_s, steps=past_steps)
    if len(past_segment) > 1:
        draw.line(past_segment, fill=(255, 255, 255, 180), width=scale_px(args, 16, 4), joint="curve")
        draw.line(past_segment, fill=(35, 139, 92, 230), width=scale_px(args, 10, 3), joint="curve")

    segment = sample_route_segment(args, track, bounds, zoom, start_s, current_s, steps=220)
    if len(segment) > 1:
        draw.line(segment, fill=(255, 255, 255, 238), width=scale_px(args, 22, 5), joint="curve")
        draw.line(segment, fill=(34, 194, 111, 255), width=scale_px(args, 14, 3), joint="curve")
        draw.line(segment, fill=(202, 239, 134, 235), width=scale_px(args, 5, 2), joint="curve")
    lat, lon = route_point_at(track, current_s)
    hx, hy = point_to_canvas(args, bounds, lat, lon, zoom)
    pulse = 0.5 + 0.5 * math.sin(progress * math.tau * 4.0)
    radius = int(scale_px(args, 34, 8) + scale_px(args, 13, 3) * pulse)
    draw.ellipse((hx - radius, hy - radius, hx + radius, hy + radius), fill=(43, 208, 125, 60))
    outer = scale_px(args, 23, 6)
    inner = scale_px(args, 14, 4)
    core = scale_px(args, 5, 2)
    draw.ellipse((hx - outer, hy - outer, hx + outer, hy + outer), fill=(255, 255, 255, 245))
    draw.ellipse((hx - inner, hy - inner, hx + inner, hy + inner), fill=(20, 118, 72, 255))
    draw.ellipse((hx - core, hy - core, hx + core, hy + core), fill=(238, 232, 143, 255))
    title_font = load_font(scale_px(args, 56, 20), bold=True)
    small_font = load_font(scale_px(args, 35, 14))
    title_x = scale_px(args, 286, 84)
    draw.text((title_x, scale_px(args, 72, 14)), args.title, font=title_font, fill=(247, 250, 236, 235))
    draw.text((title_x, scale_px(args, 148, 40)), label, font=small_font, fill=(213, 231, 184, 220))
    if place_name and place_hold_progress > 0:
        draw_place_name_card(args, overlay, place_name, place_hold_progress)
    frame.alpha_composite(overlay)
    return frame


def route_duration_for_distance(args: argparse.Namespace, walk_m: float) -> float:
    if args.route_duration is not None:
        return max(0.5, args.route_duration)
    low = min(args.route_min_duration, args.route_max_duration)
    high = max(args.route_min_duration, args.route_max_duration)
    span = max(1.0, args.route_max_distance_m - args.route_min_distance_m)
    ratio = (walk_m - args.route_min_distance_m) / span
    return low + smoothstep(ratio) * (high - low)


def render_route_clip(
    args: argparse.Namespace,
    track: Track,
    journey_start_s: float,
    source: StopGroup,
    target_group: StopGroup,
    encoder: str,
) -> Path:
    start_s = journey_start_s + source.order_s
    end_s = journey_start_s + target_group.order_s
    if end_s <= start_s:
        end_s = start_s + ((target_group.median_s - source.median_s) % track.total_m)
    walk_m = max(0.0, end_s - start_s)
    animation_duration = route_duration_for_distance(args, walk_m)
    hold_duration = max(0.0, args.place_name_hold_duration)
    total_duration = animation_duration + hold_duration
    place_name = place_name_for_group(target_group)
    route_base, bounds, full_route, zoom = build_route_base(args, track, start_s, end_s, walk_m)
    safe_place = re.sub(r"[^a-z0-9]+", "_", place_name.lower()).strip("_")[:40] or target_group.label.lower()
    path = args.build_dir / "clips" / (
        f"route_v3_{source.index:02d}_to_{target_group.index:02d}_"
        f"a{int(round(animation_duration * 10)):03d}_h{int(round(hold_duration * 10)):03d}_"
        f"z{zoom}_{safe_place}.mp4"
    )
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{args.width}x{args.height}",
        "-r",
        str(args.fps),
        "-i",
        "-",
        "-an",
        *encoder_args(encoder, args.gpu),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(path),
    ]
    print(
        f"Rendering route transition {source.label} -> {target_group.label} "
        f"({walk_m:.0f}m, {animation_duration:.1f}s + {hold_duration:.1f}s name hold, z{zoom})",
        flush=True,
    )
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert process.stdin is not None
    frames = max(1, round(total_duration * args.fps))
    try:
        label = f"{source.label} to {target_group.label} - {walk_m / 1609.344:.2f} mi"
        for frame_index in range(frames):
            elapsed = frame_index / args.fps
            if elapsed < animation_duration:
                progress = elapsed / max(0.001, animation_duration)
                place_hold_progress = 0.0
            else:
                progress = 1.0
                place_hold_progress = (
                    (elapsed - animation_duration) / max(0.001, hold_duration)
                    if hold_duration > 0
                    else 0.0
                )
            frame = draw_route_transition_frame(
                args,
                route_base,
                track,
                bounds,
                zoom,
                full_route,
                journey_start_s,
                start_s,
                end_s,
                progress,
                label,
                place_name,
                place_hold_progress,
            )
            process.stdin.write(frame.convert("RGB").tobytes())
    finally:
        process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError(f"ffmpeg failed for route clip {path}")
    return path


def render_media_clips(args: argparse.Namespace, groups: list[StopGroup], encoder: str) -> dict[Path, Path]:
    rendered: dict[Path, Path] = {}
    total = sum(len(group.items) for group in groups)
    done = 0
    for group in groups:
        for index, item in enumerate(group.items, start=1):
            done += 1
            target = clip_path_for_media(args, group, item, index)
            if item.kind == "image":
                print(f"[{done}/{total}] Image {item.path.name}", flush=True)
                render_image_clip(args, item.path, target, encoder)
            else:
                print(f"[{done}/{total}] Video {item.path.name}", flush=True)
                render_video_clip(args, item.path, target, item.duration, encoder)
            rendered[item.path] = target
    return rendered


def concatenate_clips(args: argparse.Namespace, clips: list[Path], encoder: str) -> Path:
    concat_file = args.build_dir / "concat.txt"
    concat_file.write_text("".join(f"file '{path.resolve()}'\n" for path in clips), encoding="utf-8")
    target = args.build_dir / "body-noaudio.mp4"
    try:
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(target)])
    except subprocess.CalledProcessError:
        print("Concat copy failed; re-encoding concat output.", flush=True)
        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-an",
                *encoder_args(encoder, args.gpu),
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(target),
            ]
        )
    return target


def add_music(args: argparse.Namespace, video: Path) -> Path:
    duration = ffprobe_duration(video)
    fade_start = max(0.0, duration - 6.0)
    audio = args.build_dir / "music-faded.m4a"
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(args.music),
            "-map",
            "0:a:0",
            "-vn",
            "-t",
            f"{duration:.3f}",
            "-af",
            f"afade=t=in:st=0:d=1.0,afade=t=out:st={fade_start:.3f}:d=6.0",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(audio),
        ]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(args.output),
        ]
    )
    return args.output


def clean_cache(args: argparse.Namespace) -> None:
    for relative in ("clips", "staged-images", "staged-frames"):
        path = args.build_dir / relative
        if path.exists():
            for child in path.iterdir():
                if child.is_file():
                    child.unlink()


def render(args: argparse.Namespace, track: Track, groups: list[StopGroup]) -> Path:
    encoder = choose_encoder(args)
    print(f"Using encoder: {encoder}", flush=True)
    if args.clean:
        clean_cache(args)

    clips: list[Path] = [normalize_title(args, encoder)]
    media_clips = render_media_clips(args, groups, encoder)
    journey_start_s = groups[0].median_s if groups else 0.0

    for index, group in enumerate(groups):
        for item in group.items:
            clips.append(media_clips[item.path])
        if index < len(groups) - 1:
            clips.append(render_route_clip(args, track, journey_start_s, group, groups[index + 1], encoder))

    body = concatenate_clips(args, clips, encoder)
    final = add_music(args, body)
    return final


def validate_inputs(args: argparse.Namespace) -> None:
    for path in (args.source_dir, args.gpx, args.title_video, args.music, args.logo):
        if not path.exists():
            raise SystemExit(f"Missing required input: {path}")


def main() -> None:
    args = parse_args()
    validate_inputs(args)
    args.build_dir.mkdir(parents=True, exist_ok=True)
    track = build_track(parse_gpx_points(args.gpx))
    selected = scan_media(args, track)
    groups = group_media(args, track, selected)
    write_reports(args, groups, selected, track)
    if args.dry_run:
        return
    final = render(args, track, groups)
    duration = ffprobe_duration(final)
    print(f"Wrote final slideshow: {final}")
    print(f"Duration: {duration:.1f}s")


if __name__ == "__main__":
    main()
