"""Make a local, score-blind Stage 5A review pack from one completed recorded job.

It is intentionally offline: it neither calls an AI model nor changes a job.  The private manifest keeps the
pre-existing selector score separate from the randomized viewer-facing pack.
"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path


RANDOM_STARTS = (330, 430, 650, 870, 1200, 1370, 1800, 2200, 2800, 3250, 3500, 3950)


def words_in(words, start, end):
    return " ".join(str(w.get("w", "")) for w in words if start - 0.25 <= float(w.get("s", -1)) <= end + 0.25)


def overlaps(start, end, spans):
    return any(min(end, b) - max(start, a) > 0 for a, b in spans)


def review_span(start, end, duration):
    """Give a selected event setup and first reaction without changing its original boundary."""
    return round(max(0, start - 8), 2), round(min(duration, end + 8), 2)


def encode(source, start, end, target):
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-to", str(end), "-i", str(source),
               "-map", "0:v:0?", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
               "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(target)]
    subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite existing pack: {args.output}")
    job = json.loads(args.job.read_text(encoding="utf-8"))
    root = args.job.parent
    source = Path(job.get("source_path") or root / "source.mp4")
    words_path = root / "words.json"
    if not source.is_file() or not words_path.is_file():
        raise SystemExit("The job needs its source.mp4 and words.json; nothing was written.")
    words = json.loads(words_path.read_text(encoding="utf-8"))
    duration = float(job["duration"])
    selected = [c for c in job.get("clips", []) if c.get("status") != "dropped"]
    spans, rows = [], []
    for n, clip in enumerate(selected, 1):
        start, end = review_span(float(clip["start"]), float(clip["end"]), duration)
        spans.append((start, end))
        rows.append({"source_id": job["id"], "start": start, "end": end, "original_start": clip["start"],
                     "original_end": clip["end"], "original_ai_score": clip.get("score"), "original_decision": "selected",
                     "split": "calibration" if n <= max(1, len(selected) - 2) else "holdout",
                     "transcript": words_in(words, start, end), "context": words_in(words, max(0, start - 25), start - 0.3),
                     "available_signals": "not recorded for this uploaded video"})
    for start in RANDOM_STARTS:
        end = min(duration, start + 42)
        if overlaps(start, end, spans):
            continue
        spans.append((start, end))
        rows.append({"source_id": job["id"], "start": start, "end": end, "original_start": None, "original_end": None,
                     "original_ai_score": None, "original_decision": "not selected (random source window)",
                     "split": "holdout" if len(rows) >= 19 else "calibration", "transcript": words_in(words, start, end),
                     "context": words_in(words, max(0, start - 25), start - 0.3),
                     "available_signals": "not recorded for this uploaded video"})
    if len(rows) < 20:
        raise SystemExit(f"Only {len(rows)} non-overlapping episodes available; nothing was written.")
    args.output.mkdir(parents=True)
    blind = []
    for n, row in enumerate(rows, 1):
        neutral = "E-" + hashlib.sha256(f"stage5a:{n}".encode()).hexdigest()[:8].upper()
        filename = f"{neutral}.mp4"
        encode(source, row["start"], row["end"], args.output / filename)
        row.update(episode_id=neutral, reviewable_mp4=filename,
                   sha256=hashlib.sha256((args.output / filename).read_bytes()).hexdigest())
        blind.append({"episode_id": neutral, "mp4": filename, "questions": [
            "Интересность (0–5)", "Юмор/мемность (0–5)", "Понятность без контекста (0–5)",
            "Есть ли развязка (да/нет)", "Хотели бы пересмотреть (0–5)", "Хотели бы отправить другу (0–5)",
            "Пригодность к публикации (да/нет)", "Отдельно: ASR и композиция (заметка, не оценка события)"]})
    (args.output / "private-manifest.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "blind-review.json").write_text(json.dumps(blind, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "README.txt").write_text(
        "Blind review pack. Watch in the order in blind-review.json. Do not open private-manifest.json before rating.\n"
        "No AI scores, selection decisions, source names or algorithm labels are in the blind file.\n", encoding="utf-8")
    print(f"Created {len(rows)} episodes in {args.output}")


if __name__ == "__main__":
    main()
