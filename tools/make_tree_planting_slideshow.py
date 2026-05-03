#!/usr/bin/env python3
"""Render a timestamp-ordered photo slideshow with music."""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import random
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
}

DATE_TAGS = (
    (36867, "DateTimeOriginal"),
    (36868, "DateTimeDigitized"),
    (306, "DateTime"),
)

TITLE = "Earth Day Volunteering 2026"
WIDTH = 1920
HEIGHT = 1080
FPS = 15
TITLE_DURATION = 6.0
TRANSITION_DURATION = 0.6
FIT_SCALE_STEP = 0.001

AUDIO_FILES = [
    "/mnt/synology-drive/backup/qnap-homes/zhang/chinese-music-archive/chinese-music/Children/KD03.Green_Motherland.mp3",
    "/mnt/synology-drive/backup/qnap-homes/zhang/chinese-music-archive/chinese-music/Children/KD02.Our_Fields.mp3",
    "/mnt/synology-drive/backup/qnap-homes/zhang/chinese-music-archive/chinese-music/Children/KD10.Happy_Holidays.mp3",
    "/mnt/synology-drive/backup/qnap-homes/zhang/chinese-music-archive/chinese-music/Children/KD25.QingHai_Blue.mp3",
    "/mnt/synology-drive/backup/qnap-homes/zhang/chinese-music-archive/chinese-music/Taiwan_HongKong/T14.Blue_Flower_Grass.au",
    "/mnt/synology-drive/backup/qnap-homes/zhang/chinese-music-archive/chinese-music/Taiwan_HongKong/T02.Tomorrow_Be_Better.au",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image-dir",
        type=Path,
        default=Path("/home/zhihongz/codex-workspace/tree-planting"),
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=Path("/home/zhihongz/codex-workspace/tree-planting-slideshow-build"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/home/zhihongz/codex-workspace/tree-planting/Earth_Day_Volunteering_2026_slideshow.mp4"),
    )
    parser.add_argument("--limit", type=int, default=0, help="Render only the first N images.")
    parser.add_argument(
        "--sample-count",
        type=int,
        default=0,
        help="Render N images sampled evenly from the timestamp-sorted image set.",
    )
    parser.add_argument("--title", default=TITLE)
    parser.add_argument(
        "--audio",
        type=Path,
        action="append",
        default=[],
        help="Audio file to include. Repeat this option for multiple tracks. Defaults to the Earth Day music list.",
    )
    parser.add_argument(
        "--audio-list",
        type=Path,
        help="Text file containing one audio path per line. Blank lines and lines starting with # are ignored.",
    )
    parser.add_argument("--title-duration", type=float, default=TITLE_DURATION)
    parser.add_argument("--transition-duration", type=float, default=TRANSITION_DURATION)
    parser.add_argument(
        "--encoder",
        choices=("auto", "h264_nvenc", "libx264"),
        default="auto",
        help="Video encoder. auto prefers NVIDIA NVENC when available.",
    )
    parser.add_argument("--gpu", type=int, default=1, help="NVENC GPU index to use.")
    parser.add_argument("--fps", type=int, default=FPS)
    parser.add_argument("--width", type=int, default=WIDTH)
    parser.add_argument("--height", type=int, default=HEIGHT)
    parser.add_argument(
        "--motion",
        choices=("static", "kenburns", "fit"),
        default="static",
        help="Use kenburns for full-screen crop, or fit for full-image motion with no cropping.",
    )
    parser.add_argument("--seed", type=int, default=20260422)
    parser.add_argument("--zoom-min", type=float, default=1.03)
    parser.add_argument("--zoom-max", type=float, default=1.16)
    parser.add_argument(
        "--max-duration",
        type=float,
        default=0.0,
        help="Cap output duration in seconds. Useful for quick test renders.",
    )
    return parser.parse_args()


def audio_paths_from_args(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []
    if args.audio_list:
        for line in args.audio_list.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                paths.append(Path(stripped))
    paths.extend(args.audio)
    return paths or [Path(path) for path in AUDIO_FILES]


def run(command: list[str]) -> None:
    print(" ".join(command[:4]) + (" ..." if len(command) > 4 else ""))
    subprocess.run(command, check=True)


def probe_duration(path: Path) -> float:
    completed = subprocess.run(
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
        check=True,
        capture_output=True,
        text=True,
    )
    return float(completed.stdout.strip())


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


def image_timestamp(path: Path) -> tuple[datetime, str]:
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if exif:
                for tag, label in DATE_TAGS:
                    parsed = parse_datetime(exif.get(tag))
                    if parsed:
                        return parsed, label
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime), "mtime"


def image_paths(image_dir: Path, limit: int = 0) -> list[tuple[Path, datetime, str]]:
    items = []
    for path in image_dir.iterdir():
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            timestamp, source = image_timestamp(path)
            items.append((path, timestamp, source))
    items.sort(key=lambda item: (item[1], item[0].name.lower()))
    return items[:limit] if limit else items


def sample_evenly(
    items: list[tuple[Path, datetime, str]],
    count: int,
) -> list[tuple[Path, datetime, str]]:
    if count <= 0 or count >= len(items):
        return items
    if count == 1:
        return [items[len(items) // 2]]
    indexes = sorted({round(index * (len(items) - 1) / (count - 1)) for index in range(count)})
    while len(indexes) < count:
        largest_gap_position = max(
            range(len(indexes) - 1),
            key=lambda position: indexes[position + 1] - indexes[position],
        )
        midpoint = (indexes[largest_gap_position] + indexes[largest_gap_position + 1]) // 2
        indexes.append(midpoint)
        indexes = sorted(set(indexes))
    return [items[index] for index in indexes[:count]]


def fit_text(draw: ImageDraw.ImageDraw, text: str, font_path: Path, max_width: int) -> ImageFont.FreeTypeFont:
    for size in range(92, 42, -2):
        font = ImageFont.truetype(str(font_path), size)
        if kerned_width(draw, text, font, 4) <= max_width:
            return font
    return ImageFont.truetype(str(font_path), 42)


def kerned_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, spacing: int) -> int:
    width = 0
    for index, char in enumerate(text):
        bbox = draw.textbbox((0, 0), char, font=font)
        width += bbox[2] - bbox[0]
        if index < len(text) - 1:
            width += spacing
    return width


def draw_kerned_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    spacing: int,
    fill: tuple[int, int, int],
) -> None:
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), char, font=font)
        x += bbox[2] - bbox[0] + spacing


