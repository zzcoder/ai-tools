# Slideshow Generator

The reusable program is:

```bash
python3 tools/make_photo_slideshow.py
```

It sorts photos by EXIF timestamp when available, falls back to file modified time, creates an optional title card, concatenates music, writes a timestamp order CSV, and renders MP4 through ffmpeg. Use `--encoder h264_nvenc --gpu 1` to render with the NVIDIA GPU.

## Basic Use

```bash
python3 tools/make_photo_slideshow.py \
  --image-dir /path/to/photos \
  --audio-list /path/to/songs.txt \
  --title "Trip Slideshow" \
  --motion reveal \
  --transition-duration 0.75 \
  --transition-style dip-black \
  --fps 30 \
  --width 1920 \
  --height 1080 \
  --output /path/to/slideshow.mp4 \
  --build-dir /path/to/slideshow-build \
  --encoder h264_nvenc \
  --gpu 1
```

For a silent slideshow, omit audio and pass `--duration 60`.

## Smooth Preview

This is the smooth full-image mode. It keeps the whole photo visible and uses 30 fps panning without zoom resizing.

```bash
python3 tools/make_photo_slideshow.py \
  --image-dir /home/zhihongz/codex-workspace/tree-planting \
  --audio-list tools/earth_day_2026_audio.txt \
  --title "Earth Day Volunteering 2026" \
  --motion fit \
  --fps 30 \
  --width 1920 \
  --height 1080 \
  --output /home/zhihongz/codex-workspace/tree-planting/Earth_Day_Volunteering_2026_slideshow_smooth_fullimage_1080p.mp4 \
  --build-dir /home/zhihongz/codex-workspace/tree-planting-slideshow-build-fit-preview \
  --encoder h264_nvenc \
  --gpu 1
```

## Quick Test Render

Use this before a full render:

```bash
python3 tools/make_photo_slideshow.py \
  --image-dir /home/zhihongz/codex-workspace/tree-planting \
  --audio-list tools/earth_day_2026_audio.txt \
  --title "Earth Day Volunteering 2026" \
  --motion fit \
  --fps 30 \
  --limit 8 \
  --max-duration 30 \
  --output /home/zhihongz/codex-workspace/tree-planting-slideshow-build-fit-preview/test_smooth_fit_1080p_30fps.mp4 \
  --build-dir /home/zhihongz/codex-workspace/tree-planting-slideshow-build-fit-preview \
  --encoder h264_nvenc \
  --gpu 1
```

## 4K Render

```bash
python3 tools/make_photo_slideshow.py \
  --image-dir /home/zhihongz/codex-workspace/tree-planting \
  --audio-list tools/earth_day_2026_audio.txt \
  --title "Earth Day Volunteering 2026" \
  --motion fit \
  --fps 30 \
  --width 3840 \
  --height 2160 \
  --output /home/zhihongz/codex-workspace/tree-planting/Earth_Day_Volunteering_2026_slideshow_smooth_fullimage_4K.mp4 \
  --build-dir /home/zhihongz/codex-workspace/tree-planting-slideshow-build-fit-4k \
  --encoder h264_nvenc \
  --gpu 1
```

## Notes

- `--motion fit` keeps the whole image visible with a blurred full-screen background and smooth pan motion.
- `--motion kenburns` fills the frame but crops parts of photos.
- `--motion static` keeps each photo still.
- Music can be passed with repeated `--audio /path/to/song.mp3` arguments, or with `--audio-list`.
- The build directory stores `timestamp-order.csv`, `render-summary.json`, the title card, and the combined audio cache.
