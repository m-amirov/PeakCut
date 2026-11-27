"""A refused range download falls back once to the checked local source, without re-running AI."""
import os, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-download-fallback-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")
from pathlib import Path  # noqa: E402
from studio import jobs, media  # noqa: E402

fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    if not ok:
        fails.append(name)


d = Path(os.environ["PEAKCUT_DATA"]) / "job"
d.mkdir()
source = d / "source.mp4"
job = {"id": "download-fallback", "dir": str(d), "duration": 120.0,
       "source": {"type": "url", "url": "https://www.youtube.com/watch?v=abc"},
       "options": {"quality": 1440}, "ai": {"tokens": 123}, "clips": []}
calls = []
real_download, real_probe, real_section = media.download, media.probe, media.download_section


def download(url, out, height):
    calls.append((url, Path(out), height))
    source.write_bytes(b"video")
    return source


media.download = download
media.probe = lambda path: {"width": 1920, "height": 1080, "duration": 120.0}
try:
    first = jobs._whole_source(job)
    second = jobs._whole_source(job)
    check("one full source is downloaded once for every failed stretch", (first, second, len(calls)) == (source, source, 1))
    check("the checked source keeps the existing job and AI result", job.get("source_path") == str(source) and job["ai"]["tokens"] == 123)

    clip = {"id": "clip01", "start": 20.0, "end": 40.0, "score": 88, "title": "Saved title"}
    job.pop("source_path")
    source.unlink()
    media.download_section = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ffmpeg exited with code 4294957242"))
    jobs._piece_again(job, clip)
    check("a retry uses the full-source fallback after a range reset", job.get("source_path") == str(source))
    check("the fallback does not change the chosen timestamps or metadata", (clip["start"], clip["end"], clip["score"], clip["title"]) == (20.0, 40.0, 88, "Saved title"))
finally:
    media.download, media.probe, media.download_section = real_download, real_probe, real_section

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