def create_title_card(path: Path, title: str) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), (23, 42, 30))
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            t = y / HEIGHT
            shade = int(18 + 34 * (1 - t))
            green = int(38 + 58 * (1 - t))
            pixels[x, y] = (shade, green, int(28 + 15 * t))

    draw = ImageDraw.Draw(image)
    font_path = Path("/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf")
    if not font_path.exists():
        font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf")

    font = fit_text(draw, title.upper(), font_path, int(WIDTH * 0.82))
    spacing = 5
    text_width = kerned_width(draw, title.upper(), font, spacing)
    bbox = draw.textbbox((0, 0), title.upper(), font=font)
    text_height = bbox[3] - bbox[1]
    x = (WIDTH - text_width) // 2
    y = (HEIGHT - text_height) // 2 - 18

    draw.rectangle((0, 0, WIDTH, HEIGHT), outline=(70, 103, 73), width=10)
    draw_kerned_text(draw, (x + 4, y + 5), title.upper(), font, spacing, (8, 20, 12))
    draw_kerned_text(draw, (x, y), title.upper(), font, spacing, (238, 238, 216))
    image.save(path, quality=95)


def create_audio(audio_paths: list[Path], output: Path) -> None:
    manifest = output.with_suffix(f"{output.suffix}.manifest.json")
    source_state = [
        {
            "path": str(path),
            "mtime": path.stat().st_mtime,
            "size": path.stat().st_size,
        }
        for path in audio_paths
    ]
    if output.exists():
        try:
            previous_state = json.loads(manifest.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            previous_state = None
        if previous_state == source_state:
            newest_source = max(path.stat().st_mtime for path in audio_paths)
            if output.stat().st_mtime >= newest_source:
                print(f"reusing {output}", flush=True)
                return

    inputs: list[str] = []
    filters: list[str] = []
    concat_labels: list[str] = []
    total_duration = sum(probe_duration(path) for path in audio_paths)
    fade_start = max(0.0, total_duration - 3.0)
    for index, path in enumerate(audio_paths):
        inputs.extend(["-i", str(path)])
        label = f"a{index}"
        filters.append(f"[{index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[{label}]")
        concat_labels.append(f"[{label}]")
    filters.append(
        f"{''.join(concat_labels)}concat=n={len(audio_paths)}:v=0:a=1,"
        f"afade=t=in:st=0:d=1,afade=t=out:st={fade_start:.3f}:d=3[aout]"
    )
    run(
        [
            "ffmpeg",
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[aout]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output),
        ]
    )
    manifest.write_text(json.dumps(source_state, indent=2), encoding="utf-8")


