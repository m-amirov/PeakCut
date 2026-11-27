"""Stage 6A local engine checks: real media bytes, cache, licence gate, plan and decode."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(tempfile.mkdtemp(prefix="peakcut-cinematic-test-"))
os.environ["PEAKCUT_DATA"] = str(DATA)
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from studio import cinematic  # noqa: E402


fails = []


def check(label, condition, detail=""):
    if condition:
        print("PASS", label, detail)
    else:
        print("FAIL", label, detail)
        fails.append(label)


work = DATA / "fixtures"
work.mkdir()
source = work / "source.mp4"
music = work / "music.wav"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=5",
                "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=5", "-f", "lavfi", "-i", "color=c=green:s=320x180:r=30:d=5",
                "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=22050:d=15",
                "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)], check=True)
sr = 22050
t = np.arange(sr * 20, dtype=np.float32) / sr
audio = 0.12 * np.sin(2 * np.pi * 110 * t) + 0.08 * np.sin(2 * np.pi * 220 * t)
sf.write(music, audio, sr, subtype="PCM_16")


opts = cinematic.normalize_options({"duration": 15, "atmosphere": "dramatic", "effects_intensity": "light"})
check("options normalize to a supported cinematic contract", opts["duration"] == 15 and opts["music_source"] == "auto")
try:
    cinematic.normalize_options({"duration": 12})
except ValueError:
    check("unsupported duration is rejected", True)
else:
    check("unsupported duration is rejected", False)

track = cinematic.import_music(music, {"title": "Unconfirmed Test Track", "genres": "cinematic", "moods": "dramatic"})
check("music import validates and persists metadata", track["id"].startswith("trk-") and track["license"]["status"] == "unknown")
duplicate = cinematic.import_music(music, {"title": "Should Not Duplicate"})
check("duplicate music is detected by SHA-256", duplicate["id"] == track["id"])
selected, reason, warnings = cinematic.choose_music(opts, 15)
check("unknown licence does not enter Auto selection", selected is None and "confirmed" in reason)

updated = cinematic.update_music(track["id"], {"genres": ["cinematic", "orchestral"], "moods": ["dramatic"], "license": {
    "license_confirmed": True, "license_type": "User-confirmed test permission", "allowed_platforms": ["local"]}})
check("explicit licence confirmation is retained", updated["license"]["status"] == "confirmed" and updated["license"]["confirmed_by_user"])
selected, reason, warnings = cinematic.choose_music(opts, 15)
check("Auto selection uses one eligible local track", selected and selected["track_id"] == track["id"] and "Auto-selected" in reason)
offset_opts = cinematic.normalize_options({"duration": 15, "music_source": "library", "music_track_id": track["id"], "music_start_offset": 999})
offset_track, _, offset_warnings = cinematic.choose_music(offset_opts, 15)
check("music offset beyond track duration fails safe", offset_track and offset_opts["music_start_offset"] == 0 and offset_warnings)
filtered = cinematic.list_music({"q": "test track", "min_bpm": 1, "max_bpm": 300, "min_duration": 19, "max_duration": 21})
check("library filters cover query and BPM/duration bounds", filtered and filtered[0]["id"] == track["id"])
analysis1 = cinematic.analyze_audio(music)
analysis2 = cinematic.analyze_audio(music)
cache = cinematic.MUSIC_ANALYSIS / f"{analysis1['sha256']}-{cinematic.ANALYZER_VERSION}.json"
check("music analysis has rhythm fields and cache", analysis1["duration"] >= 19 and "beat_timestamps" in analysis1 and analysis1 == analysis2 and cache.is_file())

scene_dir = work / "scene-out"
scenes = cinematic.analyze_scenes(source, scene_dir)
check("scene detection streams frames and excludes no valid colour scene", len(scenes) >= 2 and all("motion_intensity" in s for s in scenes))
plan = cinematic.make_plan(source, cinematic.validate_video(source), scenes, opts, selected, analysis1, reason, warnings)
check("edit plan contains bounded segments and beat references", not cinematic.validate_plan(plan, 15.0) and plan["segments"] and "source_in" in plan["segments"][0])

# Stage 6A.1 regression: the beat marker must become the actual hard-cut
# boundary, including the music offset, rather than remaining JSON metadata.
known_beats = dict(analysis1, rhythm_confidence=1.0, beat_timestamps=[1.0, 6.0, 11.0, 16.0], strong_accents=[6.0, 11.0])
beat_opts = cinematic.normalize_options({"duration": 15, "music_source": "library", "music_track_id": track["id"],
                                         "music_start_offset": 1.0, "beat_sync": True, "smooth_transitions": False,
                                         "effects_intensity": "light"})
beat_plan = cinematic.make_plan(source, cinematic.validate_video(source), scenes, beat_opts, selected, known_beats, reason, [])
beat_cuts = [s for s in beat_plan["segments"] if s["music_beat_reference"] is not None]
check("beat sync offset controls hard-cut timeline boundaries", not cinematic.validate_plan(beat_plan, 15.0) and
      [round(s["timeline_in"], 3) for s in beat_cuts] == [5.0, 10.0] and
      [round(s["music_beat_reference"], 3) for s in beat_cuts] == [6.0, 11.0] and
      all(abs(float(s["sync_error_frames"])) <= 1 for s in beat_cuts))
off_opts = cinematic.normalize_options({**beat_opts, "beat_sync": False})
off_plan = cinematic.make_plan(source, cinematic.validate_video(source), scenes, off_opts, selected, known_beats, reason, [])
check("Beat Sync Off removes music-driven boundaries", off_plan["sync_mode"] == "off" and
      all(s["music_beat_reference"] is None for s in off_plan["segments"]))
cross_opts = cinematic.normalize_options({**beat_opts, "smooth_transitions": True})
cross_plan = cinematic.make_plan(source, cinematic.validate_video(source), scenes, cross_opts, selected, known_beats, reason, [])
check("crossfade sync moment is explicitly documented", (cross_plan["transition_overlap"] == 0 and
      cross_plan["crossfade_sync_definition"] == "not applicable") or
      cross_plan["crossfade_sync_definition"].startswith("beat-aligned at crossfade start"))

# Stage 6A.1 regression: recommendations must reach the planner and affect
# both membership/order, while malformed recommendations stay local-only.
ai_a = {"status": "available", "recommendations": [
    {"source_id": "scene-0003", "score": 100, "reason": "ending"},
    {"source_id": "scene-0001", "score": 70, "reason": "opening"},
    {"source_id": "scene-0002", "score": 60, "reason": "middle"}]}
ai_b = {"status": "available", "recommendations": [
    {"source_id": "scene-0001", "score": 100, "reason": "opening"},
    {"source_id": "scene-0002", "score": 70, "reason": "middle"},
    {"source_id": "scene-0003", "score": 60, "reason": "ending"}]}
ai_plan_a = cinematic.make_plan(source, cinematic.validate_video(source), scenes, off_opts, selected, known_beats, reason, [], ai_notes=ai_a)
ai_plan_b = cinematic.make_plan(source, cinematic.validate_video(source), scenes, off_opts, selected, known_beats, reason, [], ai_notes=ai_b)
check("different valid AI recommendations change plan sequence", ai_plan_a["ai_recommendations_applied"] and
      [s["source_id"] for s in ai_plan_a["segments"]] != [s["source_id"] for s in ai_plan_b["segments"]])
bad_ai = {"status": "available", "recommendations": [{"source_id": "not-a-scene", "score": 1000}]}
bad_plan = cinematic.make_plan(source, cinematic.validate_video(source), scenes, off_opts, selected, known_beats, reason, [], ai_notes=bad_ai)
check("invalid AI recommendations fall back locally", not bad_plan["ai_recommendations_applied"] and any("invalid" in w for w in bad_plan["warnings"]))

out = work / "result.mp4"
metrics = cinematic.render_plan(source, Path(selected["path"]), plan, opts, out)
check("real vertical MP4 is rendered", out.is_file() and out.stat().st_size > 10000 and metrics["width"] == 1080 and metrics["height"] == 1920)
probe = json.loads(cinematic.media.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(out)]))
video = next(s for s in probe["streams"] if s["codec_type"] == "video")
check("FFprobe confirms H264/AAC-compatible output geometry", video["width"] == 1080 and video["height"] == 1920 and video["codec_name"] == "h264")
subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"], check=True)
check("full output decode succeeds", True)
result_duration = float(next(s for s in probe["streams"] if s["codec_type"] == "video").get("duration") or probe["format"].get("duration") or 0)
check("rendered duration stays within two frames", abs(result_duration - 15.0) <= 2 / 30 + 0.04)
beat_out = work / "beat-sync.mp4"
cinematic.render_plan(source, Path(selected["path"]), beat_plan, beat_opts, beat_out)
raw_frames = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", "4", "-t", "2.1", "-i", str(beat_out),
                                      "-vf", "fps=30,scale=1:1,format=rgb24", "-f", "rawvideo", "pipe:1"])
frame_pixels = np.frombuffer(raw_frames, dtype=np.uint8).reshape(-1, 3)
check("beat-sync hard cut is visible in the rendered MP4", len(frame_pixels) >= 63 and
      not np.array_equal(frame_pixels[29], frame_pixels[30]))

mix_opts = cinematic.normalize_options({"duration": 15, "music_source": "library", "music_track_id": track["id"],
                                        "keep_original_dialogue": True, "audio_ducking": True, "effects_intensity": "light"})
mix_plan = cinematic.make_plan(source, cinematic.validate_video(source), scenes, mix_opts, selected, analysis1, reason, [])
mix_out = work / "dialogue-ducked.mp4"
mix_metrics = cinematic.render_plan(source, Path(selected["path"]), mix_plan, mix_opts, mix_out)
check("music plus original dialogue renders with ducking", mix_out.is_file() and mix_metrics["audio"])
subprocess.run(["ffmpeg", "-v", "error", "-i", str(mix_out), "-f", "null", "-"], check=True)
check("dialogue mix full decode succeeds", True)
repeat_out = work / "repeat.mp4"
cinematic.render_plan(source, Path(selected["path"]), mix_plan, mix_opts, repeat_out)
check("saved plan can be rendered again without re-analysis", repeat_out.is_file() and repeat_out.stat().st_size > 10000)

short_music = work / "short-track.wav"
sf.write(short_music, np.zeros(sr, dtype=np.float32), sr, subtype="PCM_16")
short_out = work / "short-track-padded.mp4"
cinematic.render_plan(source, short_music, off_plan, off_opts, short_out)
short_probe = json.loads(cinematic.media.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(short_out)]))
short_duration = float(short_probe["format"].get("duration") or 0)
short_audio = next((s for s in short_probe["streams"] if s["codec_type"] == "audio"), {})
check("short music is padded with silence rather than unexpectedly looped", abs(short_duration - 15.0) <= 2 / 30 + 0.04 and
      abs(float(short_audio.get("duration") or 0) - short_duration) <= 0.12)

try:
    cinematic.render_plan(source, short_music, off_plan, off_opts, work / "cancelled.mp4", cancel=lambda: True)
except cinematic.Cancelled:
    check("cinematic FFmpeg render cancellation stops the job", True)
else:
    check("cinematic FFmpeg render cancellation stops the job", False)

no_audio = work / "no-audio.mp4"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(no_audio)], check=True)
check("source without audio is accepted for Original Audio fallback", cinematic.validate_video(no_audio)["has_audio"] is False)

fallback_source = work / "fallback-source.mp4"
fallback_source.write_bytes(source.read_bytes())
saved_library = cinematic._library()
cinematic._save_library([])
fallback_opts = cinematic.normalize_options({"duration": 15, "atmosphere": "dramatic", "music_source": "auto"})
fallback_job = cinematic.create_job("u_local", fallback_source, fallback_source.name, fallback_opts)
cinematic._run_job(cinematic.get_job("u_local", fallback_job["id"]))
fallback_record = cinematic.get_job("u_local", fallback_job["id"])
fallback_result = Path(fallback_record["dir"]) / "result.mp4"
fallback_probe = json.loads(cinematic.media.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(fallback_result)]))
cinematic._save_library(saved_library)
check("Auto fallback really preserves original audio", fallback_record["status"] == "done" and any(s["codec_type"] == "audio" for s in fallback_probe["streams"]))

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
