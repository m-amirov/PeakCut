"""Focused Stage 6B contract checks without an AI call or speech-model download."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

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

# Stage 6B.1: the mixer and captions must consume the same retained speech
# selection.  The fixture has intro, key and non-key source intervals with
# distinct word timestamps, so retaining the whole source audio would fail the
# RMS checks below.
dialogue_source = media_dir / "dialogue-source.mp4"
subprocess.run([
    "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050", "-t", "6", "-c:v", "libx264",
    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dialogue_source)], check=True)
dialogue_plan = {"planner_version": cinematic.PLANNER_VERSION, "source_duration": 6, "total_duration": 6,
                 "segments": [
                     {"source_id": "intro", "source_in": 0, "source_out": 2, "timeline_in": 0, "timeline_out": 2,
                      "playback_speed": 1.0, "music_phase": "hook", "transition": {"duration": 0}, "crop": {"mode": "letterbox_safe"}},
                     {"source_id": "key", "source_in": 2, "source_out": 4, "timeline_in": 2, "timeline_out": 4,
                      "playback_speed": 1.0, "music_phase": "climax", "transition": {"duration": 0}, "crop": {"mode": "letterbox_safe"}},
                     {"source_id": "non-key", "source_in": 4, "source_out": 6, "timeline_in": 4, "timeline_out": 6,
                      "playback_speed": 1.0, "music_phase": "build_up", "transition": {"duration": 0}, "crop": {"mode": "letterbox_safe"}},
                 ]}
dialogue_job = {"dir": str(media_dir / "dialogue")}
(media_dir / "dialogue").mkdir()
dialogue_words = [{"w": "Вступление", "s": 0.35, "e": 0.85}, {"w": "Кульминация", "s": 2.35, "e": 2.85},
                  {"w": "Случайный", "s": 4.35, "e": 4.85}]
intro_options = cinematic.normalize_options({"music_source": "library", "audio_mode": "dialogue_intro_music",
                                               "caption_mode": "all_dialogue", "caption_language": "ru"})
intro_caption = cinematic.prepare_captions(dialogue_job, dialogue_source, dialogue_plan, intro_options, existing_words=dialogue_words)
check("Dialogue Intro captions contain only retained intro speech", intro_caption["word_count"] == 1 and
      intro_caption["retained_dialogue_words"] == 1 and intro_caption["word_count"] == intro_caption["retained_dialogue_words"])
intro_output = media_dir / "dialogue-intro.mp4"
cinematic.render_plan(dialogue_source, None, dialogue_plan, intro_options, intro_output, captions_ass=media_dir / "dialogue" / "captions.ass")
intro_voice = float((cinematic._audio_samples(intro_output, 0.35, 0.5) ** 2).mean() ** 0.5)
intro_silence = float((cinematic._audio_samples(intro_output, 1.15, 0.5) ** 2).mean() ** 0.5)
check("Dialogue Intro mixer ducks all non-retained source audio", intro_voice > max(1e-5, intro_silence * 3.0))

key_options = cinematic.normalize_options({"music_source": "library", "audio_mode": "music_key_dialogue",
                                             "caption_mode": "all_dialogue", "caption_language": "ru"})
key_job = {"dir": str(media_dir / "key")}
(media_dir / "key").mkdir()
key_caption = cinematic.prepare_captions(key_job, dialogue_source, dialogue_plan, key_options, existing_words=dialogue_words)
check("Music plus key dialogue captions match the selected key speech", key_caption["word_count"] == 2 and
      key_caption["retained_dialogue_words"] == 2 and "Кульминация" in (media_dir / "key" / "captions.srt").read_text(encoding="utf-8") and
      "Случайный" not in (media_dir / "key" / "captions.srt").read_text(encoding="utf-8"))
key_output = media_dir / "dialogue-key.mp4"
cinematic.render_plan(dialogue_source, None, dialogue_plan, key_options, key_output, captions_ass=media_dir / "key" / "captions.ass")
key_voice = float((cinematic._audio_samples(key_output, 2.35, 0.5) ** 2).mean() ** 0.5)
key_silence = float((cinematic._audio_samples(key_output, 4.35, 0.5) ** 2).mean() ** 0.5)
check("Music plus key dialogue mixer retains only the selected key speech", key_voice > max(1e-5, key_silence * 3.0))

music_fixture = media_dir / "music-fixture.wav"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=22050", "-t", "6", str(music_fixture)], check=True)
music_options = cinematic.normalize_options({"music_source": "library", "audio_mode": "music_only", "caption_mode": "all_dialogue"})
music_job = {"dir": str(media_dir / "music")}
(media_dir / "music").mkdir()
music_caption = cinematic.prepare_captions(music_job, dialogue_source, dialogue_plan, music_options, existing_words=dialogue_words)
music_output = media_dir / "music-only.mp4"
music_metrics = cinematic.render_plan(dialogue_source, music_fixture, dialogue_plan, music_options, music_output,
                                      captions_ass=media_dir / "music" / "captions.ass")
check("Music Only has no dialogue captions and still renders the selected music", music_caption["word_count"] == 0 and
      music_metrics["music_verification"]["status"] == "present")

# All four presets are checked on the actual burned-in frame, not just on an
# ASS file.  The long Russian phrase also exercises the bounded two-line path.
long_words = [{"w": word, "s": 0.20 + i * 0.12, "e": 0.30 + i * 0.12}
              for i, word in enumerate("Это очень длинная русская реплика для проверки переноса по смыслу и безопасных зон".split())]
plain_output = media_dir / "plain.mp4"
plain_options = cinematic.normalize_options({"music_source": "original_audio", "audio_mode": "original_audio", "caption_mode": "off"})
cinematic.render_plan(caption_source, None, caption_plan, plain_options, plain_output)
for style in ("cinematic", "aura_phonk", "dynamic", "movie_original"):
    style_job = {"dir": str(media_dir / f"style-{style}")}
    Path(style_job["dir"]).mkdir()
    style_options = cinematic.normalize_options({"music_source": "original_audio", "audio_mode": "original_audio",
                                                   "caption_mode": "all_dialogue", "caption_style": style,
                                                   "caption_language": "ru", "caption_size": 88})
    style_result = cinematic.prepare_captions(style_job, caption_source, caption_plan, style_options, existing_words=long_words)
    style_output = media_dir / f"style-{style}.mp4"
    cinematic.render_plan(caption_source, None, caption_plan, style_options, style_output,
                          captions_ass=Path(style_job["dir"]) / "captions.ass")
    frame = media_dir / f"style-{style}.png"
    plain_frame = media_dir / "plain.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(style_output), "-frames:v", "1", str(frame)], check=True)
    if not plain_frame.is_file():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(plain_output), "-frames:v", "1", str(plain_frame)], check=True)
    from PIL import Image
    a = np.asarray(Image.open(frame).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(plain_frame).convert("RGB"), dtype=np.int16)
    changed = float(np.mean(np.abs(a - b)))
    ass_text = (Path(style_job["dir"]) / "captions.ass").read_text(encoding="utf-8")
    check(f"{style} burns visible Russian captions with bounded layout", style_result["word_count"] == len(long_words) and
          changed > 0.4 and "Noto Sans" in ass_text and ass_text.count("\\N") <= 1)

# A failing mechanical gate must not replace a previous successful MP4.
gate_dir = media_dir / "gate-job"
gate_dir.mkdir()
old_result = gate_dir / "result.mp4"
shutil.copyfile(intro_output, old_result)
duplicate_plan = {**dialogue_plan, "segments": [dict(dialogue_plan["segments"][0]), dict(dialogue_plan["segments"][1])]}
duplicate_plan["segments"][1]["source_id"] = duplicate_plan["segments"][0]["source_id"]
duplicate_plan["total_duration"] = 4
(gate_dir / "edit-plan.json").write_text(json.dumps(duplicate_plan), encoding="utf-8")
gate_job = {"id": "cin-gate", "user": "u_local", "dir": str(gate_dir), "source_path": str(dialogue_source),
            "options": plain_options, "effective_options": plain_options, "selected_music": {"title": "Original Audio"},
            "repeat": True, "status": "queued", "progress": 0.0, "cancel_requested": False}
cinematic._run_job(gate_job)
gate_result = json.loads((gate_dir / "quality-gate.json").read_text(encoding="utf-8"))
gate_error = json.loads((gate_dir / "error.json").read_text(encoding="utf-8"))
check("Mechanical Quality Gate FAIL is not reported Ready", gate_job["status"] == "error" and gate_result["technical_status"] == "FAIL" and
      gate_error["kind"] == "mechanical_quality_gate")
check("Failed repeat render preserves the previous successful MP4", old_result.read_bytes() == (gate_dir / "result.mp4").read_bytes())

# Style-only rerender must rebuild ASS/SRT from the persisted source-word
# timeline and must not call ASR again.
rerender_dir = media_dir / "rerender-job"
rerender_dir.mkdir()
rerender_plan = {**dialogue_plan, "captions": intro_caption}
(rerender_dir / "edit-plan.json").write_text(json.dumps(rerender_plan), encoding="utf-8")
shutil.copyfile(media_dir / "dialogue" / "captions-timeline.json", rerender_dir / "captions-timeline.json")
shutil.copyfile(media_dir / "dialogue" / "captions.ass", rerender_dir / "captions.ass")
shutil.copyfile(media_dir / "dialogue" / "captions.srt", rerender_dir / "captions.srt")
rerender_job = {"id": "cin-rerender", "user": "u_local", "dir": str(rerender_dir), "source_path": str(dialogue_source),
                "options": intro_options, "effective_options": intro_options, "status": "done", "progress": 1.0,
                "cancel_requested": False, "repeat": False}
cinematic._jobs["cin-rerender"] = rerender_job
original_queue_put = cinematic._queue.put
cinematic._queue.put = lambda _jid: None
try:
    cinematic.rerender("u_local", "cin-rerender", {"caption_style": "movie_original"})
finally:
    cinematic._queue.put = original_queue_put
check("Style-only rerender reuses persisted timestamps without ASR", rerender_job.get("rebuild_captions") is False and
      "Вступление" in (rerender_dir / "captions.srt").read_text(encoding="utf-8") and
      not (rerender_dir / "caption-asr-cache.json").is_file())

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