def encoder_available(name: str) -> bool:
    return (
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", f"encoder={name}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def choose_encoder(requested: str) -> str:
    if requested == "libx264":
        return "libx264"
    if requested == "h264_nvenc":
        if not encoder_available("h264_nvenc"):
            raise SystemExit("Requested h264_nvenc, but ffmpeg does not expose that encoder")
        return "h264_nvenc"
    return "h264_nvenc" if encoder_available("h264_nvenc") else "libx264"


def compose_photo_frame(path: Path) -> Image.Image:
    with Image.open(path) as opened:
        photo = ImageOps.exif_transpose(opened).convert("RGB")

    background = ImageOps.fit(photo, (WIDTH, HEIGHT), method=Image.Resampling.BICUBIC)
    background = background.filter(ImageFilter.GaussianBlur(24))
    background = Image.blend(background, Image.new("RGB", (WIDTH, HEIGHT), (18, 26, 20)), 0.32)

    foreground = photo.copy()
    foreground.thumbnail((int(WIDTH * 0.88), int(HEIGHT * 0.86)), Image.Resampling.LANCZOS)

    canvas = background.convert("RGBA")
    x = (WIDTH - foreground.width) // 2
    y = (HEIGHT - foreground.height) // 2

    shadow_pad = 56
    shadow = Image.new("RGBA", (foreground.width + shadow_pad, foreground.height + shadow_pad), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rectangle(
        (shadow_pad // 2, shadow_pad // 2, shadow_pad // 2 + foreground.width, shadow_pad // 2 + foreground.height),
        fill=(0, 0, 0, 120),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    canvas.alpha_composite(shadow, (x - shadow_pad // 2 + 12, y - shadow_pad // 2 + 16))
    canvas.alpha_composite(foreground.convert("RGBA"), (x, y))
    return canvas.convert("RGB")


def stable_seed(seed: int, index: int, path: Path) -> int:
    digest = hashlib.sha256(f"{seed}:{index}:{path.name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


@dataclass
class MotionClip:
    source: Image.Image
    start_xy: tuple[float, float]
    end_xy: tuple[float, float]
    zoom_start: float
    zoom_end: float
    zoom_max: float

    def frame(self, progress: float) -> Image.Image:
        eased = smoothstep(progress)
        zoom = self.zoom_start + (self.zoom_end - self.zoom_start) * eased
        crop_w = min(self.source.width, max(WIDTH, round(WIDTH * self.zoom_max / zoom)))
        crop_h = min(self.source.height, max(HEIGHT, round(HEIGHT * self.zoom_max / zoom)))
        pan_x = self.start_xy[0] + (self.end_xy[0] - self.start_xy[0]) * eased
        pan_y = self.start_xy[1] + (self.end_xy[1] - self.start_xy[1]) * eased
        x = round((self.source.width - crop_w) * pan_x)
        y = round((self.source.height - crop_h) * pan_y)
        cropped = self.source.crop((x, y, x + crop_w, y + crop_h))
        if cropped.size == (WIDTH, HEIGHT):
            return cropped
        return cropped.resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)


@dataclass
class FitMotionClip:
    foreground_max: Image.Image
    shadow_max: Image.Image
    shadow_pad: int
    background: Image.Image
    start_xy: tuple[float, float]
    end_xy: tuple[float, float]
    scale_start: float
    scale_end: float
    asset_cache: dict[tuple[int, int], tuple[Image.Image, Image.Image]] = field(default_factory=dict)

    def assets(self, width: int, height: int) -> tuple[Image.Image, Image.Image]:
        key = (width, height)
        if key not in self.asset_cache:
            if self.foreground_max.size == key:
                foreground = self.foreground_max
            else:
                foreground = self.foreground_max.resize(key, Image.Resampling.BICUBIC)

            shadow_size = (width + self.shadow_pad, height + self.shadow_pad)
            if self.shadow_max.size == shadow_size:
                shadow = self.shadow_max
            else:
                shadow = self.shadow_max.resize(shadow_size, Image.Resampling.BICUBIC)
            self.asset_cache[key] = (foreground, shadow)

        return self.asset_cache[key]

    def frame(self, progress: float) -> Image.Image:
        eased = smoothstep(progress)
        scale_factor = self.scale_start + (self.scale_end - self.scale_start) * eased
        quantized_scale = min(1.0, max(0.01, round(scale_factor / FIT_SCALE_STEP) * FIT_SCALE_STEP))
        width = max(1, round(self.foreground_max.width * quantized_scale))
        height = max(1, round(self.foreground_max.height * quantized_scale))
        foreground, shadow = self.assets(width, height)

        pan_x = self.start_xy[0] + (self.end_xy[0] - self.start_xy[0]) * eased
        pan_y = self.start_xy[1] + (self.end_xy[1] - self.start_xy[1]) * eased
        x = round((WIDTH - width) * pan_x)
        y = round((HEIGHT - height) * pan_y)

        canvas = self.background.copy()
        canvas.alpha_composite(
            shadow,
            (
                x - self.shadow_pad // 2 + self.shadow_pad // 5,
                y - self.shadow_pad // 2 + self.shadow_pad // 4,
            ),
        )
        canvas.alpha_composite(foreground, (x, y))
        return canvas.convert("RGB")


def create_motion_clip(path: Path, index: int, seed: int, zoom_min: float, zoom_max: float) -> MotionClip:
    rng = random.Random(stable_seed(seed, index, path))
    with Image.open(path) as opened:
        photo = ImageOps.exif_transpose(opened).convert("RGB")

    target_w = math.ceil(WIDTH * zoom_max)
    target_h = math.ceil(HEIGHT * zoom_max)
    scale = max(target_w / photo.width, target_h / photo.height)
    source_size = (math.ceil(photo.width * scale), math.ceil(photo.height * scale))
    source = photo.resize(source_size, Image.Resampling.LANCZOS)

    start_xy = (rng.random(), rng.random())
    end_xy = (rng.random(), rng.random())
    if abs(start_xy[0] - end_xy[0]) + abs(start_xy[1] - end_xy[1]) < 0.45:
        end_xy = (1.0 - start_xy[0], 1.0 - start_xy[1])

    if rng.random() < 0.72:
        zoom_start = rng.uniform(zoom_min, (zoom_min + zoom_max) / 2)
        zoom_end = rng.uniform((zoom_min + zoom_max) / 2, zoom_max)
    else:
        zoom_start = rng.uniform((zoom_min + zoom_max) / 2, zoom_max)
        zoom_end = rng.uniform(zoom_min, (zoom_min + zoom_max) / 2)

    return MotionClip(
        source=source,
        start_xy=start_xy,
        end_xy=end_xy,
        zoom_start=zoom_start,
        zoom_end=zoom_end,
        zoom_max=zoom_max,
    )


def create_fit_motion_clip(path: Path, index: int, seed: int) -> FitMotionClip:
    rng = random.Random(stable_seed(seed, index, path))
    with Image.open(path) as opened:
        photo = ImageOps.exif_transpose(opened).convert("RGB")

    background = ImageOps.fit(photo, (WIDTH, HEIGHT), method=Image.Resampling.BICUBIC)
    background = background.filter(ImageFilter.GaussianBlur(max(18, round(min(WIDTH, HEIGHT) * 0.022))))
    background = Image.blend(background, Image.new("RGB", (WIDTH, HEIGHT), (15, 22, 18)), 0.36)
    background = background.convert("RGBA")

    max_width = round(WIDTH * 0.94)
    max_height = round(HEIGHT * 0.88)
    fit_scale = min(max_width / photo.width, max_height / photo.height)
    foreground_size = (max(1, round(photo.width * fit_scale)), max(1, round(photo.height * fit_scale)))
    foreground_max = photo.resize(foreground_size, Image.Resampling.LANCZOS).convert("RGBA")
    shadow_pad = max(28, round(min(WIDTH, HEIGHT) * 0.026))
    shadow_max = Image.new(
        "RGBA",
        (foreground_max.width + shadow_pad, foreground_max.height + shadow_pad),
        (0, 0, 0, 0),
    )
    shadow_draw = ImageDraw.Draw(shadow_max)
    shadow_draw.rectangle(
        (
            shadow_pad // 2,
            shadow_pad // 2,
            shadow_pad // 2 + foreground_max.width,
            shadow_pad // 2 + foreground_max.height,
        ),
        fill=(0, 0, 0, 115),
    )
    shadow_max = shadow_max.filter(ImageFilter.GaussianBlur(max(10, shadow_pad // 3)))

    start_xy = (0.5 + rng.uniform(-0.14, 0.14), 0.5 + rng.uniform(-0.11, 0.11))
    end_xy = (0.5 + rng.uniform(-0.14, 0.14), 0.5 + rng.uniform(-0.11, 0.11))
    if abs(start_xy[0] - end_xy[0]) + abs(start_xy[1] - end_xy[1]) < 0.12:
        end_xy = (1.0 - start_xy[0], 1.0 - start_xy[1])

    scale_start = 1.0
    scale_end = 1.0

    return FitMotionClip(
        foreground_max=foreground_max,
        shadow_max=shadow_max,
        shadow_pad=shadow_pad,
        background=background,
        start_xy=start_xy,
        end_xy=end_xy,
        scale_start=scale_start,
        scale_end=scale_end,
    )


def write_frame(process: subprocess.Popen[bytes], frame: Image.Image) -> None:
    if process.stdin is None:
        raise RuntimeError("ffmpeg stdin is unavailable")
    try:
        process.stdin.write(frame.tobytes())
    except BrokenPipeError as exc:
        process.wait()
        raise RuntimeError(f"ffmpeg stopped while receiving frames, exit code {process.returncode}") from exc


def write_repeated_frames(process: subprocess.Popen[bytes], frame: Image.Image, count: int) -> None:
    if count <= 0:
        return
    if process.stdin is None:
        raise RuntimeError("ffmpeg stdin is unavailable")
    frame_bytes = frame.tobytes()
    for _ in range(count):
        try:
            process.stdin.write(frame_bytes)
        except BrokenPipeError as exc:
            process.wait()
            raise RuntimeError(f"ffmpeg stopped while receiving frames, exit code {process.returncode}") from exc


def write_crossfade_frames(
    process: subprocess.Popen[bytes],
    current: Image.Image,
    next_frame: Image.Image,
    count: int,
) -> None:
    if count <= 0:
        return
    for frame_index in range(1, count + 1):
        alpha = frame_index / (count + 1)
        write_frame(process, Image.blend(current, next_frame, alpha))


def video_command(
    output: Path,
    audio_file: Path,
    duration: float,
    fps: int,
    encoder: str,
    gpu: int,
) -> list[str]:
    base = [
        "ffmpeg",
        "-y",
        "-thread_queue_size",
        "512",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-i",
        str(audio_file),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
    ]
    if encoder == "h264_nvenc":
        pixel_scale = max(1.0, (WIDTH * HEIGHT) / (1920 * 1080))
        bitrate = 8 if pixel_scale <= 1.0 else math.ceil(8 * math.sqrt(pixel_scale))
        maxrate = 18 if pixel_scale <= 1.0 else math.ceil(18 * math.sqrt(pixel_scale))
        base.extend(
            [
                "-c:v",
                "h264_nvenc",
                "-gpu",
                str(gpu),
                "-preset",
                "p4",
                "-tune",
                "hq",
                "-rc",
                "vbr",
                "-cq",
                "20",
                "-b:v",
                f"{bitrate}M",
                "-maxrate",
                f"{maxrate}M",
                "-bufsize",
                f"{maxrate * 2}M",
                "-spatial-aq",
                "1",
            ]
        )
    else:
        base.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
    base.extend(
        [
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-t",
            f"{duration:.3f}",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return base


def render_streamed_slideshow(
    image_items: list[tuple[Path, datetime, str]],
    title_card: Path,
    audio_file: Path,
    output: Path,
    duration: float,
    fps: int,
    encoder: str,
    gpu: int,
) -> None:
    total_frames = max(1, math.ceil(duration * fps))
    title_frames = min(total_frames, max(1, round(TITLE_DURATION * fps)))
    transition_frames = max(1, round(TRANSITION_DURATION * fps))
    temp_output = output.with_name(f"{output.stem}.tmp{output.suffix}")
    if temp_output.exists():
        temp_output.unlink()

    command = video_command(temp_output, audio_file, duration, fps, encoder, gpu)
    print(" ".join(command[:18]) + " ...", flush=True)
    process = subprocess.Popen(command, stdin=subprocess.PIPE)

    frames_written = 0
    with Image.open(title_card) as opened:
        title_frame = opened.convert("RGB")
    current = compose_photo_frame(image_items[0][0])

    title_xfade_frames = min(transition_frames, max(0, title_frames - 1))
    title_hold_frames = title_frames - title_xfade_frames
    write_repeated_frames(process, title_frame, title_hold_frames)
    frames_written += title_hold_frames
    write_crossfade_frames(process, title_frame, current, title_xfade_frames)
    frames_written += title_xfade_frames

    image_frames = max(0, total_frames - frames_written)
    image_count = len(image_items)
    for index, (image_path, _, _) in enumerate(image_items):
        segment_start = round(index * image_frames / image_count)
        segment_end = round((index + 1) * image_frames / image_count)
        segment_frames = max(0, segment_end - segment_start)
        next_frame = compose_photo_frame(image_items[index + 1][0]) if index + 1 < image_count else None
        if next_frame is None:
            write_repeated_frames(process, current, segment_frames)
            frames_written += segment_frames
        else:
            xfade_frames = min(transition_frames, max(0, segment_frames - 1))
            hold_frames = segment_frames - xfade_frames
            write_repeated_frames(process, current, hold_frames)
            write_crossfade_frames(process, current, next_frame, xfade_frames)
            frames_written += hold_frames + xfade_frames
            current = next_frame
        if index == 0 or (index + 1) % 25 == 0 or index + 1 == image_count:
            print(f"rendered {index + 1}/{image_count} images", flush=True)

    if frames_written < total_frames:
        write_repeated_frames(process, current, total_frames - frames_written)

    if process.stdin is not None:
        process.stdin.close()
    return_code = process.wait()
    if return_code != 0:
        raise SystemExit(f"ffmpeg render failed with exit code {return_code}")
    temp_output.replace(output)


def render_kenburns_slideshow(
    image_items: list[tuple[Path, datetime, str]],
    title_card: Path,
    audio_file: Path,
    output: Path,
    duration: float,
    fps: int,
    encoder: str,
    gpu: int,
    seed: int,
    zoom_min: float,
    zoom_max: float,
) -> None:
    total_frames = max(1, math.ceil(duration * fps))
    title_frames = min(total_frames, max(1, round(TITLE_DURATION * fps)))
    transition_frames = max(1, round(TRANSITION_DURATION * fps))
    image_frames = max(1, total_frames - title_frames)
    image_count = len(image_items)
    boundaries = [round(index * image_frames / image_count) for index in range(image_count + 1)]
    temp_output = output.with_name(f"{output.stem}.tmp{output.suffix}")
    if temp_output.exists():
        temp_output.unlink()

    command = video_command(temp_output, audio_file, duration, fps, encoder, gpu)
    print(" ".join(command[:18]) + " ...", flush=True)
    process = subprocess.Popen(command, stdin=subprocess.PIPE)

    with Image.open(title_card) as opened:
        title_frame = opened.convert("RGB")

    clip_cache: dict[int, MotionClip] = {}

    def clip(index: int) -> MotionClip:
        if index not in clip_cache:
            clip_cache[index] = create_motion_clip(image_items[index][0], index, seed, zoom_min, zoom_max)
            for cached_index in list(clip_cache):
                if cached_index < index - 1 or cached_index > index + 2:
                    del clip_cache[cached_index]
        return clip_cache[index]

    title_hold_frames = max(0, title_frames - transition_frames)
    for frame_number in range(title_frames):
        if frame_number < title_hold_frames:
            write_frame(process, title_frame)
            continue
        alpha = smoothstep((frame_number - title_hold_frames + 1) / (transition_frames + 1))
        write_frame(process, Image.blend(title_frame, clip(0).frame(0.0), alpha))

    for t in range(image_frames):
        index = max(0, min(image_count - 1, bisect.bisect_right(boundaries, t) - 1))
        segment_start = boundaries[index]
        segment_end = boundaries[index + 1]
        visible_start = max(0, segment_start - (transition_frames if index > 0 else 0))
        visible_end = max(visible_start + 1, segment_end)
        current_progress = (t - visible_start) / (visible_end - visible_start)
        frame = clip(index).frame(current_progress)

        if index + 1 < image_count and t >= segment_end - transition_frames:
            next_visible_start = segment_end - transition_frames
            next_visible_end = max(next_visible_start + 1, boundaries[index + 2])
            next_progress = (t - next_visible_start) / (next_visible_end - next_visible_start)
            alpha = smoothstep((t - next_visible_start + 1) / (transition_frames + 1))
            frame = Image.blend(frame, clip(index + 1).frame(next_progress), alpha)

        write_frame(process, frame)
        if t == 0 or t + 1 == image_frames or (index + 1) % 25 == 0 and t == segment_start:
            print(f"rendered frame {t + 1}/{image_frames}, image {index + 1}/{image_count}", flush=True)

    if process.stdin is not None:
        process.stdin.close()
    return_code = process.wait()
    if return_code != 0:
        raise SystemExit(f"ffmpeg render failed with exit code {return_code}")
    temp_output.replace(output)


def render_fit_motion_slideshow(
    image_items: list[tuple[Path, datetime, str]],
    title_card: Path,
    audio_file: Path,
    output: Path,
    duration: float,
    fps: int,
    encoder: str,
    gpu: int,
    seed: int,
) -> None:
    total_frames = max(1, math.ceil(duration * fps))
    title_frames = min(total_frames, max(1, round(TITLE_DURATION * fps)))
    transition_frames = max(1, round(TRANSITION_DURATION * fps))
    image_frames = max(1, total_frames - title_frames)
    image_count = len(image_items)
    boundaries = [round(index * image_frames / image_count) for index in range(image_count + 1)]
    temp_output = output.with_name(f"{output.stem}.tmp{output.suffix}")
    if temp_output.exists():
        temp_output.unlink()

    command = video_command(temp_output, audio_file, duration, fps, encoder, gpu)
    print(" ".join(command[:18]) + " ...", flush=True)
    process = subprocess.Popen(command, stdin=subprocess.PIPE)

    with Image.open(title_card) as opened:
        title_frame = opened.convert("RGB")

    clip_cache: dict[int, FitMotionClip] = {}

    def clip(index: int) -> FitMotionClip:
        if index not in clip_cache:
            clip_cache[index] = create_fit_motion_clip(image_items[index][0], index, seed)
            for cached_index in list(clip_cache):
                if cached_index < index - 1 or cached_index > index + 2:
                    del clip_cache[cached_index]
        return clip_cache[index]

    title_hold_frames = max(0, title_frames - transition_frames)
    for frame_number in range(title_frames):
        if frame_number < title_hold_frames:
            write_frame(process, title_frame)
            continue
        alpha = smoothstep((frame_number - title_hold_frames + 1) / (transition_frames + 1))
        write_frame(process, Image.blend(title_frame, clip(0).frame(0.0), alpha))

    for t in range(image_frames):
        index = max(0, min(image_count - 1, bisect.bisect_right(boundaries, t) - 1))
        segment_start = boundaries[index]
        segment_end = boundaries[index + 1]
        visible_start = max(0, segment_start - (transition_frames if index > 0 else 0))
        visible_end = max(visible_start + 1, segment_end)
        current_progress = (t - visible_start) / (visible_end - visible_start)
        frame = clip(index).frame(current_progress)

        if index + 1 < image_count and t >= segment_end - transition_frames:
            next_visible_start = segment_end - transition_frames
            next_visible_end = max(next_visible_start + 1, boundaries[index + 2])
            next_progress = (t - next_visible_start) / (next_visible_end - next_visible_start)
            alpha = smoothstep((t - next_visible_start + 1) / (transition_frames + 1))
            frame = Image.blend(frame, clip(index + 1).frame(next_progress), alpha)

        write_frame(process, frame)
        if t == 0 or t + 1 == image_frames or (index + 1) % 25 == 0 and t == segment_start:
            print(f"rendered frame {t + 1}/{image_frames}, image {index + 1}/{image_count}", flush=True)

    if process.stdin is not None:
        process.stdin.close()
    return_code = process.wait()
    if return_code != 0:
        raise SystemExit(f"ffmpeg render failed with exit code {return_code}")
    temp_output.replace(output)


def media_filter(index: int, label: str) -> str:
    return (
        f"[{index}:v]fps={FPS},scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},setsar=1,format=yuv420p,setpts=PTS-STARTPTS[{label}]"
    )


def create_filter_script(image_count: int, image_duration: float, filter_path: Path) -> None:
    lines: list[str] = []
    total_inputs = image_count + 1
    durations = [TITLE_DURATION] + [image_duration] * image_count
    for index in range(total_inputs):
        lines.append(media_filter(index, f"v{index}"))

    previous = "v0"
    elapsed = durations[0]
    for next_index in range(1, total_inputs):
        offset = elapsed - TRANSITION_DURATION
        out_label = "vout" if next_index == total_inputs - 1 else f"x{next_index}"
        lines.append(
            f"[{previous}][v{next_index}]xfade=transition=fade:"
            f"duration={TRANSITION_DURATION:.3f}:offset={offset:.3f}[{out_label}]"
        )
        previous = out_label
        elapsed += durations[next_index] - TRANSITION_DURATION

    filter_path.write_text(";\n".join(lines), encoding="utf-8")


def write_order_report(items: list[tuple[Path, datetime, str]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["order", "timestamp", "timestamp_source", "path"])
        writer.writeheader()
        for index, (image_path, timestamp, source) in enumerate(items, start=1):
            writer.writerow(
                {
                    "order": index,
                    "timestamp": timestamp.isoformat(sep=" "),
                    "timestamp_source": source,
                    "path": str(image_path),
                }
            )


def main() -> int:
    args = parse_args()
    global WIDTH, HEIGHT, FPS, TITLE_DURATION, TRANSITION_DURATION
    WIDTH = args.width
    HEIGHT = args.height
    FPS = args.fps
    TITLE_DURATION = args.title_duration
    TRANSITION_DURATION = args.transition_duration

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("ffmpeg and ffprobe are required")

    args.build_dir.mkdir(parents=True, exist_ok=True)
    image_items = sample_evenly(image_paths(args.image_dir, args.limit), args.sample_count)
    if not image_items:
        raise SystemExit(f"No images found in {args.image_dir}")

    audio_paths = audio_paths_from_args(args)
    missing_audio = [str(path) for path in audio_paths if not path.exists()]
    if missing_audio:
        raise SystemExit(f"Missing audio files: {missing_audio}")

    title_card = args.build_dir / "title-card.jpg"
    audio_file = args.build_dir / "earth-day-music.m4a"
    order_report = args.build_dir / "timestamp-order.csv"
    summary_file = args.build_dir / "render-summary.json"

    create_title_card(title_card, args.title)
    create_audio(audio_paths, audio_file)
    audio_duration = probe_duration(audio_file)
    render_duration = min(audio_duration, args.max_duration) if args.max_duration > 0 else audio_duration
    image_duration = (render_duration - TITLE_DURATION + len(image_items) * TRANSITION_DURATION) / len(image_items)
    if image_duration <= TRANSITION_DURATION + 0.2:
        raise SystemExit("Audio is too short for the number of images and transition duration")

    write_order_report(image_items, order_report)
    encoder = choose_encoder(args.encoder)

    if args.motion == "kenburns":
        render_kenburns_slideshow(
            image_items=image_items,
            title_card=title_card,
            audio_file=audio_file,
            output=args.output,
            duration=render_duration,
            fps=args.fps,
            encoder=encoder,
            gpu=args.gpu,
            seed=args.seed,
            zoom_min=args.zoom_min,
            zoom_max=args.zoom_max,
        )
    elif args.motion == "fit":
        render_fit_motion_slideshow(
            image_items=image_items,
            title_card=title_card,
            audio_file=audio_file,
            output=args.output,
            duration=render_duration,
            fps=args.fps,
            encoder=encoder,
            gpu=args.gpu,
            seed=args.seed,
        )
    else:
        render_streamed_slideshow(
            image_items=image_items,
            title_card=title_card,
            audio_file=audio_file,
            output=args.output,
            duration=render_duration,
            fps=args.fps,
            encoder=encoder,
            gpu=args.gpu,
        )

    summary = {
        "title": args.title,
        "output": str(args.output),
        "image_count": len(image_items),
        "audio_duration_seconds": audio_duration,
        "render_duration_seconds": render_duration,
        "title_duration_seconds": TITLE_DURATION,
        "transition_duration_seconds": TRANSITION_DURATION,
        "image_clip_duration_seconds": image_duration,
        "new_image_cadence_seconds": image_duration - TRANSITION_DURATION,
        "fps": args.fps,
        "width": WIDTH,
        "height": HEIGHT,
        "motion": args.motion,
        "seed": args.seed if args.motion in {"kenburns", "fit"} else None,
        "zoom_min": args.zoom_min if args.motion == "kenburns" else None,
        "zoom_max": args.zoom_max if args.motion == "kenburns" else None,
        "encoder": encoder,
        "gpu": args.gpu if encoder == "h264_nvenc" else None,
        "audio_paths": [str(path) for path in audio_paths],
        "order_report": str(order_report),
        "audio_file": str(audio_file),
    }
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
