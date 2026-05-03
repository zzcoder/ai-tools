#!/usr/bin/env python3
import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


AUDIO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".aac",
    ".aif",
    ".aifc",
    ".aiff",
    ".alac",
    ".amr",
    ".au",
    ".caf",
    ".flac",
    ".m4a",
    ".m4b",
    ".mid",
    ".midi",
    ".mp1",
    ".mp2",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".ra",
    ".rm",
    ".snd",
    ".wav",
    ".wave",
    ".webm",
    ".wma",
}


FIELDS = [
    "path",
    "extension",
    "size_bytes",
    "mtime",
    "duration_seconds",
    "format_name",
    "codec_name",
    "codec_long_name",
    "sample_rate",
    "channels",
    "bit_rate",
    "title",
    "artist",
    "album",
    "date",
    "ffprobe_error",
]


def iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().isoformat(timespec="seconds")


def first_audio_stream(probe: dict) -> dict:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "audio":
            return stream
    return probe.get("streams", [{}])[0] if probe.get("streams") else {}


def ffprobe(path: str, timeout: float) -> tuple[dict, str]:
    command = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {}, f"ffprobe timed out after {timeout:g}s"

    if result.returncode != 0:
        return {}, (result.stderr or f"ffprobe exited {result.returncode}").strip()

    try:
        return json.loads(result.stdout), ""
    except json.JSONDecodeError as exc:
        return {}, f"invalid ffprobe JSON: {exc}"


def audio_files(root: Path):
    for current_root, _, files in os.walk(root):
        for name in files:
            path = Path(current_root) / name
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                yield path


def catalog(root: Path, output: Path, timeout: float) -> int:
    count = 0
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()

        for path in audio_files(root):
            count += 1
            if count % 100 == 0:
                print(f"cataloged {count} audio files...", file=sys.stderr, flush=True)

            try:
                stat = path.stat()
                size = stat.st_size
                mtime = iso_time(stat.st_mtime)
            except OSError as exc:
                size = ""
                mtime = ""
                probe = {}
                error = f"stat failed: {exc}"
            else:
                probe, error = ffprobe(str(path), timeout)

            stream = first_audio_stream(probe)
            fmt = probe.get("format", {})
            tags = fmt.get("tags", {}) or {}

            writer.writerow(
                {
                    "path": str(path),
                    "extension": path.suffix.lower(),
                    "size_bytes": size,
                    "mtime": mtime,
                    "duration_seconds": fmt.get("duration", ""),
                    "format_name": fmt.get("format_name", ""),
                    "codec_name": stream.get("codec_name", ""),
                    "codec_long_name": stream.get("codec_long_name", ""),
                    "sample_rate": stream.get("sample_rate", ""),
                    "channels": stream.get("channels", ""),
                    "bit_rate": fmt.get("bit_rate") or stream.get("bit_rate", ""),
                    "title": tags.get("title") or tags.get("TITLE", ""),
                    "artist": tags.get("artist") or tags.get("ARTIST", ""),
                    "album": tags.get("album") or tags.get("ALBUM", ""),
                    "date": tags.get("date") or tags.get("DATE", ""),
                    "ffprobe_error": error,
                }
            )

    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Catalog audio files with basic ffprobe metadata.")
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ffprobe-timeout", type=float, default=10.0)
    args = parser.parse_args()

    count = catalog(args.root, args.output, args.ffprobe_timeout)
    print(f"Wrote {count} audio rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
