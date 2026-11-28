"""Stage 6B.2 duration-capacity regressions for semantic cinematic plans."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="peakcut-cinematic-duration-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studio import cinematic  # noqa: E402


fails = []


def check(label, condition):
    print("PASS" if condition else "FAIL", label)
    if not condition:
        fails.append(label)


def fixture_pool():
    scenes, events = [], []
    source_at = 0.0
    scene_index = 0
    event_index = 0
    for event_index in range(10):
        event_id = f"event-{event_index:04d}"
        shots = []
        event_start = source_at
        for shot_offset, length in enumerate((1.5, 1.5, 2.5, 1.0, 2.5, 2.0)):
            scene_id = f"scene-{scene_index:04d}"
            shot = {"id": scene_id, "start": source_at, "end": source_at + length, "duration": length,
                    "excluded": False, "face_detected": shot_offset == 2, "sharpness": 120,
                    "motion_intensity": 0.12, "visual_group_id": f"visual-group-{event_index:02d}"}
            scenes.append(shot)
            shots.append(scene_id)
            source_at += length
            scene_index += 1
        events.append({"id": event_id, "start": event_start, "end": source_at, "duration": source_at - event_start,
                       "shot_ids": shots, "dialogue": False, "face_shot_count": 1,
                       "dominant_visual_group": f"visual-group-{event_index:02d}", "local_discovery_score": 0.8})
    return scenes, events, source_at


scenes, events, source_duration = fixture_pool()
for requested in (15, 30, 45, 60):
    options = cinematic.normalize_options({"duration": requested, "music_source": "original_audio",
                                            "audio_mode": "original_audio", "smooth_transitions": True})
    plan = cinematic.make_plan(Path("capacity-fixture.mkv"), {"duration": source_duration}, scenes, options, None, None,
                               "fixture", [], events=events)
    check(f"requested {requested}s is carried through the selected plan", plan["requested_duration"] == requested and
          abs(float(plan["total_duration"]) - requested) <= 2 / 30 + 0.01)
    check(f"requested {requested}s uses distinct approved shots", len(plan["segments"]) == len({x["source_id"] for x in plan["segments"]}) and
          all(float(x["source_in"]) >= float(x["approved_source_start"]) - 0.01 and
              float(x["source_out"]) <= float(x["approved_source_end"]) + 0.01 for x in plan["segments"]))

short_scenes = scenes[:6]
short_events = events[:1]
short_options = cinematic.normalize_options({"duration": 30, "music_source": "original_audio", "audio_mode": "original_audio"})
try:
    cinematic.make_plan(Path("insufficient-fixture.mkv"), {"duration": 20}, short_scenes, short_options, None, None,
                        "fixture", [], events=short_events)
except cinematic.InsufficientSuitableFootage as exc:
    check("insufficient approved footage fails closed with an actionable state", "Insufficient suitable footage" in str(exc) and
          "shorter edit" in str(exc))
else:
    check("insufficient approved footage fails closed with an actionable state", False)

media_dir = Path(tempfile.mkdtemp(prefix="peakcut-duration-gate-"))
output = media_dir / "short.mp4"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30:d=5.85",
                "-f", "lavfi", "-i", "sine=frequency=440:d=5.85", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(output)], check=True)
gate = cinematic.quality_gate({"requested_duration": 30, "total_duration": 5.854, "source_duration": 60,
                               "segments": [{"source_id": "a", "source_in": 0, "source_out": 5.854,
                                             "approved_source_start": 0, "approved_source_end": 6,
                                             "timeline_in": 0, "timeline_out": 5.854, "transition": {"duration": 0}}]}, output)
check("quality gate rejects a short plan even when MP4 matches that short plan", gate["technical_status"] == "FAIL" and
      not gate["checks"]["requested_planned_duration"] and gate["checks"]["planned_actual_duration"])

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
