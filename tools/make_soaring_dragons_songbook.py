#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import librosa
import numpy as np
from music21 import clef, instrument, key, metadata, midi, meter, note, stream, tempo as m21tempo
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
MP3 = ROOT / "input" / "soaring-dragons.mp3"
LYRICS = ROOT / "input" / "lyrics.txt"
COVER = ROOT / "input" / "cover.png"
OUT_DIR = ROOT / "output" / "songbook"
PDF_OUT = OUT_DIR / "soaring_dragons_songbook.pdf"
MUSICXML_OUT = OUT_DIR / "soaring_dragons_lead_sheet.musicxml"
MIDI_OUT = OUT_DIR / "soaring_dragons_melody.mid"
JSON_OUT = OUT_DIR / "soaring_dragons_transcription.json"

CJK_FONT = "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"
SERIF_FONT = "/usr/share/fonts/truetype/freefont/FreeSerif.ttf"
SERIF_BOLD = "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf"

KEY_TONIC = "G"
KEY_PITCH_CLASSES = {7, 9, 11, 0, 2, 4, 6}  # G major / E minor, keeps F#.
DEGREE_BY_PC = {7: "1", 9: "2", 11: "3", 0: "4", 2: "5", 4: "6", 6: "7"}
NOTE_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
DIATONIC_INDEX = {"C": 0, "D": 1, "E": 2, "F": 3, "G": 4, "A": 5, "B": 6}


def chinese_syllables(line: str) -> list[str]:
    return [ch for ch in line if ("\u4e00" <= ch <= "\u9fff") or ch.isalnum()]


def midi_to_name(midi_num: int) -> str:
    pc = midi_num % 12
    octv = midi_num // 12 - 1
    return f"{NOTE_NAMES_SHARP[pc]}{octv}"


def name_to_midi(name: str) -> int:
    m = re.match(r"^([A-G])(#?)(-?\d+)$", name)
    if not m:
        raise ValueError(name)
    letter, sharp, octave = m.groups()
    pc = NOTE_NAMES_SHARP.index(letter + sharp)
    return (int(octave) + 1) * 12 + pc


def snap_to_g_major(midi_num: int) -> int:
    if midi_num % 12 in KEY_PITCH_CLASSES:
        return midi_num
    candidates = []
    for delta in range(-2, 3):
        cand = midi_num + delta
        if cand % 12 in KEY_PITCH_CLASSES:
            candidates.append((abs(delta), delta > 0, cand))
    return min(candidates)[2] if candidates else midi_num


def octave_correct(raw_midi: int, prev_midi: int | None) -> int:
    candidates = []
    for shift in (-24, -12, 0, 12, 24):
        cand = raw_midi + shift
        if 55 <= cand <= 81:
            center_cost = abs(cand - 70) * 0.18
            jump_cost = 0 if prev_midi is None else abs(cand - prev_midi)
            candidates.append((jump_cost + center_cost, cand))
    return min(candidates)[1] if candidates else raw_midi


