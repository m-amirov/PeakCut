"""Focused Stage 6B contract checks without an AI call or speech-model download."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="peakcut-cinematic-6b-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from studio import cinematic  # noqa: E402


fails = []


def check(label, condition):
    if condition:
        print("PASS", label)
    else:
        print("FAIL", label)
        fails.append(label)


opts = cinematic.normalize_options({"edit_mode": "character_edit", "music_source": "library", "music_track_id": "trk-demo",
                                    "caption_mode": "all_dialogue", "caption_style": "dynamic", "caption_language": "ru",
                                    "audio_mode": "music_key_dialogue", "transition_style": "auto"})
check("Stage 6B exposes the five edit modes and explicit caption/audio contracts",
      opts["edit_mode"] == "character_edit" and opts["caption_language"] == "ru" and opts["audio_mode"] == "music_key_dialogue")
check("Character, Action and Phonk default to hard cuts",
      cinematic._transition_duration({**opts, "edit_mode": "character_edit"}, "build_up", 2, 4.0) == 0 and
      cinematic._transition_duration({**opts, "edit_mode": "action_edit"}, "climax", 2, 1.2) == 0 and
      cinematic._transition_duration({**opts, "edit_mode": "phonk_aura"}, "hook", 2, 1.2) == 0)
check("Emotional pacing permits selective calm crossfades",
      cinematic._transition_duration({**opts, "edit_mode": "emotional_edit"}, "resolution", 2, 4.0) > 0)

plan = {"planner_version": cinematic.PLANNER_VERSION, "source_duration": 30, "total_duration": 10,
        "segments": [
            {"source_id": "a", "source_in": 0, "source_out": 4, "timeline_in": 0, "timeline_out": 4,
             "playback_speed": 1.0, "transition": {"duration": 0}},
            {"source_id": "b", "source_in": 10, "source_out": 14, "timeline_in": 4, "timeline_out": 8,
             "playback_speed": 1.0, "transition": {"duration": 0.25}},
            {"source_id": "c", "source_in": 20, "source_out": 24, "timeline_in": 8, "timeline_out": 10,
             "playback_speed": 2.0, "transition": {"duration": 0}},
        ]}
mapped = cinematic.remap_caption_words([
    {"w": "Привет", "s": 0.5, "e": 1.0}, {"w": "мир", "s": 10.5, "e": 11.0},
    {"w": "скорость", "s": 22.0, "e": 23.0}], plan)
check("Caption timestamps remap source intervals, speed and transition overlap",
      [(x["w"], x["s"], x["e"]) for x in mapped] == [("Привет", 0.5, 1.0), ("мир", 4.5, 5.0), ("скорость", 9.0, 9.5)])
check("Music-only cannot display dialogue captions", not cinematic._caption_words_for_mode(mapped, plan, {"caption_mode": "all_dialogue", "audio_mode": "music_only"}))

events = [{"id": f"event-{i}", "start": float(i * 10), "end": float(i * 10 + 4), "duration": 4.0,
           "shot_ids": [f"shot-{i}"], "face_shot_count": 1 if i in (0, 4) else 0,
           "dominant_visual_group": "visual-group-01" if i in (0, 4) else f"visual-group-{i + 2:02d}",
           "local_discovery_score": 0.2} for i in range(5)]
notes = {"status": "available", "concept": "same character across the source", "recommendations": [
    {"event_id": "event-0", "score": 95}, {"event_id": "event-4", "score": 94}]}
character_arc, _, status = cinematic._select_event_arc(events, notes, 8, {"edit_mode": "character_edit"})
check("Character Edit may select related non-contiguous source events", status == "ai_character" and
      {x["id"] for x in character_arc} >= {"event-0", "event-4"})

# A small real-media caption fixture proves the existing ASS/libass path, the
# actual PeakCut word timestamp shape, Cyrillic glyphs, and a burned-in MP4.
media_dir = Path(tempfile.mkdtemp(prefix="peakcut-cinematic-caption-6b-"))
caption_source = media_dir / "caption-source.mp4"
subprocess.run([
    "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050", "-t", "2", "-c:v", "libx264",
    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(caption_source)], check=True)
caption_plan = {"planner_version": cinematic.PLANNER_VERSION, "source_duration": 2, "total_duration": 2,
                "segments": [{"source_id": "caption-shot", "source_in": 0, "source_out": 2,
                              "approved_source_start": 0, "approved_source_end": 2,
                              "timeline_in": 0, "timeline_out": 2, "playback_speed": 1.0,
                              "transition": {"duration": 0}, "crop": {"mode": "letterbox_safe"}}]}
caption_options = cinematic.normalize_options({"duration": 15, "music_source": "original_audio",
                                                "audio_mode": "original_audio", "caption_mode": "all_dialogue",
                                                "caption_style": "cinematic", "caption_language": "ru",
                                                "caption_size": 88})
caption_job = {"dir": str(media_dir)}
caption_result = cinematic.prepare_captions(caption_job, caption_source, caption_plan, caption_options,
                                            existing_words=[{"w": "Привет", "s": 0.45, "e": 1.20},
                                                            {"w": "мир", "s": 1.25, "e": 1.65}])
caption_ass = media_dir / "captions.ass"
caption_srt = media_dir / "captions.srt"
check("caption preparation consumes PeakCut w/s/e timestamps", caption_result["status"] == "ready" and
      caption_result["word_count"] == 2 and caption_ass.is_file() and caption_srt.is_file())
check("Russian ASS uses bundled glyph-capable font and SRT is written", "Noto Sans" in caption_ass.read_text(encoding="utf-8") and
      "Привет" in caption_srt.read_text(encoding="utf-8"))
caption_output = media_dir / "captioned.mp4"
caption_metrics = cinematic.render_plan(caption_source, None, caption_plan, caption_options, caption_output,
                                        captions_ass=caption_ass)
caption_probe = json.loads(cinematic.media.run(["ffprobe", "-v", "error", "-print_format", "json",
                                                "-show_streams", "-show_format", str(caption_output)]))
subprocess.run(["ffmpeg", "-v", "error", "-i", str(caption_output), "-f", "null", "-"], check=True)
caption_video = next(stream for stream in caption_probe["streams"] if stream["codec_type"] == "video")
check("captioned vertical MP4 has audio and exact short-plan duration", caption_output.is_file() and
      caption_metrics["audio"] and caption_video["width"] == 1080 and caption_video["height"] == 1920 and
      abs(float(caption_probe["format"].get("duration") or 0) - 2.0) <= 0.15)
check("captioned MP4 full decode succeeds", True)

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
