"""Create bounded, synthetic technical smoke artifacts for Stage 6A.

The generated media is explicitly technical-only and must not be treated as
artistic, listening, rights, or publication acceptance evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(cmd: list[str]):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")


def write_json(path: Path, value: object):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def probe(path: Path) -> dict:
    return json.loads(run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)]).stdout)


def loudness(path: Path) -> dict:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-filter_complex", "ebur128=peak=true:framelog=verbose", "-f", "null", "-"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = result.stderr
    integrated = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s+LUFS", text)
    peak = re.findall(r"Peak:\s*(-?\d+(?:\.\d+)?)\s+dBFS", text)
    return {"integrated_lufs": float(integrated[-1]) if integrated else "NOT AVAILABLE", "true_peak_dbtp": float(peak[-1]) if peak else "NOT AVAILABLE"}


def make_source(path: Path):
    colors = ["red", "blue", "green", "yellow", "purple", "cyan", "orange", "white"]
    args = ["ffmpeg", "-y", "-v", "error"]
    for color in colors:
        args += ["-f", "lavfi", "-i", f"color=c={color}:s=640x360:r=30:d=6"]
    args += ["-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=22050:d={len(colors) * 6}"]
    video_inputs = "".join(f"[{i}:v]" for i in range(len(colors)))
    args += ["-filter_complex", f"{video_inputs}concat=n={len(colors)}:v=1:a=0[v]", "-map", "[v]", "-map", f"{len(colors)}:a",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)]
    run(args)


def make_music(path: Path):
    sr = 22050
    t = np.arange(sr * 45, dtype=np.float32) / sr
    beat = 0.13 * np.sin(2 * np.pi * 110 * t) + 0.06 * np.sin(2 * np.pi * 220 * t)
    envelope = 0.55 + 0.45 * (0.5 + 0.5 * np.sin(2 * np.pi * 1.5 * t))
    sf.write(path, (beat * envelope).astype(np.float32), sr, subtype="PCM_16")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=ROOT / "data" / "stage6a-smoke")
    args = ap.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ["PEAKCUT_DATA"] = str(output)
    os.environ["DATABASE_URL"] = ""
    from studio import cinematic  # noqa: WPS433

    fixture = output / "fixtures"
    fixture.mkdir(parents=True, exist_ok=True)
    source = fixture / "synthetic-six-scenes.mp4"
    music = fixture / "synthetic-cinematic.wav"
    if not source.is_file():
        make_source(source)
    if not music.is_file():
        make_music(music)
    track = cinematic.import_music(music, {"title": "Synthetic Cinematic Technical Track", "artist": "PeakCut test fixture",
                                           "genres": "cinematic, dramatic", "moods": "dramatic", "license_type": "Synthetic technical test permission",
                                           "allowed_platforms": "local", "license_confirmed": True, "content_id_status": "unknown"})
    source_info = cinematic.validate_video(source)
    scenes = cinematic.analyze_scenes(source, output / "scene-analysis")
    cases = [
        ("dramatic-auto-30s", {"duration": 30, "atmosphere": "dramatic", "music_source": "auto", "effects_intensity": "light"}),
        ("emotional-library-15s", {"duration": 15, "atmosphere": "emotional", "music_source": "library", "music_track_id": track["id"]}),
        ("dialogue-ducking-30s", {"duration": 30, "atmosphere": "dramatic", "music_source": "library", "music_track_id": track["id"], "effects_intensity": "light",
                                   "keep_original_dialogue": True, "audio_ducking": True}),
    ]
    for name, raw_options in cases:
        case = output / name
        case.mkdir(parents=True, exist_ok=True)
        options = cinematic.normalize_options(raw_options)
        if options["music_source"] == "auto":
            chosen, case_reason, case_warnings = cinematic.choose_music(options, options["duration"])
        else:
            chosen = {"track_id": track["id"], "path": str(cinematic.music_path(track["id"])), "title": track["title"],
                      "license": track["license"], "analysis": cinematic.analyze_audio(cinematic.music_path(track["id"]), digest=track["sha256"])}
            case_reason, case_warnings = "Manually selected from the local music library.", []
        if not chosen:
            raise RuntimeError(f"No music was selected for {name}: {case_reason}")
        analysis = chosen["analysis"]
        plan = cinematic.make_plan(source, source_info, scenes, options, chosen, analysis, case_reason, list(case_warnings))
        if errors := cinematic.validate_plan(plan, source_info["duration"]):
            raise RuntimeError("; ".join(errors))
        result = case / "result.mp4"
        metrics = cinematic.render_plan(source, Path(chosen["path"]), plan, options, result)
        write_json(case / "edit-plan.json", plan)
        write_json(case / "music-analysis.json", {"track_id": chosen["track_id"], **analysis})
        write_json(case / "scene-analysis.json", {"source": source.name, "scenes": scenes})
        write_json(case / "license.json", {"track_id": chosen["track_id"], "license": chosen["license"], "attribution": chosen["license"].get("attribution", "")})
        media_probe = probe(result)
        run(["ffmpeg", "-v", "error", "-i", str(result), "-f", "null", "-"])
        write_json(case / "metrics.json", metrics)
        write_json(case / "smoke-report.json", {"artifact": str(result), "ffprobe": media_probe, "full_decode": "PASS", "loudness": loudness(result),
                                                   "beat_alignment": {"status": "renderer consumes plan sync points; hard-cut frame evidence is in tests/test_cinematic.py", "markers": [s.get("music_beat_reference") for s in plan["segments"]], "sync_definition": plan.get("crossfade_sync_definition")},
                                                   "render_time_seconds": metrics["render_seconds"], "cpu_ram_vram": metrics["cpu_ram_vram"],
                                                   "ai_usage": "NOT RUN: deterministic local smoke; paid OpenRouter smoke is recorded separately", "selection_reason": case_reason,
                                                   "acceptance": {"technical": "PASS", "artistic": "NOT RUN", "listening": "NOT RUN", "rights": "TECHNICAL-ONLY synthetic fixture", "publication": "BLOCKED"}})
    write_json(output / "README.json", {"scope": "Stage 6A technical smoke only", "source": str(source), "track": str(music),
                                         "artistic_listening_rights_publication": "NOT ACCEPTED", "generated_cases": [x[0] for x in cases]})
    print(output)


if __name__ == "__main__":
    main()