def estimate_transcription() -> dict:
    text = LYRICS.read_text(encoding="utf-8").strip().splitlines()
    title = text[0].strip()
    lyric_lines = [line.strip() for line in text[1:] if line.strip()]

    y, sr = librosa.load(str(MP3), sr=22050, mono=True)
    tempo_val, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
    bpm = float(np.ravel(tempo_val)[0])
    beat_dur = 60.0 / bpm

    y_harm, _ = librosa.effects.hpss(y)
    f0, voiced_flag, _ = librosa.pyin(
        y_harm,
        fmin=librosa.note_to_hz("C3"),
        fmax=librosa.note_to_hz("C6"),
        sr=sr,
        frame_length=2048,
        hop_length=256,
    )
    times = librosa.frames_to_time(np.arange(len(f0)), sr=sr, hop_length=256)

    # The vocal entry is the first long continuous voiced region after the intro.
    start_time = float(beats[np.argmin(np.abs(beats - 16.67))])

    prev_midi: int | None = None
    lines = []
    for line_index, lyric_line in enumerate(lyric_lines):
        syllables = chinese_syllables(lyric_line)
        durations = [2.0] * len(syllables)
        if sum(durations) < 16.0:
            durations[-1] += 16.0 - sum(durations)
        elif sum(durations) > 16.0:
            durations = [16.0 / len(syllables)] * len(syllables)

        cursor = start_time + line_index * 16.0 * beat_dur
        notes = []
        for syllable, dur in zip(syllables, durations):
            start = cursor
            end = cursor + dur * beat_dur
            cursor = end
            idx = (times >= start + 0.04) & (times <= end - 0.04) & np.isfinite(f0)
            if np.count_nonzero(idx) >= 3:
                raw_midi = int(round(float(np.nanmedian(librosa.hz_to_midi(f0[idx])))))
                midi_num = snap_to_g_major(octave_correct(raw_midi, prev_midi))
                prev_midi = midi_num
            elif prev_midi is not None:
                midi_num = prev_midi
            else:
                midi_num = 71
            notes.append(
                {
                    "syllable": syllable,
                    "duration_beats": dur,
                    "midi": midi_num,
                    "pitch": midi_to_name(midi_num),
                    "start_sec": round(start, 3),
                    "end_sec": round(end, 3),
                }
            )
        lines.append({"lyric": lyric_line, "notes": notes})

    return {
        "title": title,
        "source_audio": str(MP3.relative_to(ROOT)),
        "lyrics": str(LYRICS.relative_to(ROOT)),
        "estimated_bpm": round(bpm),
        "key": KEY_TONIC,
        "time_signature": "4/4",
        "vocal_start_sec": round(start_time, 3),
        "method": "Audio-derived lead sheet: pYIN pitch tracking, 16-beat lyric-line alignment, octave correction, G-major snapping.",
        "lines": lines,
    }


def build_musicxml_and_midi(data: dict) -> None:
    score = stream.Score()
    score.metadata = metadata.Metadata()
    score.metadata.title = data["title"]
    score.metadata.composer = "Audio transcription from input/soaring-dragons.mp3"
    part = stream.Part()
    part.insert(0, instrument.Vocalist())
    part.insert(0, clef.TrebleClef())
    part.insert(0, meter.TimeSignature("4/4"))
    part.insert(0, key.Key("G"))
    part.insert(0, m21tempo.MetronomeMark(number=data["estimated_bpm"]))

    for line in data["lines"]:
        for item in line["notes"]:
            n = note.Note(item["pitch"], quarterLength=item["duration_beats"])
            n.lyric = item["syllable"]
            part.append(n)
    score.insert(0, part)
    score.write("musicxml", fp=str(MUSICXML_OUT))
    mf = midi.translate.streamToMidiFile(score)
    mf.open(str(MIDI_OUT), "wb")
    mf.write()
    mf.close()


def register_fonts() -> None:
    pdfmetrics.registerFont(TTFont("CJK", CJK_FONT))
    pdfmetrics.registerFont(TTFont("FreeSerif", SERIF_FONT))
    pdfmetrics.registerFont(TTFont("FreeSerifBold", SERIF_BOLD))


def draw_centered(c: canvas.Canvas, text: str, x: float, y: float, font: str, size: float, color=colors.black) -> None:
    c.setFillColor(color)
    c.setFont(font, size)
    c.drawCentredString(x, y, text)


def fit_image(c: canvas.Canvas, image_path: Path, x: float, y: float, w: float, h: float) -> None:
    img = Image.open(image_path)
    iw, ih = img.size
    scale = min(w / iw, h / ih)
    dw, dh = iw * scale, ih * scale
    c.drawImage(ImageReader(img), x + (w - dw) / 2, y + (h - dh) / 2, dw, dh, preserveAspectRatio=True, mask="auto")


def pitch_step_from_e4(pitch_name: str) -> int:
    midi_num = name_to_midi(pitch_name)
    pc_name = NOTE_NAMES_SHARP[midi_num % 12].replace("#", "")
    octave = midi_num // 12 - 1
    return (octave - 4) * 7 + DIATONIC_INDEX[pc_name] - DIATONIC_INDEX["E"]


