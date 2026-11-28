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
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=5.5",
                "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=5.5", "-f", "lavfi", "-i", "color=c=green:s=320x180:r=30:d=5.5",
                "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=22050:d=16.5",
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
check("edit plan contains bounded segments and beat references", not cinematic.validate_plan(plan, cinematic.validate_video(source)["duration"]) and plan["segments"] and "source_in" in plan["segments"][0])

# Stage 6A.1 regression: the beat marker must become the actual hard-cut
# boundary, including the music offset, rather than remaining JSON metadata.
known_beats = dict(analysis1, rhythm_confidence=1.0, beat_timestamps=[1.0, 6.0, 11.0, 16.0], strong_accents=[6.0, 11.0])
beat_opts = cinematic.normalize_options({"duration": 15, "music_source": "library", "music_track_id": track["id"],
                                         "music_start_offset": 1.0, "beat_sync": True, "smooth_transitions": False,
                                         "effects_intensity": "light"})
beat_plan = cinematic.make_plan(source, cinematic.validate_video(source), scenes, beat_opts, selected, known_beats, reason, [])
beat_cuts = [s for s in beat_plan["segments"] if s["music_beat_reference"] is not None]
check("beat sync offset controls hard-cut timeline boundaries", not cinematic.validate_plan(beat_plan, cinematic.validate_video(source)["duration"]) and
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

# Stage 6A.2 regression: semantic planning keeps one chronological event arc,
# applies different AI anchors to the final plan, and never leaves approved
# shot boundaries.  These bounded fixtures exercise the planner without an
# additional full-film AI call.
semantic_scenes = []
semantic_events = []
for index in range(5):
    start = index * 5.0
    sid = f"semantic-shot-{index + 1:02d}"
    semantic_scenes.append({"id": sid, "start": start, "end": start + 5.0, "duration": 5.0, "excluded": False,
                            "motion_intensity": 0.2, "sharpness": 0.5, "brightness": 0.5,
                            "reframe_center_x": 0.5, "reframe_center_y": 0.5, "focus_mode": "saliency_or_center"})
    semantic_events.append({"id": f"semantic-event-{index + 1:02d}", "start": start, "end": start + 5.0,
                            "duration": 5.0, "shot_ids": [sid], "dialogue": "", "focus_mode": "saliency_or_center",
                            "reframe_center_x": 0.5, "reframe_center_y": 0.5, "local_discovery_score": 0.5})
semantic_info = {**cinematic.validate_video(source), "duration": 25.0}
semantic_a = cinematic.make_plan(source, semantic_info, semantic_scenes, off_opts, selected, known_beats,
                                 reason, [], ai_notes={"status": "available", "concept": "opening arc", "recommendations": [
                                     {"event_id": "semantic-event-01", "score": 100, "phase": "hook"}]}, events=semantic_events)
semantic_b = cinematic.make_plan(source, semantic_info, semantic_scenes, off_opts, selected, known_beats,
                                 reason, [], ai_notes={"status": "available", "concept": "ending arc", "recommendations": [
                                     {"event_id": "semantic-event-05", "score": 100, "phase": "climax"}]}, events=semantic_events)
check("semantic AI anchors change the final event arc", semantic_a["ai_recommendations_applied"] and
      semantic_a["selected_event_ids"] != semantic_b["selected_event_ids"])
check("semantic plan keeps every source segment inside approved shot bounds",
      not cinematic.validate_plan(semantic_a, 25.0) and all(
          float(seg["source_in"]) >= float(seg["approved_source_start"]) - 0.02 and
          float(seg["source_out"]) <= float(seg["approved_source_end"]) + 0.02 for seg in semantic_a["segments"]))
bad_semantic = json.loads(json.dumps(semantic_a))
bad_semantic["segments"][0]["source_out"] = bad_semantic["segments"][0]["approved_source_end"] + 0.5
check("approved source boundary violations are rejected", any("approved shot/event boundary" in e for e in cinematic.validate_plan(bad_semantic, 25.0)))
semantic_beats = cinematic.make_plan(source, semantic_info, semantic_scenes, beat_opts, selected, known_beats,
                                     reason, [], events=semantic_events)
semantic_sync = [s for s in semantic_beats["segments"] if s["music_beat_reference"] is not None]
check("semantic beat markers become exact timeline boundaries", semantic_sync and
      [round(s["timeline_in"], 3) for s in semantic_sync] == [5.0, 10.0] and
      all(abs(float(s["sync_error_frames"])) <= 1 for s in semantic_sync))

out = work / "result.mp4"
metrics = cinematic.render_plan(source, Path(selected["path"]), plan, opts, out)
check("real vertical MP4 is rendered", out.is_file() and out.stat().st_size > 10000 and metrics["width"] == 1080 and metrics["height"] == 1920)
check("Music Only confirms the selected track in the rendered MP4", metrics["music_verification"]["status"] == "present" and
      metrics["music_verification"]["selected_sha256"] == cinematic._sha256(Path(selected["path"])))
probe = json.loads(cinematic.media.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(out)]))
video = next(s for s in probe["streams"] if s["codec_type"] == "video")
check("FFprobe confirms H264/AAC-compatible output geometry", video["width"] == 1080 and video["height"] == 1920 and video["codec_name"] == "h264")
subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"], check=True)
check("full output decode succeeds", True)
result_duration = float(next(s for s in probe["streams"] if s["codec_type"] == "video").get("duration") or probe["format"].get("duration") or 0)
check("rendered duration matches the validated plan", abs(result_duration - float(plan["total_duration"])) <= 2 / 30 + 0.04)
if float(plan["total_duration"]) < 15.0:
    check("insufficient approved source material is reported", any("shorter" in w.lower() or "enough" in w.lower() for w in plan["warnings"]))
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
# This fixture contains synthetic tones, not speech. Supply explicit synthetic
# word stamps to exercise the retained-dialogue contract without any ASR model.
# The legacy planner does not annotate music phases, so mark the fixture's
# first segment as a key phase. Production jobs obtain phases from the planner.
try:
    cinematic.render_plan(source, Path(selected["path"]), mix_plan, mix_opts, mix_out)
