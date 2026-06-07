#!/usr/bin/env python3
"""Decorate an event photo with GPT Image, then restore original photo pixels.

This wraps the existing imagegen CLI workflow:
1. Create a mask that protects the human/photo region while allowing GPT to edit decorations.
2. Ask GPT Image to add event text and decorations.
3. Resize the GPT output to the original dimensions.
4. Feather-select the human/photo region from the original and overlay it on the GPT result.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps


DEFAULT_OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
DEFAULT_IMAGEGEN_CLI = Path.home() / ".codex" / "skills" / ".system" / "imagegen" / "scripts" / "image_gen.py"
DEFAULT_TMP_PYTHON = Path("/tmp/imagegen-cli-venv/bin/python")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Decorate an event group photo with GPT Image and restore original people pixels."
    )
    parser.add_argument("--image", required=True, type=Path, help="Source event/group photo.")
    parser.add_argument("--output", required=True, type=Path, help="Final decorated image path.")
    parser.add_argument("--title", required=True, help="Main event title text.")
    parser.add_argument("--year", required=True, help="Year text.")
    parser.add_argument("--subtitle", required=True, help="Subtitle/team text.")
    parser.add_argument("--theme", default="warm event celebration", help="Theme or occasion for the decoration.")
    parser.add_argument(
        "--accent-instructions",
        default=(
            "Add tasteful celebratory accents around the edges only, using colors and textures "
            "that match the original photo."
        ),
        help="Decoration guidance for the GPT edit.",
    )
    parser.add_argument(
        "--skip-gpt-text",
        action="store_true",
        help="Ask GPT to create only decorations/background, leaving final text for a deterministic local overlay.",
    )
    parser.add_argument("--build-dir", type=Path, help="Directory for mask, prompt, raw GPT output, and manifest.")
    parser.add_argument("--model", default="gpt-image-2", help="GPT Image model.")
    parser.add_argument("--size", default="3072x2048", help="Image API output size.")
    parser.add_argument("--quality", default="high", choices=["low", "medium", "high", "auto"])
    parser.add_argument("--imagegen-cli", type=Path, default=DEFAULT_IMAGEGEN_CLI)
    parser.add_argument(
        "--imagegen-python",
        default=os.environ.get("IMAGEGEN_PYTHON")
        or (str(DEFAULT_TMP_PYTHON) if DEFAULT_TMP_PYTHON.exists() else sys.executable),
        help="Python interpreter with the OpenAI SDK installed for image_gen.py.",
    )
    parser.add_argument("--openclaw-config", type=Path, default=DEFAULT_OPENCLAW_CONFIG)
    parser.add_argument("--reuse-gpt", type=Path, help="Skip API call and composite from this raw GPT output.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output/build artifacts.")
    parser.add_argument("--header-ratio", type=float, default=0.235, help="Editable top/header fraction.")
    parser.add_argument("--side-ratio", type=float, default=0.030, help="Editable side-border fraction.")
    parser.add_argument("--bottom-start-ratio", type=float, default=0.915, help="Editable bottom band start fraction.")
    parser.add_argument("--paste-start-ratio", type=float, default=0.292, help="Original-photo paste-back start fraction.")
    parser.add_argument("--paste-end-ratio", type=float, default=0.902, help="Original-photo paste-back end fraction.")
    parser.add_argument(
        "--human-rect",
        action="append",
        default=[],
        metavar="X1,Y1,X2,Y2",
        help=(
            "Human/source-photo region to protect during GPT edit and overlay from the original afterward; "
            "may be repeated. Defaults to a full-width region from --paste-start-ratio to --paste-end-ratio."
        ),
    )
    parser.add_argument(
        "--human-feather-px",
        type=int,
        default=50,
        help="Pixel feather radius for the original human-region overlay.",
    )
    parser.add_argument("--feather-ratio", type=float, default=0.018, help="Legacy feather fraction used if --human-feather-px is 0.")
    parser.add_argument(
        "--no-auto-preserve-source-structures",
        dest="auto_preserve_source_structures",
        action="store_false",
        help="Disable automatic preservation of poles, flags, buildings, trees, and other real photo structures.",
    )
    parser.add_argument(
        "--auto-preserve-end-ratio",
        type=float,
        help="Bottom edge for automatic source-structure preservation; defaults to the paste-back feather boundary.",
    )
    parser.add_argument(
        "--protect-rect",
        action="append",
        default=[],
        metavar="X1,Y1,X2,Y2",
        help="Additional source-photo rectangle to keep unedited and restore exactly; may be repeated.",
    )
    parser.add_argument(
        "--restore-non-sky-rect",
        action="append",
        default=[],
        metavar="X1,Y1,X2,Y2",
        help="Restore only source pixels that do not look like blue sky inside this rectangle; may be repeated.",
    )
    parser.set_defaults(auto_preserve_source_structures=True)
    return parser.parse_args()


def parse_rect(value: str) -> tuple[int, int, int, int]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(f"Expected X1,Y1,X2,Y2, got: {value}")
    try:
        x1, y1, x2, y2 = (int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Rectangle coordinates must be integers: {value}") from exc
    if x2 <= x1 or y2 <= y1:
        raise argparse.ArgumentTypeError(f"Rectangle must have positive width and height: {value}")
    return x1, y1, x2, y2


def resolved_protect_rects(args: argparse.Namespace, width: int, height: int) -> list[tuple[int, int, int, int]]:
    return resolved_rects(args.protect_rect, width, height)


def resolved_restore_non_sky_rects(args: argparse.Namespace, width: int, height: int) -> list[tuple[int, int, int, int]]:
    return resolved_rects(args.restore_non_sky_rect, width, height)


def resolved_human_rects(args: argparse.Namespace, width: int, height: int) -> list[tuple[int, int, int, int]]:
    rects = resolved_rects(args.human_rect, width, height)
    if rects:
        return rects
    start = max(0, min(height, int(height * args.paste_start_ratio)))
    end = max(0, min(height, int(height * args.paste_end_ratio)))
    if end <= start:
        return []
    return [(0, start, width, end)]


def resolved_rects(values: list[str], width: int, height: int) -> list[tuple[int, int, int, int]]:
    rects: list[tuple[int, int, int, int]] = []
    for value in values:
        x1, y1, x2, y2 = parse_rect(value)
        x1 = max(0, min(width, x1))
        x2 = max(0, min(width, x2))
        y1 = max(0, min(height, y1))
        y2 = max(0, min(height, y2))
        if x2 > x1 and y2 > y1:
            rects.append((x1, y1, x2, y2))
    return rects


def build_feathered_rect_mask(
    size: tuple[int, int], rects: list[tuple[int, int, int, int]], feather_px: int
) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for rect in rects:
        draw.rectangle(rect, fill=255)
    if feather_px > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(feather_px))
    return mask


def build_source_structure_mask(region: Image.Image) -> Image.Image:
    rgb = region.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    non_sky_mask = Image.new("L", (width, height), 0)
    non_sky_pixels = non_sky_mask.load()
    non_sky_count = 0

    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            sky_blue = b > r + 30 and g > r + 10 and b > 120 and g > 80
            if not sky_blue:
                non_sky_pixels[x, y] = 255
                non_sky_count += 1

    non_sky_mask = non_sky_mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(0.7))

    edge_mask = ImageOps.grayscale(rgb).filter(ImageFilter.FIND_EDGES)
    edge_mask = edge_mask.point(lambda value: 255 if value > 18 else 0)
    edge_mask = edge_mask.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(1.0))

    # Blue-sky detection works well for typical outdoor event photos. If too much
    # of the region is classified as non-sky, fall back to edge preservation so
    # sunset/overcast skies do not erase the generated title.
    non_sky_ratio = non_sky_count / max(1, width * height)
    if non_sky_ratio > 0.45:
        return edge_mask
    return ImageChops.lighter(non_sky_mask, edge_mask)


def build_source_preserve_mask(image: Image.Image, args: argparse.Namespace) -> Image.Image:
    width, height = image.size
    preserve_mask = Image.new("L", (width, height), 0)
    rects = resolved_restore_non_sky_rects(args, width, height)

    if args.auto_preserve_source_structures:
        feather = max(1, int(height * args.feather_ratio))
        default_end = int(height * args.paste_start_ratio) + feather
        if args.auto_preserve_end_ratio is not None:
            default_end = int(height * args.auto_preserve_end_ratio)
        auto_end = max(int(height * args.header_ratio), default_end)
        auto_end = max(1, min(height, auto_end))
        rects.append((0, 0, width, auto_end))

    for x1, y1, x2, y2 in rects:
        region = image.crop((x1, y1, x2, y2))
        structure_mask = build_source_structure_mask(region)
        existing = preserve_mask.crop((x1, y1, x2, y2))
        preserve_mask.paste(ImageChops.lighter(existing, structure_mask), (x1, y1))

    return preserve_mask


def load_openai_api_key(openclaw_config: Path) -> str:
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]

    config_path = openclaw_config.expanduser()
    if not config_path.exists():
        raise SystemExit(
            "OPENAI_API_KEY is unset and OpenClaw config was not found. "
            "Set OPENAI_API_KEY or pass --openclaw-config."
        )

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse OpenClaw config: {config_path}") from exc

    provider = config.get("models", {}).get("providers", {}).get("openai", {})
    api_key_config = provider.get("apiKey")
    if isinstance(api_key_config, str) and api_key_config:
        return api_key_config
    if isinstance(api_key_config, dict) and api_key_config.get("source") == "env":
        env_name = str(api_key_config.get("id") or "OPENAI_API_KEY")
        value = os.environ.get(env_name)
        if value:
            return value
        raise SystemExit(
            f"OpenClaw points OpenAI apiKey to environment variable {env_name}, but it is unset."
        )

    raise SystemExit("Could not resolve an OpenAI API key from OPENAI_API_KEY or OpenClaw config.")


def write_mask(source: Path, out: Path, args: argparse.Namespace) -> tuple[int, int]:
    image = Image.open(source).convert("RGB")
    width, height = image.size
    mask = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    draw = ImageDraw.Draw(mask)

    header_y = int(height * args.header_ratio)
    side_w = int(min(width, height) * args.side_ratio)
    bottom_y = int(height * args.bottom_start_ratio)

    draw.rectangle([0, 0, width, header_y], fill=(0, 0, 0, 0))
    draw.rectangle([0, 0, side_w, height], fill=(0, 0, 0, 0))
    draw.rectangle([width - side_w, 0, width, height], fill=(0, 0, 0, 0))
    draw.rectangle([0, bottom_y, width, height], fill=(0, 0, 0, 0))
    human_mask = build_feathered_rect_mask(
        (width, height), resolved_human_rects(args, width, height), max(0, args.human_feather_px)
    )
    mask.putalpha(ImageChops.lighter(mask.getchannel("A"), human_mask))
    for rect in resolved_protect_rects(args, width, height):
        draw.rectangle(rect, fill=(0, 0, 0, 255))
    preserve_mask = build_source_preserve_mask(image, args)
    mask.putalpha(ImageChops.lighter(mask.getchannel("A"), preserve_mask))

    out.parent.mkdir(parents=True, exist_ok=True)
    mask.save(out)
    return width, height


def write_prompt(out: Path, args: argparse.Namespace) -> None:
    text_lines = [line for line in (args.title, args.year, args.subtitle) if line.strip()]
    exact_text = "\n".join(text_lines)
    if args.skip_gpt_text:
        text_request = (
            "The final text will be overlaid later by another tool. Use it only to reserve enough visual space, "
            "but do not render any letters, words, numbers, calligraphy, date text, signature text, "
            f"or placeholder text. Final text to reserve space for:\n{exact_text}\n"
            "Leave a clean, elegant blank banner/header area for the later text overlay."
        )
    else:
        text_request = f"""Add an elegant event-photo header. Include this exact text, spelled exactly, with no missing or cropped characters:
{exact_text}

