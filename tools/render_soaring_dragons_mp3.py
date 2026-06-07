#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf


ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPTION = ROOT / "output" / "songbook" / "soaring_dragons_transcription.json"
OUT_DIR = ROOT / "output" / "songbook"
WAV_OUT = OUT_DIR / "soaring_dragons_sheet_music_render.wav"
MP3_OUT = OUT_DIR / "soaring_dragons_sheet_music_render.mp3"
WAV_CLEAN_OUT = OUT_DIR / "soaring_dragons_sheet_music_clean.wav"
MP3_CLEAN_OUT = OUT_DIR / "soaring_dragons_sheet_music_clean.mp3"


SAMPLE_RATE = 44100


def midi_to_freq(midi: int) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def add_tone(audio: np.ndarray, start: float, duration: float, midi: int, velocity: float = 0.42) -> None:
    """Add a clear, piano-like tone with fast attack and natural decay."""
    start_i = max(0, int(start * SAMPLE_RATE))
    n = max(1, int(duration * SAMPLE_RATE))
    end_i = min(len(audio), start_i + n)
    if end_i <= start_i:
        return

    t = np.arange(end_i - start_i, dtype=np.float64) / SAMPLE_RATE
    freq = midi_to_freq(midi)
    attack = np.minimum(1.0, t / 0.018)
    decay = np.exp(-2.4 * t / max(duration, 0.15))
    release_len = min(len(t), int(0.08 * SAMPLE_RATE))
    release = np.ones_like(t)
    if release_len > 1:
        release[-release_len:] = np.linspace(1.0, 0.0, release_len)
    env = attack * decay * release

    tone = (
        1.0 * np.sin(2 * np.pi * freq * t)
        + 0.42 * np.sin(2 * np.pi * freq * 2 * t)
        + 0.18 * np.sin(2 * np.pi * freq * 3 * t)
        + 0.08 * np.sin(2 * np.pi * freq * 4 * t)
    )
    audio[start_i:end_i] += velocity * env * tone


def add_click(audio: np.ndarray, start: float, strong: bool = False) -> None:
    start_i = int(start * SAMPLE_RATE)
    n = int(0.035 * SAMPLE_RATE)
    end_i = min(len(audio), start_i + n)
    if end_i <= start_i:
        return
    t = np.arange(end_i - start_i, dtype=np.float64) / SAMPLE_RATE
    freq = 1320.0 if strong else 880.0
    env = np.exp(-90 * t)
    audio[start_i:end_i] += (0.28 if strong else 0.16) * env * np.sin(2 * np.pi * freq * t)


def render_one(wav_out: Path, mp3_out: Path, include_clicks: bool) -> None:
    data = json.loads(TRANSCRIPTION.read_text(encoding="utf-8"))
    bpm = float(data["estimated_bpm"])
    beat = 60.0 / bpm
    count_in_beats = 4 if include_clicks else 0
    tail = 2.0
    total_beats = sum(item["duration_beats"] for line in data["lines"] for item in line["notes"])
    total_seconds = (count_in_beats + total_beats) * beat + tail

    mono = np.zeros(int(total_seconds * SAMPLE_RATE), dtype=np.float64)

    if include_clicks:
        # Four-beat count-in, useful for learners.
        for b in range(count_in_beats):
            add_click(mono, b * beat, strong=(b == 0))

    cursor = count_in_beats * beat
    for line_index, line in enumerate(data["lines"]):
        if include_clicks:
            # A subtle downbeat click every lyric line.
            add_click(mono, cursor, strong=True)
        for item in line["notes"]:
            dur = float(item["duration_beats"]) * beat
            midi = int(item["midi"])
            add_tone(mono, cursor, max(0.08, dur * 0.92), midi, velocity=0.48)
            cursor += dur

    # Soft limiter and stereo spread.
    peak = float(np.max(np.abs(mono))) or 1.0
    mono = np.tanh(mono / peak * 1.8) * 0.72
    delay = int(0.012 * SAMPLE_RATE)
    left = mono
    right = np.concatenate([np.zeros(delay), mono[:-delay]]) * 0.92
    stereo = np.stack([left, right], axis=1).astype(np.float32)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sf.write(wav_out, stereo, SAMPLE_RATE)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(wav_out),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-metadata",
            f"title={data['title']} sheet music render",
            "-metadata",
            "artist=Codex local render",
            str(mp3_out),
        ],
        check=True,
    )
    print(mp3_out)
    print(wav_out)


def render() -> None:
    render_one(WAV_CLEAN_OUT, MP3_CLEAN_OUT, include_clicks=False)
    render_one(WAV_OUT, MP3_OUT, include_clicks=True)


if __name__ == "__main__":
    render()
