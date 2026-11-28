"""Re-render the preserved Stage 6B.2 user job without changing its original result."""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOB_DIR = ROOT / "data/users/u_local/cinematic/cin-6687d56a4db540"
OUT_DIR = JOB_DIR / "stage6b2-regression-v3"

sys.path.insert(0, str(ROOT))
from studio import cinematic  # noqa: E402


def read(name, default):
    try:
        return json.loads((JOB_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


if OUT_DIR.exists() and any(OUT_DIR.iterdir()):
    raise SystemExit(f"Refusing to overwrite existing regression artifact: {OUT_DIR}")
OUT_DIR.mkdir(exist_ok=True)

job = read("job.json", {})
source = Path(job["source_path"])
selected = dict(job["selected_music"])
selected["path"] = str(JOB_DIR / "music-source.mp3")
music_analysis = read("music-analysis.json", {})
scenes_data = read("scene-analysis.json", [])
events_data = read("event-analysis.json", [])
scenes = scenes_data.get("scenes", []) if isinstance(scenes_data, dict) else scenes_data
events = events_data.get("events", []) if isinstance(events_data, dict) else events_data
ai_notes = read("ai-scene-selection.json", {"status": "not_requested"})
options = cinematic.normalize_options(job["effective_options"])
source_info = {"duration": float(job["source"]["duration"]), "has_audio": True}
plan = cinematic.make_plan(source, source_info, scenes, options, selected, music_analysis,
                           job.get("selection_reason", "").strip(), [], ai_notes=ai_notes, events=events)
plan["captions"] = {"status": "ready", "mode": "off", "audio_mode": options["audio_mode"]}
cinematic._json_write(OUT_DIR / "edit-plan.json", plan)

output = OUT_DIR / "result.mp4"
metrics = cinematic.render_plan(source, Path(selected["path"]), plan, options, output)
gate = cinematic.quality_gate(plan, output)
cinematic._json_write(OUT_DIR / "metrics.json", metrics)
cinematic._json_write(OUT_DIR / "quality-gate.json", gate)
shutil.copy2(JOB_DIR / "scene-analysis.json", OUT_DIR / "scene-analysis.json")
shutil.copy2(JOB_DIR / "event-analysis.json", OUT_DIR / "event-analysis.json")
shutil.copy2(JOB_DIR / "ai-scene-selection.json", OUT_DIR / "ai-scene-selection.json")
shutil.copy2(JOB_DIR / "music-analysis.json", OUT_DIR / "music-analysis.json")

decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"],
                        capture_output=True, text=True)
probe = cinematic._probe(output)
report = {
    "job_id": job["id"],
    "original_result": {"path": str(JOB_DIR / "result.mp4"), "sha256": sha256(JOB_DIR / "result.mp4"),
                         "bytes": (JOB_DIR / "result.mp4").stat().st_size,
                         "duration": cinematic._probe(JOB_DIR / "result.mp4").get("duration")},
    "regression_result": {"path": str(output), "sha256": sha256(output), "bytes": output.stat().st_size,
                          "duration": probe.get("duration"), "probe": probe, "full_decode": decode.returncode == 0},
    "requested_duration": plan.get("requested_duration"), "planned_duration": plan.get("total_duration"),
    "quality_gate": gate,
    "source_boundaries": all(float(x["source_in"]) >= float(x["approved_source_start"]) - 0.01 and
                              float(x["source_out"]) <= float(x["approved_source_end"]) + 0.01 for x in plan["segments"]),
    "music_acceptance": "BLOCKED" if (selected.get("license") or {}).get("status") != "confirmed" else "NOT_RUN",
    "music_note": "The preserved selected track has unknown/unconfirmed licence metadata; technical render evidence does not establish rights acceptance.",
}
cinematic._json_write(OUT_DIR / "regression-report.json", report)
print(json.dumps(report, ensure_ascii=False, indent=2))
if decode.returncode or gate.get("technical_status") != "PASS" or not report["source_boundaries"]:
    raise SystemExit(1)