Place all text high in the open header/background area, centered with generous safe margins. Every character must be fully inside the image canvas; no text may touch or be cut off by the top, left, right, or bottom edges. Make the text smaller and use multiple balanced lines if needed."""
    prompt = f"""Decorate this existing group photo for this theme: {args.theme}.
Do not move, reframe, crop, or change the people.

Only use the editable mask areas: open header/background space, very slim side borders, and a lower edge accent. Keep the central group photo, people, faces, bodies, hands, important props, and real background structures unchanged.

{text_request}

Do not place header elements over faces, people, lamps, signs, flags, poles, buildings, or other real photo structures. {args.accent_instructions} Do not add a large ribbon or decoration over the people. Do not duplicate or misspell text. No extra logos. No watermark.
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(prompt, encoding="utf-8")


def run_gpt_edit(args: argparse.Namespace, mask_path: Path, prompt_path: Path, raw_gpt_path: Path) -> None:
    if not args.imagegen_cli.exists():
        raise SystemExit(f"image_gen.py not found: {args.imagegen_cli}")
    if not Path(args.imagegen_python).exists() and shutil.which(args.imagegen_python) is None:
        raise SystemExit(f"Python interpreter not found: {args.imagegen_python}")

    env = os.environ.copy()
    env["OPENAI_API_KEY"] = load_openai_api_key(args.openclaw_config)
    cmd = [
        args.imagegen_python,
        str(args.imagegen_cli),
        "edit",
        "--model",
        args.model,
        "--image",
        str(args.image),
        "--mask",
        str(mask_path),
        "--prompt-file",
        str(prompt_path),
        "--size",
        args.size,
        "--quality",
        args.quality,
        "--background",
        "opaque",
        "--output-format",
        "png",
        "--no-augment",
        "--out",
        str(raw_gpt_path),
    ]
    if args.force:
        cmd.append("--force")
    subprocess.run(cmd, check=True, env=env)