except ValueError as exc:
    check("dialogue mix without retained speech is rejected", "No retained dialogue" in str(exc))
else:
    check("dialogue mix without retained speech is rejected", False)
first = mix_plan["segments"][0]
first["music_phase"] = "hook"
fixture_words = [{"w": "fixture", "s": float(first["source_in"]) + 0.35,
                  "e": float(first["source_in"]) + 0.85}]
mix_job_dir = work / "dialogue-mix-fixture"
mix_job_dir.mkdir()
mix_caption = cinematic.prepare_captions({"dir": str(mix_job_dir)}, source, mix_plan, mix_opts,
                                        existing_words=fixture_words)
check("dialogue fixture has retained speech timestamps", mix_caption["retained_dialogue_words"] > 0)
mix_metrics = cinematic.render_plan(source, Path(selected["path"]), mix_plan, mix_opts, mix_out)
check("dialogue mix has full-length audio", abs(mix_metrics["audio_stream_duration"] - mix_plan["total_duration"]) <= cinematic.DURATION_TOLERANCE)
check("music plus original dialogue renders with ducking", mix_out.is_file() and mix_metrics["audio"])
subprocess.run(["ffmpeg", "-v", "error", "-i", str(mix_out), "-f", "null", "-"], check=True)
check("dialogue mix full decode succeeds", True)
repeat_out = work / "repeat.mp4"
cinematic.render_plan(source, Path(selected["path"]), mix_plan, mix_opts, repeat_out)
check("saved plan can be rendered again without re-analysis", repeat_out.is_file() and repeat_out.stat().st_size > 10000)

short_music = work / "short-track.wav"
short_t = np.arange(sr, dtype=np.float32) / sr
sf.write(short_music, 0.12 * np.sin(2 * np.pi * 330 * short_t), sr, subtype="PCM_16")
short_opts = cinematic.normalize_options({"duration": 15, "music_source": "library", "music_track_id": track["id"],
                                          "music_start_offset": 0, "beat_sync": False, "effects_intensity": "light"})
short_plan = cinematic.make_plan(source, cinematic.validate_video(source), scenes, short_opts, selected, known_beats, reason, [])
try:
    cinematic.render_plan(source, short_music, short_plan, short_opts, work / "should-not-render-short-music.mp4")
except ValueError as exc:
    check("short music is rejected before rendering when the selected span is unavailable", "music_segment_exceeds_available_duration" in str(exc))
else:
    check("short music is rejected before rendering when the selected span is unavailable", False)

try:
    cinematic.render_plan(source, Path(selected["path"]), off_plan, off_opts, work / "cancelled.mp4", cancel=lambda: True)
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
cinematic._save_library(saved_library)
check("Auto without an eligible track stops at Music required", fallback_record["status"] == "music_required" and
      not (Path(fallback_record["dir"]) / "result.mp4").exists() and not fallback_record.get("selected_music"))

explicit_opts = cinematic.normalize_options({"duration": 15, "atmosphere": "dramatic", "music_source": "original_audio"})
explicit_source = work / "explicit-source.mp4"
explicit_source.write_bytes(source.read_bytes())
explicit_job = cinematic.create_job("u_local", explicit_source, explicit_source.name, explicit_opts)
cinematic._run_job(cinematic.get_job("u_local", explicit_job["id"]))
explicit_record = cinematic.get_job("u_local", explicit_job["id"])
explicit_result = Path(explicit_record["dir"]) / "result.mp4"
explicit_probe = json.loads(cinematic.media.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(explicit_result)]))
check("Original Audio renders only after explicit selection", explicit_record["status"] == "done" and
      any(s["codec_type"] == "audio" for s in explicit_probe["streams"]))

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