def draw_notehead(c: canvas.Canvas, x: float, y: float, dur: float, stem_up: bool) -> None:
    c.setStrokeColor(colors.black)
    c.setFillColor(colors.white)
    c.ellipse(x - 5.8, y - 3.8, x + 5.8, y + 3.8, fill=1, stroke=1)
    if dur < 4:
        if stem_up:
            c.line(x + 5.5, y, x + 5.5, y + 30)
        else:
            c.line(x - 5.5, y, x - 5.5, y - 30)


def draw_staff_system(c: canvas.Canvas, line: dict, idx: int, x0: float, y_top: float, width: float) -> None:
    gap = 7.0
    bottom = y_top - 4 * gap
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.7)
    for i in range(5):
        y = y_top - i * gap
        c.line(x0, y, x0 + width, y)

    c.setFont("FreeSerif", 30)
    c.drawString(x0 - 40, bottom - 3, "𝄞")
    if idx == 0:
        c.setFont("FreeSerif", 10)
        c.drawString(x0 - 16, y_top - 6, "4")
        c.drawString(x0 - 16, y_top - 20, "4")
    c.setFont("CJK", 9)
    c.drawString(x0 - 50, y_top + 10, f"{idx + 1}.")

    for beat in (0, 4, 8, 12, 16):
        x = x0 + width * beat / 16.0
        c.line(x, y_top, x, bottom)

    beat_cursor = 0.0
    for item in line["notes"]:
        dur = float(item["duration_beats"])
        x = x0 + width * (beat_cursor + dur / 2.0) / 16.0
        step = pitch_step_from_e4(item["pitch"])
        y = bottom + step * gap / 2.0

        if step < 0:
            for ledger_step in range(0, step - 1, -2):
                ly = bottom + ledger_step * gap / 2.0
                c.line(x - 10, ly, x + 10, ly)
        elif step > 8:
            for ledger_step in range(10, step + 1, 2):
                ly = bottom + ledger_step * gap / 2.0
                c.line(x - 10, ly, x + 10, ly)

        if item["pitch"].startswith("F#"):
            c.setFont("FreeSerif", 12)
            c.drawString(x - 15, y - 4, "#")
        draw_notehead(c, x, y, dur, stem_up=step < 4)
        c.setFillColor(colors.black)
        c.setFont("CJK", 12)
        c.drawCentredString(x, bottom - 24, item["syllable"])
        c.setFillColor(colors.black)
        beat_cursor += dur


def jianpu_degree(midi_num: int) -> tuple[str, int]:
    pc = midi_num % 12
    degree = DEGREE_BY_PC.get(pc)
    if degree is None:
        degree = DEGREE_BY_PC[snap_to_g_major(midi_num) % 12]
    # Middle numbered-notation octave for 1=G is G4-F#5.
    if midi_num < 67:
        octave = -1
    elif midi_num > 78:
        octave = 1
    else:
        octave = 0
    return degree, octave


def draw_jianpu_line(c: canvas.Canvas, line: dict, idx: int, x0: float, y: float, width: float) -> None:
    c.setFont("CJK", 11)
    c.drawString(x0 - 36, y + 24, f"{idx + 1}.")
    c.drawString(x0, y + 24, line["lyric"])
    c.setStrokeColor(colors.HexColor("#777777"))
    c.setLineWidth(0.45)
    for beat in range(17):
        x = x0 + width * beat / 16.0
        if beat % 4 == 0:
            c.line(x, y - 10, x, y + 17)
    c.line(x0, y - 10, x0 + width, y - 10)

    beat_cursor = 0.0
    for item in line["notes"]:
        dur = int(round(item["duration_beats"]))
        for offset in range(dur):
            x = x0 + width * (beat_cursor + offset + 0.5) / 16.0
            if offset == 0:
                degree, octave = jianpu_degree(int(item["midi"]))
                c.setFont("FreeSerifBold", 15)
                c.drawCentredString(x, y, degree)
                c.setFillColor(colors.black)
                if octave > 0:
                    c.circle(x, y + 17, 1.6, fill=1, stroke=0)
                elif octave < 0:
                    c.circle(x, y - 5, 1.6, fill=1, stroke=0)
                c.setFont("CJK", 10)
                c.drawCentredString(x, y - 27, item["syllable"])
            else:
                c.setFont("FreeSerif", 13)
                c.drawCentredString(x, y, "-")
        beat_cursor += item["duration_beats"]


