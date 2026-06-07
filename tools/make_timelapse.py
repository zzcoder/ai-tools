#!/usr/bin/env python3
"""Generate a 4K timelapse video from EXIF-timestamped JPEG photos.

The input photo folder is never modified. Source images are sorted by EXIF
timestamp, rendered as ordered 4K frames, then encoded into timelapse.mp4.
Subdirectories are scanned recursively; non-JPEG files are ignored.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


WIDTH = 3840
HEIGHT = 2160
OUTPUT_FPS = 30
FRAME_PATTERN = "ts-%04d.JPG"
IMAGE_EXTENSIONS = {".jpg", ".jpeg"}
DATE_TAGS = (
    (36868, "DateTimeDigitized"),
    (36867, "DateTimeOriginal"),
    (306, "DateTime"),
)


@dataclass(frozen=True)
class PhotoInfo:
    path: Path
    capture_datetime: datetime | None
    timestamp_text: str
    date_source: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("photo_folder", type=Path, help="Folder containing source JPG photos.")
    parser.add_argument("output_folder", type=Path, help="Folder for rendered frames and timelapse.mp4.")
    parser.add_argument("frame_rate", type=float, help="Input timelapse frame rate for ffmpeg.")
    parser.add_argument(
        "--skip-timestamp",
        action="store_true",
        help="Do not draw EXIF timestamp text on output frames.",
    )
    return parser.parse_args()


def parse_exif_datetime(value: Any) -> tuple[datetime, str] | None:
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip().replace("\x00", "")
    text = re.sub(r"\s+", " ", text)
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            capture_datetime = datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt)
            if "%H" in fmt:
                return capture_datetime, capture_datetime.strftime("%Y-%m-%d %H:%M:%S")
            return capture_datetime, capture_datetime.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def read_photo_info(path: Path) -> PhotoInfo:
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            for tag_id, tag_name in DATE_TAGS:
                parsed = parse_exif_datetime(exif.get(tag_id))
                if parsed:
                    capture_datetime, timestamp_text = parsed
                    return PhotoInfo(path, capture_datetime, timestamp_text, tag_name)
    except Exception as exc:
        return PhotoInfo(path, None, "", f"EXIF error: {exc}")
    return PhotoInfo(path, None, "", "filesystem")


def find_photos(photo_folder: Path) -> list[PhotoInfo]:
    photos: list[PhotoInfo] = []
    ignored = 0
    for path in sorted(photo_folder.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            ignored += 1
            continue
        photos.append(read_photo_info(path))
    if not photos:
        raise RuntimeError(f"No JPG photos found in {photo_folder}")
    print(f"found {len(photos)} JPEG photo(s) under {photo_folder}")
    if ignored:
        print(f"ignored {ignored} non-JPEG file(s)")
    return sorted(
        photos,
        key=lambda info: (
            info.capture_datetime is None,
            info.capture_datetime or datetime.max,
            info.path.name.lower(),
        ),
    )


def load_font(point_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/opentype/urw-base35/NimbusMonoPS-Bold.otf",
        "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, point_size)
    return ImageFont.load_default()


def center_crop_4k(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGB")
    scale = max(WIDTH / image.width, HEIGHT / image.height)
    resized = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - WIDTH) // 2)
    top = max(0, (resized.height - HEIGHT) // 2)
    return resized.crop((left, top, left + WIDTH, top + HEIGHT))


def draw_timestamp(frame: Image.Image, timestamp: str, font: ImageFont.ImageFont) -> None:
    if not timestamp:
        return
    draw = ImageDraw.Draw(frame)
    bbox = draw.textbbox((0, 0), timestamp, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (WIDTH - text_w) // 2
    y = HEIGHT - text_h - 100
    pad_x = 26
    pad_y = 16
    shadow_box = (x - pad_x, y - pad_y, x + text_w + pad_x, y + text_h + pad_y)
    draw.rounded_rectangle(shadow_box, radius=18, fill=(0, 0, 0, 115))
    draw.text((x + 4, y + 4), timestamp, font=font, fill=(0, 0, 0))
    draw.text((x, y), timestamp, font=font, fill=(255, 255, 255))


def prepare_output(output_folder: Path) -> tuple[Path, Path, Path]:
    frames_dir = output_folder / "frames"
    video_path = output_folder / "timelapse.mp4"
    manifest_path = output_folder / "manifest.csv"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old_frame in frames_dir.glob("ts-*.JPG"):
        old_frame.unlink()
    return frames_dir, video_path, manifest_path


def render_frames(photos: list[PhotoInfo], frames_dir: Path, skip_timestamp: bool) -> list[tuple[PhotoInfo, Path]]:
    font = load_font(120)
    rendered: list[tuple[PhotoInfo, Path]] = []
    for index, info in enumerate(photos, start=1):
        frame_path = frames_dir / FRAME_PATTERN.replace("%04d", f"{index:04d}")
        with Image.open(info.path) as image:
            frame = center_crop_4k(image)
        if not skip_timestamp:
            draw_timestamp(frame, info.timestamp_text, font)
        frame.save(frame_path, quality=95, subsampling=0)
        rendered.append((info, frame_path))
        print(f"rendered {index:04d}/{len(photos):04d}: {info.path}")
    return rendered


def write_manifest(rendered: list[tuple[PhotoInfo, Path]], manifest_path: Path) -> None:
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frame", "source_photo", "timestamp", "date_source"])
        for info, frame_path in rendered:
            writer.writerow([frame_path.name, str(info.path), info.timestamp_text, info.date_source])


def encode_video(frames_dir: Path, video_path: Path, frame_rate: float) -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required but was not found in PATH")
    video_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(frame_rate),
        "-start_number",
        "1",
        "-i",
        str(frames_dir / FRAME_PATTERN),
        "-c:v",
        "libx264",
        "-r",
        str(OUTPUT_FPS),
        "-pix_fmt",
        "yuv420p",
        str(video_path),
    ]
    print("encoding timelapse.mp4")
    subprocess.run(command, check=True)


def main() -> None:
    args = parse_args()
    if args.frame_rate <= 0:
        raise SystemExit("frame_rate must be greater than 0")
    photo_folder = args.photo_folder.expanduser().resolve()
    output_folder = args.output_folder.expanduser().resolve()
    if not photo_folder.is_dir():
        raise SystemExit(f"Photo folder does not exist: {photo_folder}")

    photos = find_photos(photo_folder)
    frames_dir, video_path, manifest_path = prepare_output(output_folder)
    rendered = render_frames(photos, frames_dir, args.skip_timestamp)
    write_manifest(rendered, manifest_path)
    encode_video(frames_dir, video_path, args.frame_rate)
    print(f"wrote frames: {frames_dir}")
    print(f"wrote manifest: {manifest_path}")
    print(f"wrote video: {video_path}")


if __name__ == "__main__":
    main()