def composite_original_people(args: argparse.Namespace, raw_gpt_path: Path) -> None:
    original = Image.open(args.image).convert("RGB")
    width, height = original.size
    gpt = Image.open(raw_gpt_path).convert("RGB").resize((width, height), Image.Resampling.LANCZOS)

    mask = Image.new("L", (width, height), 0)
    feather = max(0, args.human_feather_px)
    if feather == 0:
        feather = max(1, int(height * args.feather_ratio))
    human_mask = build_feathered_rect_mask((width, height), resolved_human_rects(args, width, height), feather)
    mask = ImageChops.lighter(mask, human_mask)
    draw = ImageDraw.Draw(mask)
    for rect in resolved_protect_rects(args, width, height):
        draw.rectangle(rect, fill=255)
    mask = ImageChops.lighter(mask, build_source_preserve_mask(original, args))

    final = Image.composite(original, gpt, mask)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    suffix = args.output.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        final.save(args.output, quality=95, subsampling=0, optimize=True)
    else:
        final.save(args.output)


def write_manifest(args: argparse.Namespace, manifest_path: Path, raw_gpt_path: Path, mask_path: Path, prompt_path: Path) -> None:
    manifest: dict[str, Any] = {
        "source": str(args.image),
        "output": str(args.output),
        "raw_gpt_output": str(raw_gpt_path),
        "mask": str(mask_path),
        "prompt": str(prompt_path),
        "model": args.model,
        "size": args.size,
        "quality": args.quality,
        "text": {
            "title": args.title,
            "year": args.year,
            "subtitle": args.subtitle,
        },
        "theme": args.theme,
        "accent_instructions": args.accent_instructions,
        "skip_gpt_text": args.skip_gpt_text,
        "ratios": {
            "header": args.header_ratio,
            "side": args.side_ratio,
            "bottom_start": args.bottom_start_ratio,
            "paste_start": args.paste_start_ratio,
            "paste_end": args.paste_end_ratio,
            "feather": args.feather_ratio,
        },
        "human_rects": args.human_rect,
        "human_feather_px": args.human_feather_px,
        "protect_rects": args.protect_rect,
        "restore_non_sky_rects": args.restore_non_sky_rect,
        "auto_preserve_source_structures": args.auto_preserve_source_structures,
        "auto_preserve_end_ratio": args.auto_preserve_end_ratio,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    args.image = args.image.expanduser()
    args.output = args.output.expanduser()
    if not args.image.exists():
        raise SystemExit(f"Source image not found: {args.image}")
    if args.output.exists() and not args.force:
        raise SystemExit(f"Output already exists, pass --force to overwrite: {args.output}")

    build_dir = args.build_dir or args.output.with_suffix("").parent / f"{args.output.stem}-build"
    build_dir = build_dir.expanduser()
    build_dir.mkdir(parents=True, exist_ok=True)

    mask_path = build_dir / "edit-mask.png"
    prompt_path = build_dir / "prompt.txt"
    raw_gpt_path = build_dir / "raw-gpt-decorated.png"
    manifest_path = build_dir / "manifest.json"

    write_mask(args.image, mask_path, args)
    write_prompt(prompt_path, args)

    if args.reuse_gpt:
        raw_source = args.reuse_gpt.expanduser()
        if not raw_source.exists():
            raise SystemExit(f"Raw GPT output not found: {raw_source}")
        if raw_source.resolve() != raw_gpt_path.resolve():
            shutil.copy2(raw_source, raw_gpt_path)
    else:
        run_gpt_edit(args, mask_path, prompt_path, raw_gpt_path)

    composite_original_people(args, raw_gpt_path)
    write_manifest(args, manifest_path, raw_gpt_path, mask_path, prompt_path)
    print(f"Wrote {args.output}")
    print(f"Build artifacts: {build_dir}")


if __name__ == "__main__":
    main()