def draw_pdf(data: dict) -> None:
    register_fonts()
    c = canvas.Canvas(str(PDF_OUT), pagesize=letter)
    page_w, page_h = letter

    # Cover page.
    draw_centered(c, data["title"], page_w / 2, page_h - 52, "CJK", 30, colors.HexColor("#9d1b16"))
    draw_centered(c, "五线谱加简谱学习版", page_w / 2, page_h - 87, "CJK", 18)
    fit_image(c, COVER, 126, 158, 360, 500)
    c.setFont("FreeSerif", 12)
    c.drawCentredString(page_w / 2 - 30, 125, f"1={KEY_TONIC}    4/4    q={data['estimated_bpm']}")
    draw_centered(c, "（自动估计）", page_w / 2 + 86, 125, "CJK", 10)
    draw_centered(c, "根据音频自动转写，适合学唱；建议按原曲校对细节。", page_w / 2, 94, "CJK", 9, colors.HexColor("#555555"))
    c.showPage()

    # Notes and lyrics page.
    c.setFont("CJK", 20)
    c.drawString(54, page_h - 70, "歌词")
    y = page_h - 112
    c.setFont("CJK", 12)
    for line in [data["title"], ""] + [line["lyric"] for line in data["lines"]]:
        c.drawString(72, y, line)
        y -= 24
    c.setFont("CJK", 10)
    c.setFillColor(colors.HexColor("#555555"))
    c.drawString(54, 92, "说明：这是自动生成的主旋律学习谱，不是人工精修总谱。")
    c.drawString(54, 74, "另附可编辑乐谱和旋律文件，方便后续校对。")
    c.setFillColor(colors.black)
    c.showPage()

    # Staff notation pages.
    c.setFont("CJK", 18)
    c.drawString(54, page_h - 54, "五线谱")
    c.setFont("FreeSerif", 10)
    c.drawRightString(page_w - 54, page_h - 52, f"1={KEY_TONIC}    4/4    q={data['estimated_bpm']}")
    x0, width = 92, page_w - 146
    systems_per_page = 4
    y_positions = [page_h - 105, page_h - 275, page_h - 445, page_h - 615]
    for idx, line in enumerate(data["lines"]):
        if idx > 0 and idx % systems_per_page == 0:
            c.showPage()
            c.setFont("CJK", 18)
            c.drawString(54, page_h - 54, "五线谱")
            c.setFont("FreeSerif", 10)
            c.drawRightString(page_w - 54, page_h - 52, f"1={KEY_TONIC}    4/4    q={data['estimated_bpm']}")
        draw_staff_system(c, line, idx, x0, y_positions[idx % systems_per_page], width)
    c.showPage()

    # Jianpu pages.
    c.setFont("CJK", 18)
    c.drawString(54, page_h - 54, "简谱")
    c.setFont("FreeSerif", 10)
    c.drawRightString(page_w - 176, page_h - 52, f"1={KEY_TONIC}    4/4    q={data['estimated_bpm']}")
    c.setFont("CJK", 10)
    c.drawRightString(page_w - 54, page_h - 52, "数字和横线各占一拍")
    jianpu_y_positions = [page_h - 120, page_h - 225, page_h - 330, page_h - 435, page_h - 540, page_h - 645]
    for idx, line in enumerate(data["lines"]):
        if idx > 0 and idx % 6 == 0:
            c.showPage()
            c.setFont("CJK", 18)
            c.drawString(54, page_h - 54, "简谱")
            c.setFont("FreeSerif", 10)
            c.drawRightString(page_w - 176, page_h - 52, f"1={KEY_TONIC}    4/4    q={data['estimated_bpm']}")
            c.setFont("CJK", 10)
            c.drawRightString(page_w - 54, page_h - 52, "数字和横线各占一拍")
        draw_jianpu_line(c, line, idx, 78, jianpu_y_positions[idx % 6], page_w - 132)
    c.save()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = estimate_transcription()
    JSON_OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    build_musicxml_and_midi(data)
    draw_pdf(data)
    print(PDF_OUT)
    print(MUSICXML_OUT)
    print(MIDI_OUT)
    print(JSON_OUT)


if __name__ == "__main__":
    main()
