"""Local cinematic-edit engine.

This module is deliberately separate from the established highlight pipeline.  It keeps its own small, JSON-backed
job records, uses FFmpeg for media work, and never makes an AI call unless a caller explicitly opts into the bounded
scene-selector.  Music is local-only: there is no catalogue downloader and unknown licence data stays unknown.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config, ctx, media


ANALYZER_VERSION = "cinematic-audio-1"
PLANNER_VERSION = "cinematic-planner-1"
MUSIC_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov"}
ATMOSPHERE_TAGS = {
    "dramatic": {"cinematic", "dramatic", "orchestral"},
    "dark_cinematic": {"dark", "ambient", "suspense", "electronic"},
    "emotional": {"emotional", "piano", "ambient", "cinematic"},
    "epic": {"epic", "orchestral", "trailer"},
    "natural_film": {"instrumental", "ambient", "cinematic", "natural"},
    "phonk": {"phonk", "action", "electronic"},
}
INTENSITY = {"light": 5.0, "medium": 3.5, "strong": 2.25}
DEFAULT_OPTIONS = {
    "music_source": "auto",
    "music_track_id": "",
    "music_upload_id": "",
    "music_genre": "",
    "music_start_offset": 0.0,
    "atmosphere": "dramatic",
    "duration": 30,
    "effects_intensity": "medium",
    "beat_sync": True,
    "dynamic_zoom": True,
    "color_grading": True,
    "smart_reframe": True,
    "smooth_transitions": True,
    "speed_ramp": False,
    "keep_original_dialogue": False,
    "audio_ducking": True,
    "target_platform": "local",
    "ai_scene_selector": False,
}
ALLOWED_ATMOSPHERES = set(ATMOSPHERE_TAGS)
ALLOWED_INTENSITY = set(INTENSITY)
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MUSIC_DIR = config.DATA_DIR / "music"
MUSIC_FILES = MUSIC_DIR / "tracks"
MUSIC_ANALYSIS = MUSIC_DIR / "analysis"
MUSIC_INDEX = MUSIC_DIR / "library.json"
TEMP_MUSIC = config.TMP_DIR / "cinematic-music"
for _d in (MUSIC_DIR, MUSIC_FILES, MUSIC_ANALYSIS, TEMP_MUSIC):
    _d.mkdir(parents=True, exist_ok=True)

_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_queue: queue.Queue[str] = queue.Queue()
_started = False


class Cancelled(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _json_write(path: Path, value: object):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _json_read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _clean_name(value: str, default: str = "file") -> str:
    name = Path(str(value or default)).name
    return _SAFE.sub("_", name)[:140] or default


def _id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", str(value or "")):
        raise ValueError("Unknown cinematic item.")
    return str(value)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _probe(path: Path) -> dict:
    try:
        raw = media.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=120)
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"FFprobe could not read this file: {str(e)[:180]}") from e
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        duration = float((data.get("format") or {}).get("duration") or video and video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise ValueError("This file has no usable duration.")
    return {"duration": duration, "has_video": bool(video), "has_audio": bool(audio),
            "width": int(video.get("width") or 0) if video else 0,
            "height": int(video.get("height") or 0) if video else 0,
            "audio_codec": audio.get("codec_name", "") if audio else ""}


def normalize_options(options: dict | None) -> dict:
    raw = options if isinstance(options, dict) else {}
    out = dict(DEFAULT_OPTIONS)
    out.update({k: raw[k] for k in DEFAULT_OPTIONS if k in raw})
    if out["music_source"] not in ("auto", "library", "upload", "original_audio"):
        raise ValueError("Music source is not available.")
    if out["atmosphere"] not in ALLOWED_ATMOSPHERES:
        raise ValueError("Choose a supported atmosphere.")
    if out["effects_intensity"] not in ALLOWED_INTENSITY:
        raise ValueError("Choose Light, Medium or Strong effects.")
    try:
        out["duration"] = int(out["duration"])
    except (TypeError, ValueError):
        raise ValueError("Duration must be 15, 30, 45 or 60 seconds.")
    if out["duration"] not in (15, 30, 45, 60):
        raise ValueError("Duration must be 15, 30, 45 or 60 seconds.")
    try:
        out["music_start_offset"] = max(0.0, float(out["music_start_offset"]))
    except (TypeError, ValueError):
        raise ValueError("Music start offset is not valid.")
    for k in ("beat_sync", "dynamic_zoom", "color_grading", "smart_reframe", "smooth_transitions", "speed_ramp",
              "keep_original_dialogue", "audio_ducking", "ai_scene_selector"):
        out[k] = bool(out[k])
    out["music_track_id"] = str(out.get("music_track_id") or "")[:80]
    out["music_upload_id"] = str(out.get("music_upload_id") or "")[:80]
    out["music_genre"] = _SAFE.sub("", str(out.get("music_genre") or "").lower())[:40]
    out["target_platform"] = _SAFE.sub("", str(out.get("target_platform") or "local").lower())[:30] or "local"
    return out


def _library() -> list[dict]:
    data = _json_read(MUSIC_INDEX, [])
    return data if isinstance(data, list) else []


def _save_library(items: list[dict]):
    _json_write(MUSIC_INDEX, items)


def _track_path(track: dict) -> Path:
    rel = Path(str(track.get("file") or ""))
    path = (MUSIC_DIR / rel).resolve()
    if MUSIC_DIR.resolve() not in path.parents or not path.is_file():
        raise FileNotFoundError("The music file is no longer in the PeakCut music library.")
    return path


def _public_track(track: dict) -> dict:
    out = {k: v for k, v in track.items() if k not in ("file", "analysis_file")}
    out["file_name"] = Path(str(track.get("file") or "")).name
    return out


def list_music(filters: dict | None = None) -> list[dict]:
    filters = filters if isinstance(filters, dict) else {}
    genre = str(filters.get("genre") or "").lower()
    mood = str(filters.get("mood") or "").lower()
    favorite = filters.get("favorite")
    query = str(filters.get("q") or "").lower()
    try:
        min_bpm = float(filters.get("min_bpm")) if filters.get("min_bpm") not in (None, "") else None
        max_bpm = float(filters.get("max_bpm")) if filters.get("max_bpm") not in (None, "") else None
        min_duration = float(filters.get("min_duration")) if filters.get("min_duration") not in (None, "") else None
        max_duration = float(filters.get("max_duration")) if filters.get("max_duration") not in (None, "") else None
    except (TypeError, ValueError):
        raise ValueError("Music filters must contain numeric BPM and duration values.")
    out = []
    for track in _library():
        tags = {str(x).lower() for x in (track.get("genres") or []) + (track.get("moods") or [])}
        if genre and genre not in tags:
            continue
        if mood and mood not in tags:
            continue
        if favorite is True and not track.get("favorite"):
            continue
        if query and query not in " ".join(str(track.get(k) or "") for k in ("title", "artist", "file_name", "source_url")).lower():
            continue
        analysis = track.get("analysis") or {}
        bpm = float(analysis.get("bpm_estimate") or 0)
        duration = float(track.get("duration") or analysis.get("duration") or 0)
        if min_bpm is not None and bpm < min_bpm:
            continue
        if max_bpm is not None and bpm > max_bpm:
            continue
        if min_duration is not None and duration < min_duration:
            continue
        if max_duration is not None and duration > max_duration:
            continue
        out.append(_public_track(track))
    return sorted(out, key=lambda x: (not x.get("favorite", False), str(x.get("title") or "").lower()))


def validate_music(path: Path) -> dict:
    if path.suffix.lower() not in MUSIC_EXTS:
        raise ValueError("Music must be MP3, WAV, M4A, FLAC or OGG.")
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("The music file is empty or missing.")
    info = _probe(path)
    if not info["has_audio"]:
        raise ValueError("This file has no audio stream.")
    return info


def validate_video(path: Path) -> dict:
    if path.suffix.lower() not in VIDEO_EXTS:
        raise ValueError("Source video must be MP4, MKV or MOV.")
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("The source video is empty or missing.")
    info = _probe(path)
    if not info["has_video"]:
        raise ValueError("This file has no video stream.")
    return info


def _license(meta: dict) -> dict:
    platforms = meta.get("allowed_platforms", [])
    if isinstance(platforms, str):
        platforms = [x.strip().lower() for x in platforms.split(",") if x.strip()]
    if not isinstance(platforms, list):
        platforms = []
    confirmed = bool(meta.get("license_confirmed", False))
    return {"status": "confirmed" if confirmed else "unknown", "confirmed_by_user": confirmed,
            "type": str(meta.get("license_type") or "")[:160], "source_url": str(meta.get("source_url") or "")[:500],
            "license_url": str(meta.get("license_url") or "")[:500], "text": str(meta.get("license_text") or "")[:5000],
            "attribution": str(meta.get("attribution") or "")[:1000], "allowed_platforms": [str(x).lower()[:30] for x in platforms[:20]],
            "content_id": str(meta.get("content_id_status") or "unknown") if meta.get("content_id_status") in ("known", "unknown", "registered") else "unknown"}


def import_music(path: Path, metadata: dict | None = None) -> dict:
    metadata = metadata if isinstance(metadata, dict) else {}
    info = validate_music(path)
    digest = _sha256(path)
    items = _library()
    duplicate = next((x for x in items if x.get("sha256") == digest), None)
    if duplicate:
        return _public_track(duplicate)
    tid = "trk-" + uuid.uuid4().hex[:14]
    ext = path.suffix.lower()
    dest = MUSIC_FILES / f"{tid}{ext}"
    shutil.copy2(path, dest)
    genres = metadata.get("genres", metadata.get("genre", []))
    moods = metadata.get("moods", metadata.get("mood", []))
    if isinstance(genres, str):
        genres = [x.strip().lower() for x in genres.split(",") if x.strip()]
    if isinstance(moods, str):
        moods = [x.strip().lower() for x in moods.split(",") if x.strip()]
    track = {"id": tid, "title": str(metadata.get("title") or path.stem)[:200], "artist": str(metadata.get("artist") or "")[:160],
             "genres": [str(x).lower()[:40] for x in (genres or [])[:20]], "moods": [str(x).lower()[:40] for x in (moods or [])[:20]],
             "file": str(dest.relative_to(MUSIC_DIR)).replace("\\", "/"), "sha256": digest,
             "imported_at": _now(), "favorite": False, "use_count": 0, "last_used": 0,
             "license": _license(metadata), "analysis": None}
    track["duration"] = round(info["duration"], 3)
    track["analysis"] = analyze_audio(dest, digest=digest)
    _save_library(items + [track])
    return _public_track(track)


def update_music(tid: str, changes: dict) -> dict:
    tid = _id(tid)
    items = _library()
    for track in items:
        if track.get("id") != tid:
            continue
        changes = changes if isinstance(changes, dict) else {}
        for key in ("title", "artist"):
            if key in changes:
                track[key] = str(changes[key] or "")[:200 if key == "title" else 160]
        for key in ("genres", "moods"):
            if key in changes:
                value = changes[key]
                value = value.split(",") if isinstance(value, str) else value
                track[key] = [str(x).strip().lower()[:40] for x in (value or []) if str(x).strip()][:20]
        if "favorite" in changes:
            track["favorite"] = bool(changes["favorite"])
        if "license" in changes and isinstance(changes["license"], dict):
            old = track.get("license") or {}
            new = dict(old)
            new.update(_license(changes["license"]))
            track["license"] = new
        _save_library(items)
        return _public_track(track)
    raise FileNotFoundError("Track not found.")


def reanalyze_music(tid: str) -> dict:
    tid = _id(tid)
    items = _library()
    for track in items:
        if track.get("id") == tid:
            path = _track_path(track)
            track["analysis"] = analyze_audio(path, digest=track["sha256"])
            track["duration"] = track["analysis"]["duration"]
            _save_library(items)
            return _public_track(track)
    raise FileNotFoundError("Track not found.")


def delete_music(tid: str, confirmed: bool = False) -> dict:
    tid = _id(tid)
    if not confirmed:
        raise PermissionError("Confirm removal of this composition.")
    for job in _all_jobs():
        if (job.get("selected_music") or {}).get("track_id") == tid:
            raise PermissionError("This composition is used by a saved cinematic edit. Keep it or remove that edit first.")
    items = _library()
    for track in items:
        if track.get("id") == tid:
            try:
                _track_path(track).unlink(missing_ok=True)
            except (OSError, FileNotFoundError):
                pass
            _save_library([x for x in items if x.get("id") != tid])
            return {"ok": True}
    raise FileNotFoundError("Track not found.")


def temp_music(path: Path) -> dict:
    info = validate_music(path)
    uid = "upl-" + uuid.uuid4().hex[:14]
    dest = TEMP_MUSIC / f"{uid}{path.suffix.lower()}"
    shutil.copy2(path, dest)
    return {"id": uid, "path": str(dest), "name": _clean_name(path.name), "duration": info["duration"]}


def _temp_music_path(upload_id: str) -> Path:
    upload_id = _id(upload_id)
    matches = list(TEMP_MUSIC.glob(upload_id + ".*"))
    if len(matches) != 1 or matches[0].suffix.lower() not in MUSIC_EXTS:
        raise FileNotFoundError("The music upload has expired. Upload it again.")
    return matches[0]


def _frame_features(path: Path, out_dir: Path, cancel=None) -> list[dict]:
    info = validate_video(path)
    duration = info["duration"]
    frame_size = 320 * 180 * 3
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-an", "-sn", "-vf", "fps=2,scale=320:180:flags=bilinear,format=rgb24",
                             "-f", "rawvideo", "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    records, previous, index = [], None, 0
    try:
        while True:
            if cancel and cancel():
                proc.terminate()
                raise Cancelled()
            raw = proc.stdout.read(frame_size)
            if len(raw) != frame_size:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(180, 320, 3)
            gray = frame.mean(axis=2)
            diff = float(np.mean(np.abs(frame.astype(np.float32) - previous.astype(np.float32))) / 255.0) if previous is not None else 0.0
            gx = np.diff(gray, axis=1)
            gy = np.diff(gray, axis=0)
            sharp = float((gx[:, :-1] ** 2).mean() + (gy[:-1, :] ** 2).mean())
            records.append({"t": round(index / 2.0, 3), "motion": round(diff, 6), "sharpness": round(sharp, 4),
                            "brightness": round(float(gray.mean() / 255.0), 6), "black_ratio": round(float((gray < 8).mean()), 6)})
            previous, index = frame, index + 1
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not records:
        raise RuntimeError("FFmpeg did not return any video frames.")
    out_dir.mkdir(parents=True, exist_ok=True)
    cuts = []
    last = 0.0
    for row in records[1:]:
        if row["motion"] >= 0.18 and row["t"] - last >= 0.75 and row["t"] < duration - 0.4:
            cuts.append(row["t"])
            last = row["t"]
    points = [0.0] + cuts + [duration]
    scenes = []
    frame_dir = out_dir / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    for n, (a, b) in enumerate(zip(points, points[1:]), 1):
        rows = [x for x in records if a <= x["t"] < b] or [min(records, key=lambda x: abs(x["t"] - (a + b) / 2))]
        black = float(np.mean([x["black_ratio"] for x in rows]))
        excluded = black >= 0.94 or b - a < 0.65
        mid = max(0.0, min(duration - 0.05, (a + b) / 2))
        frame_name = f"scene-{n:04d}.jpg"
        if not excluded:
            try:
                media.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{mid:.3f}", "-i", str(path), "-frames:v", "1",
                           "-vf", "scale=320:-2", "-q:v", "5", str(frame_dir / frame_name)], timeout=30)
            except Exception:
                frame_name = ""
        scenes.append({"id": f"scene-{n:04d}", "start": round(a, 3), "end": round(b, 3), "duration": round(b - a, 3),
                       "representative_frame": str((Path("frames") / frame_name).as_posix()) if frame_name else "",
                       "motion_intensity": round(float(np.mean([x["motion"] for x in rows])), 6),
                       "sharpness": round(float(np.mean([x["sharpness"] for x in rows])), 4),
                       "brightness": round(float(np.mean([x["brightness"] for x in rows])), 6),
                       "black_frame": black >= 0.94, "excluded": excluded,
                       "exclusion_reason": "black-frame" if black >= 0.94 else "too-short" if b - a < 0.65 else ""})
    return scenes


def analyze_scenes(path: Path, output_dir: Path, cancel=None) -> list[dict]:
    scenes = _frame_features(path, output_dir, cancel=cancel)
    _json_write(output_dir / "scene-analysis.json", {"version": "cinematic-scenes-1", "source": path.name, "scenes": scenes})
    return scenes


def _sections(values: np.ndarray, timestamps: np.ndarray, predicate) -> list[list[float]]:
    if not len(values):
        return []
    yes = predicate(values)
    out, start = [], None
    for i, active in enumerate(yes):
        if active and start is None:
            start = float(timestamps[i])
        if start is not None and (not active or i == len(yes) - 1):
            end = float(timestamps[i] + (timestamps[1] - timestamps[0] if len(timestamps) > 1 else 1.0))
            if end - start >= 0.5:
                out.append([round(start, 3), round(end, 3)])
            start = None
    return out


def analyze_audio(path: Path, digest: str | None = None) -> dict:
    info = validate_music(path)
    digest = digest or _sha256(path)
    cached = MUSIC_ANALYSIS / f"{digest}-{ANALYZER_VERSION}.json"
    if cached.is_file():
        got = _json_read(cached, None)
        if isinstance(got, dict):
            return got
    wav = config.TMP_DIR / f"cinematic-analysis-{uuid.uuid4().hex}.wav"
    try:
        media.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "22050", "-c:a", "pcm_s16le", str(wav)], timeout=900)
        frame, hop, sr = 2048, 512, 22050
        window = np.hanning(frame).astype(np.float32)
        energies, flux = [], []
        previous = None
        carry = np.zeros(0, dtype=np.float32)
        with sf.SoundFile(str(wav)) as audio:
            while True:
                block = audio.read(65536, dtype="float32", always_2d=False)
                if len(block) == 0:
                    break
                if getattr(block, "ndim", 1) > 1:
                    block = block.mean(axis=1)
                samples = np.concatenate((carry, np.asarray(block, dtype=np.float32)))
                count = max(0, (len(samples) - frame) // hop + 1)
                for n in range(count):
                    x = samples[n * hop:n * hop + frame] * window
                    energies.append(float(np.sqrt(np.mean(x * x) + 1e-10)))
                    mag = np.abs(np.fft.rfft(x))
                    mag /= max(1e-6, float(mag.sum()))
                    flux.append(float(np.maximum(0, mag - previous).sum()) if previous is not None else 0.0)
                    previous = mag
                carry = samples[count * hop:]
        env = np.asarray(flux, dtype=np.float32)
        energy = np.asarray(energies, dtype=np.float32)
        step = hop / sr
        times = np.arange(len(env), dtype=np.float32) * step
        if len(env):
            baseline = float(np.percentile(env, 45))
            spread = float(np.std(env)) + 1e-7
            normalized = np.maximum(0, env - baseline)
            threshold = baseline + spread * 0.55
            gap = max(1, int(0.18 / step))
            peaks = [i for i in range(1, len(env) - 1) if env[i] >= env[i - 1] and env[i] >= env[i + 1] and env[i] >= threshold]
            onsets = []
            for i in peaks:
                if not onsets or i - onsets[-1] >= gap:
                    onsets.append(i)
                elif env[i] > env[onsets[-1]]:
                    onsets[-1] = i
            corr_scores = []
            for bpm in range(60, 181):
                lag = max(1, int(round((60.0 / bpm) / step)))
                if lag >= len(normalized):
                    continue
                a, b = normalized[:-lag], normalized[lag:]
                denom = float(np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
                corr_scores.append((float(np.dot(a, b) / denom), bpm))
            corr_scores.sort(reverse=True)
            confidence = max(0.0, min(1.0, ((corr_scores[0][0] - np.median([x[0] for x in corr_scores])) * 4) if corr_scores else 0.0))
            bpm = int(corr_scores[0][1]) if corr_scores and confidence >= 0.12 else None
            accents = [round(float(times[i]), 3) for i in sorted(onsets, key=lambda j: env[j], reverse=True)[:80]]
            accents.sort()
            quiet = _sections(energy, times, lambda x: x <= np.percentile(x, 25))
            high = _sections(energy, times, lambda x: x >= np.percentile(x, 75))
            build = []
            if len(energy) > 8:
                slope = np.convolve(energy, np.ones(8) / 8, mode="same")
                build = _sections(np.gradient(slope), times, lambda x: x >= max(0, np.percentile(x, 72)))
        else:
            bpm, confidence, accents, quiet, high, build = None, 0.0, [], [], [], []
        samples_rms = float(np.sqrt(np.mean(energy * energy) + 1e-10)) if len(energy) else 0.0
        lufs = 20 * math.log10(max(samples_rms, 1e-5)) - 0.691
        analysis = {"analyzer_version": ANALYZER_VERSION, "sha256": digest, "duration": round(info["duration"], 3),
                    "bpm_estimate": bpm, "beat_timestamps": ([round(float(x), 3) for x in np.arange(0, info["duration"], 60.0 / bpm)] if bpm else []),
                    "onset_timestamps": accents, "strong_accents": accents, "quiet_sections": quiet,
                    "build_up_sections": build, "high_energy_sections": high, "loudness_lufs_estimate": round(lufs, 2),
                    "true_peak_dbtp_estimate": round(20 * math.log10(max(float(np.max(np.abs(energy))) if len(energy) else 1e-5, 1e-5)), 2),
                    "rhythm_confidence": round(float(confidence), 3),
                    "rhythm_fallback": not bool(bpm),
                    "phrase_timestamps": [round(float(x), 3) for x in np.arange(0, info["duration"], 4.0)]}
        _json_write(cached, analysis)
        return analysis
    finally:
        wav.unlink(missing_ok=True)


def _library_track(tid: str) -> dict:
    for track in _library():
        if track.get("id") == tid:
            return track
    raise FileNotFoundError("Track not found.")


def music_path(tid: str) -> Path:
    return _track_path(_library_track(tid))


def choose_music(options: dict, duration: float) -> tuple[dict | None, str, list[str]]:
    source = options["music_source"]
    warnings = []
    if source == "original_audio":
        return None, "Original Audio selected by the user.", warnings
    if source == "upload":
        path = _temp_music_path(options["music_upload_id"])
        analysis = analyze_audio(path)
        if options["music_start_offset"] >= analysis["duration"]:
            options["music_start_offset"] = 0.0
            warnings.append("Music start offset exceeded the uploaded track; it was reset to 0 seconds.")
        return {"track_id": "upload", "path": str(path), "title": path.stem, "license": {"status": "unknown"}, "analysis": analysis}, "User-uploaded music selected; licence status remains user responsibility.", warnings
    if source == "library":
        track = _library_track(options["music_track_id"])
        path = _track_path(track)
        if not track.get("analysis"):
            track["analysis"] = analyze_audio(path, digest=track.get("sha256"))
        if options["music_start_offset"] >= track["analysis"]["duration"]:
            options["music_start_offset"] = 0.0
            warnings.append("Music start offset exceeded the selected track; it was reset to 0 seconds.")
        return {"track_id": track["id"], "path": str(path), "title": track.get("title", path.stem), "license": track.get("license", {}),
                "analysis": track["analysis"]}, "Manually selected from the local music library.", warnings
    tags = ATMOSPHERE_TAGS.get(options["atmosphere"], set())
    requested_genre = options.get("music_genre")
    target = options.get("target_platform", "local")
    candidates = []
    items = _library()
    for track in items:
        lic = track.get("license") or {}
        allowed = set(lic.get("allowed_platforms") or [])
        if lic.get("status") != "confirmed" or not lic.get("confirmed_by_user"):
            continue
        if allowed and target not in allowed and "any" not in allowed and target != "local":
            continue
        path = _track_path(track)
        if not track.get("analysis"):
            track["analysis"] = analyze_audio(path, digest=track.get("sha256"))
        if track["analysis"]["duration"] + 0.25 < duration:
            continue
        all_tags = {str(x).lower() for x in (track.get("genres") or []) + (track.get("moods") or [])}
        tag_score = len(tags & all_tags) / max(1, len(tags))
        genre_score = 1.0 if requested_genre and requested_genre in all_tags else 0.0 if requested_genre else 0.5
        rhythm = float(track["analysis"].get("rhythm_confidence") or 0)
        history = 1.0 / (1 + int(track.get("use_count") or 0))
        score = tag_score * 0.50 + genre_score * 0.20 + rhythm * 0.15 + history * 0.15
        candidates.append((score, track))
    if not candidates:
        return None, "No confirmed, platform-compatible local track is long enough; choose Upload or Original Audio.", ["music_library_empty_or_not_eligible"]
    candidates.sort(key=lambda x: (-x[0], float(x[1].get("last_used") or 0)))
    score, track = candidates[0]
    track["use_count"] = int(track.get("use_count") or 0) + 1
    track["last_used"] = _now()
    _save_library(items)
    path = _track_path(track)
    if options["music_start_offset"] >= track["analysis"]["duration"]:
        options["music_start_offset"] = 0.0
        warnings.append("Music start offset exceeded the auto-selected track; it was reset to 0 seconds.")
    return {"track_id": track["id"], "path": str(path), "title": track.get("title", path.stem), "license": track.get("license", {}),
            "analysis": track["analysis"]}, f"Auto-selected '{track.get('title', path.stem)}': confirmed licence, atmosphere tags, rhythm confidence and usage history scored locally (score {score:.3f}).", warnings


def _scene_score(scene: dict, atmosphere: str, max_motion: float, max_sharp: float) -> float:
    if scene.get("excluded"):
        return -1.0
    motion = min(1.0, float(scene.get("motion_intensity") or 0) / max(0.03, max_motion))
    sharp = min(1.0, float(scene.get("sharpness") or 0) / max(1.0, max_sharp))
    bright = float(scene.get("brightness") or 0.5)
    light = 1.0 - abs(bright - (0.35 if atmosphere == "dark_cinematic" else 0.55))
    return round(sharp * 0.42 + motion * 0.34 + max(0.0, light) * 0.24, 6)


def _nearest_music_marker(analysis: dict | None, at: float) -> float | None:
    if not analysis:
        return None
    points = analysis.get("strong_accents") or analysis.get("beat_timestamps") or analysis.get("phrase_timestamps") or []
    if not points:
        return None
    return min((float(x) for x in points), key=lambda x: abs(x - at))


def make_plan(source: Path, source_info: dict, scenes: list[dict], options: dict, music: dict | None, music_analysis: dict | None,
              reason: str, warnings: list[str]) -> dict:
    duration = min(float(options["duration"]), float(source_info["duration"]))
    usable = [dict(s) for s in scenes if not s.get("excluded") and float(s.get("duration") or 0) >= 0.65]
    if not usable:
        raise RuntimeError("No usable scenes were found in this video.")
    max_motion = max(float(s.get("motion_intensity") or 0) for s in usable) or 1.0
    max_sharp = max(float(s.get("sharpness") or 0) for s in usable) or 1.0
    for scene in usable:
        scene["selection_score"] = _scene_score(scene, options["atmosphere"], max_motion, max_sharp)
    usable.sort(key=lambda s: s["selection_score"], reverse=True)
    target_len = INTENSITY[options["effects_intensity"]]
    selected = []
    for scene in usable:
        if any(not (scene["end"] <= x["start"] or scene["start"] >= x["end"]) for x in selected):
            continue
        selected.append(scene)
        estimated = sum(min(float(x["duration"]), target_len) for x in selected)
        if estimated >= duration:
            break
    if not selected:
        selected = [usable[0]]
    selected.sort(key=lambda s: s["start"])
    segments, cursor = [], 0.0
    transition = 0.3 if options["smooth_transitions"] and len(selected) > 1 else 0.0
    for index, scene in enumerate(selected):
        speed = 1.0
        if options["speed_ramp"] and options["effects_intensity"] == "strong":
            speed = 1.08 if index % 2 else 0.94
        wanted_output = min(float(scene["duration"]) / speed, target_len, max(0.8, duration - cursor))
        source_len = min(float(scene["duration"]), wanted_output * speed)
        source_in = float(scene["start"])
        source_out = min(float(scene["end"]), source_in + source_len)
        output_len = (source_out - source_in) / speed
        timeline_in = 0.0 if not segments else max(0.0, cursor - transition)
        timeline_out = timeline_in + output_len
        segments.append({"source_id": scene["id"], "source_in": round(source_in, 3), "source_out": round(source_out, 3),
                         "timeline_in": round(timeline_in, 3), "timeline_out": round(timeline_out, 3), "playback_speed": speed,
                         "crop": {"mode": "center", "smart_reframe": options["smart_reframe"], "zoom": 1.05 if options["dynamic_zoom"] else 1.0},
                         "transition": {"type": "crossfade" if transition else "hard_cut", "duration": transition},
                         "effects": {"color_grading": options["color_grading"], "intensity": options["effects_intensity"]},
                         "music_beat_reference": _nearest_music_marker(music_analysis, timeline_in),
                         "selection_reason": f"score={scene['selection_score']}; motion={scene['motion_intensity']}; sharpness={scene['sharpness']}"})
        cursor = timeline_out
        if cursor >= duration - 0.05:
            break
    if cursor < duration - 0.5:
        warnings.append("The source did not contain enough distinct usable scenes; the edit is shorter than requested.")
    total = round(min(cursor, duration), 3)
    return {"planner_version": PLANNER_VERSION, "source_name": source.name, "source_duration": round(source_info["duration"], 3),
            "track_id": (music or {}).get("track_id", ""), "music_offset": round(float(options["music_start_offset"]), 3),
            "music_duration": round(float((music_analysis or {}).get("duration") or 0), 3), "atmosphere": options["atmosphere"],
            "intensity": options["effects_intensity"], "requested_duration": options["duration"], "total_duration": total,
            "transition_overlap": transition, "selection_reason": reason, "warnings": warnings, "segments": segments,
            "beat_sync": options["beat_sync"], "ai_scene_selector": bool(options.get("ai_scene_selector"))}


def validate_plan(plan: dict, source_duration: float) -> list[str]:
    errors, last_out = [], 0.0
    segments = plan.get("segments") if isinstance(plan, dict) else None
    if not isinstance(segments, list) or not segments:
        return ["The edit plan has no segments."]
    for i, seg in enumerate(segments):
        try:
            a, b, ti, to = float(seg["source_in"]), float(seg["source_out"]), float(seg["timeline_in"]), float(seg["timeline_out"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"Segment {i + 1} has invalid time values.")
            continue
        if not (0 <= a < b <= source_duration + 0.05):
            errors.append(f"Segment {i + 1} leaves the source boundaries.")
        if to <= ti or ti < -0.01:
            errors.append(f"Segment {i + 1} has an invalid timeline range.")
        if ti > last_out + 0.4:
            errors.append(f"Segment {i + 1} leaves a timeline gap.")
        last_out = max(last_out, to)
    if abs(float(plan.get("total_duration") or 0) - last_out) > 0.2:
        errors.append("Plan duration does not match its segments.")
    return errors


def _ffmpeg_cancel(cmd: list[str], cancel=None):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    while proc.poll() is None:
        if cancel and cancel():
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise Cancelled()
        time.sleep(0.15)
    stderr = proc.stderr.read()[-3000:]
    if proc.returncode:
        raise RuntimeError(f"FFmpeg render failed: {stderr[-1200:]}")


def render_plan(source: Path, music_path: Path | None, plan: dict, options: dict, output: Path, cancel=None) -> dict:
    errors = validate_plan(plan, float(plan.get("source_duration") or 0))
    if errors:
        raise ValueError("Invalid edit plan: " + " ".join(errors))
    segments = plan["segments"]
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for seg in segments:
        cmd += ["-ss", f"{float(seg['source_in']):.3f}", "-t", f"{float(seg['source_out']) - float(seg['source_in']):.3f}", "-i", str(source)]
    music_index = len(segments)
    if music_path:
        cmd += ["-stream_loop", "-1", "-ss", f"{float(plan.get('music_offset') or 0):.3f}", "-i", str(music_path)]
    filters, video_labels, audio_labels = [], [], []
    grading = {"dramatic": (1.08, 0.94, -0.015), "dark_cinematic": (1.12, 0.82, -0.05), "emotional": (1.03, 0.92, 0.01),
               "epic": (1.10, 1.10, 0.0), "natural_film": (1.02, 1.0, 0.0), "phonk": (1.15, 1.16, -0.01)}[options["atmosphere"]]
    for i, seg in enumerate(segments):
        speed = float(seg.get("playback_speed") or 1.0)
        zoom = float((seg.get("crop") or {}).get("zoom") or 1.0)
        if zoom > 1:
            scale = f"scale=1080*{zoom:.4f}:1920*{zoom:.4f}:force_original_aspect_ratio=increase:flags=lanczos"
        else:
            scale = "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos"
        crop = "crop=1080:1920:(in_w-out_w)/2+(in_w-out_w)*0.08*sin(t*0.7):(in_h-out_h)/2+(in_h-out_h)*0.06*cos(t*0.5)"
        if not options["dynamic_zoom"]:
            crop = "crop=1080:1920:(in_w-out_w)/2:(in_h-out_h)/2"
        eq = f"eq=contrast={grading[0]:.3f}:saturation={grading[1]:.3f}:brightness={grading[2]:.3f}" if options["color_grading"] else "null"
        filters.append(f"[{i}:v]setpts=PTS/{speed:.5f},fps=30,{scale},{crop},{eq},setsar=1[v{i}]")
        video_labels.append((f"v{i}", float(seg["timeline_out"]) - float(seg["timeline_in"])))
        if options["keep_original_dialogue"]:
            filters.append(f"[{i}:a]asetpts=PTS-STARTPTS,atempo={1.0 / speed:.5f}[a{i}]")
            audio_labels.append((f"a{i}", float(seg["timeline_out"]) - float(seg["timeline_in"])))
    current = video_labels[0][0]
    accumulated = video_labels[0][1]
    for i in range(1, len(video_labels)):
        label, length = video_labels[i]
        trans = min(0.3 if options["smooth_transitions"] else 0.0, accumulated / 3, length / 3)
        if trans:
            out = f"vx{i}"
            filters.append(f"[{current}][{label}]xfade=transition=fade:duration={trans:.3f}:offset={accumulated - trans:.3f}[{out}]")
            current, accumulated = out, accumulated + length - trans
        else:
            out = f"vx{i}"
            filters.append(f"[{current}][{label}]concat=n=2:v=1:a=0[{out}]")
            current, accumulated = out, accumulated + length
    filters.append(f"[{current}]fade=t=in:st=0:d=0.25,fade=t=out:st={max(0.0, accumulated - 0.25):.3f}:d=0.25[vout]")
    audio = None
    if audio_labels:
        audio = audio_labels[0][0]
        for i in range(1, len(audio_labels)):
            label, length = audio_labels[i]
            trans = min(0.3 if options["smooth_transitions"] else 0.0, accumulated / 3, length / 3)
            out = f"ax{i}"
            if trans:
                filters.append(f"[{audio}][{label}]acrossfade=d={trans:.3f}:c1=tri:c2=tri[{out}]")
            else:
                filters.append(f"[{audio}][{label}]concat=n=2:v=0:a=1[{out}]")
            audio = out
        if music_path:
            filters.append(f"[{music_index}:a]atrim=duration={accumulated:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.35,afade=t=out:st={max(0.0, accumulated - 0.5):.3f}:d=0.5[music]")
            if options["audio_ducking"]:
                filters.append(f"[music][{audio}]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=250[duck]")
                filters.append(f"[duck][{audio}]amix=inputs=2:duration=longest:dropout_transition=0,loudnorm=I=-14:TP=-1.0:print_format=none[aout]")
            else:
                filters.append(f"[music][{audio}]amix=inputs=2:duration=longest:dropout_transition=0,loudnorm=I=-14:TP=-1.0:print_format=none[aout]")
            audio = "aout"
        else:
            filters.append(f"[{audio}]loudnorm=I=-14:TP=-1.0:print_format=none[aout]")
            audio = "aout"
    elif music_path:
        filters.append(f"[{music_index}:a]atrim=duration={accumulated:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.35,afade=t=out:st={max(0.0, accumulated - 0.5):.3f}:d=0.5,loudnorm=I=-14:TP=-1.0:print_format=none[aout]")
        audio = "aout"
    cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]"]
    if audio:
        cmd += ["-map", f"[{audio}]"]
    else:
        cmd += ["-an"]
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd += ["-t", f"{accumulated:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-r", "30", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", str(output)]
    began = time.monotonic()
    _ffmpeg_cancel(cmd, cancel=cancel)
    return {"render_seconds": round(time.monotonic() - began, 3), "duration": round(accumulated, 3), "width": 1080, "height": 1920,
            "fps": 30, "audio": bool(audio), "cpu_ram_vram": "NOT AVAILABLE: runtime metrics are not recorded"}


def _job_file(job: dict, name: str) -> Path:
    return Path(job["dir"]) / name


def _save_job(job: dict):
    _json_write(_job_file(job, "job.json"), {k: v for k, v in job.items() if k != "user"})
    ctx.bump(job.get("user"))


def _all_jobs() -> list[dict]:
    with _lock:
        return list(_jobs.values())


def _load_all():
    with _lock:
        for user_dir in config.USERS_DIR.iterdir():
            root = user_dir / "cinematic"
            for f in root.glob("*/job.json") if root.is_dir() else []:
                data = _json_read(f, {})
                if not isinstance(data, dict) or not data.get("id"):
                    continue
                data["user"] = user_dir.name
                data["dir"] = str(f.parent)
                if data.get("status") in ("running", "queued"):
                    data["status"], data["stage"] = "queued", "Waiting after restart"
                    data["cancel_requested"] = False
                _jobs[data["id"]] = data


def public_job(job: dict) -> dict:
    out = {k: v for k, v in job.items() if k not in ("dir", "user", "source_path", "cancel_requested", "selected_music")}
    out["source"] = {"name": (job.get("source") or {}).get("name", ""), "duration": (job.get("source") or {}).get("duration", 0)}
    out["files"] = {}
    for name, public_name in (("result.mp4", "video"), ("edit-plan.json", "plan"), ("music-analysis.json", "music_analysis"),
                              ("scene-analysis.json", "scenes"), ("license.json", "license"), ("metrics.json", "metrics")):
        if _job_file(job, name).is_file():
            out["files"][public_name] = f"/media/cinematic/{job['id']}/{name}"
    return out


def get_job(uid: str, jid: str) -> dict | None:
    try:
        jid = _id(jid)
    except ValueError:
        return None
    job = _jobs.get(jid)
    return job if job and job.get("user") == uid else None


def list_jobs(uid: str) -> list[dict]:
    return [public_job(x) for x in sorted(_all_jobs(), key=lambda j: -float(j.get("created") or 0)) if x.get("user") == uid]


def any_running() -> bool:
    return any(j.get("status") in ("queued", "running") for j in _all_jobs())


def create_job(uid: str, source_path: Path, filename: str, options: dict | None = None) -> dict:
    info = validate_video(source_path)
    opts = normalize_options(options)
    jid = "cin-" + uuid.uuid4().hex[:14]
    root = config.user_dir(uid) / "cinematic" / jid
    root.mkdir(parents=True, exist_ok=False)
    dest = root / ("source" + source_path.suffix.lower())
    shutil.move(str(source_path), dest)
    job = {"id": jid, "user": uid, "dir": str(root), "created": _now(), "kind": "cinematic_edit", "status": "queued",
           "stage": "Waiting", "progress": 0.0, "error": "", "options": opts,
           "source": {"name": _clean_name(filename, "source"), "duration": round(info["duration"], 3)}, "source_path": str(dest),
           "warnings": [], "selection_reason": "", "selected_music": {}, "repeat": False}
    with _lock:
        _jobs[jid] = job
    _save_job(job)
    _queue.put(jid)
    return public_job(job)


def cancel_job(uid: str, jid: str) -> dict:
    job = get_job(uid, jid)
    if not job:
        raise FileNotFoundError("Cinematic edit not found.")
    if job.get("status") in ("done", "error", "cancelled"):
        return public_job(job)
    job["cancel_requested"] = True
    job["stage"] = "Cancelling"
    _save_job(job)
    return public_job(job)


def rerender(uid: str, jid: str) -> dict:
    job = get_job(uid, jid)
    if not job:
        raise FileNotFoundError("Cinematic edit not found.")
    if not _job_file(job, "edit-plan.json").is_file():
        raise ValueError("There is no saved edit plan to render again.")
    job.update(status="queued", stage="Waiting to render saved plan", progress=0.0, error="", cancel_requested=False, repeat=True)
    _save_job(job)
    _queue.put(jid)
    return public_job(job)


def _update(job: dict, **values):
    with _lock:
        job.update(values)
    _save_job(job)


def _ai_scene_notes(job: dict, scenes: list[dict], options: dict) -> dict:
    if not options.get("ai_scene_selector"):
        return {"status": "not_requested", "model": "", "calls": 0, "estimated_cost_usd": None}
    candidates = [s for s in scenes if not s.get("excluded")][:12]
    prompt = ("Select cinematic scenes only from this bounded list. Do not invent content. Return JSON list with source_id, "
              "score 0-100, and short reason. Atmosphere: " + options["atmosphere"] + ".\n" + json.dumps(candidates, ensure_ascii=False))
    try:
        from . import brain
        answer, model, spent = brain.ask_ai(prompt, {"type": "array", "items": {"type": "object"}}, purpose="Cinematic scene selector",
                                            timeout=45, cheap=True, model_override="google/gemini-2.5-flash-lite")
        return {"status": "available", "model": model, "calls": 1, "tokens": spent, "estimated_cost_usd": None,
                "recommendations": answer if isinstance(answer, list) else []}
    except Exception as e:  # noqa: BLE001
        return {"status": "fallback_local", "model": "google/gemini-2.5-flash-lite", "calls": 1, "error": str(e)[:240], "estimated_cost_usd": None}


def _run_job(job: dict):
    uid = job["user"]
    source = Path(job["source_path"])
    options = normalize_options(job.get("options"))
    def cancelled():
        return bool(job.get("cancel_requested"))
    try:
        if cancelled():
            raise Cancelled()
        source_info = validate_video(source)
        fallback_original = options["music_source"] == "original_audio"
        options["keep_original_dialogue"] = (options["keep_original_dialogue"] or fallback_original) and source_info["has_audio"]
        if job.get("repeat") and _job_file(job, "edit-plan.json").is_file():
            _update(job, status="running", stage="Rendering saved edit plan", progress=0.55)
            plan = _json_read(_job_file(job, "edit-plan.json"), {})
            selected = job.get("selected_music") or {}
            music_path = Path(selected["path"]) if selected.get("path") else None
            if music_path and not music_path.is_file() and selected.get("track_id") not in ("", "upload"):
                track = _library_track(selected["track_id"])
                music_path = _track_path(track)
            metrics = render_plan(source, music_path, plan, options, _job_file(job, "result.mp4"), cancel=cancelled)
        else:
            _update(job, status="running", stage="Detecting scenes", progress=0.1)
            scenes = analyze_scenes(source, Path(job["dir"]), cancel=cancelled)
            ai_notes = _ai_scene_notes(job, scenes, options)
            _json_write(_job_file(job, "ai-scene-selection.json"), ai_notes)
            _update(job, stage="Selecting music", progress=0.28)
            music, reason, warnings = choose_music(options, options["duration"])
            music_analysis = (music or {}).get("analysis")
            if music and not music_analysis:
                music_analysis = analyze_audio(Path(music["path"]))
                music["analysis"] = music_analysis
            if music_analysis:
                _json_write(_job_file(job, "music-analysis.json"), {"track_id": music.get("track_id"), **music_analysis})
            else:
                _json_write(_job_file(job, "music-analysis.json"), {"source": "original_audio", "reason": reason, "analysis": "NOT APPLICABLE"})
            if not music and options["music_source"] == "auto":
                options["keep_original_dialogue"] = source_info["has_audio"]
                warnings.append("Auto music did not find a confirmed eligible library track; Original Audio was used.")
            _update(job, stage="Building edit plan", progress=0.45, warnings=warnings, selection_reason=reason,
                    selected_music={k: v for k, v in (music or {}).items() if k != "analysis"})
            plan = make_plan(source, source_info, scenes, options, music, music_analysis, reason, warnings)
            plan_errors = validate_plan(plan, source_info["duration"])
            if plan_errors:
                raise ValueError("Invalid edit plan: " + " ".join(plan_errors))
            _json_write(_job_file(job, "edit-plan.json"), plan)
            license_meta = (music or {}).get("license") if music else {"status": "not_applicable", "reason": "Original Audio"}
            _json_write(_job_file(job, "license.json"), {"track_id": (music or {}).get("track_id", ""), "license": license_meta,
                                                             "attribution": (license_meta or {}).get("attribution", "")})
            _update(job, stage="Rendering MP4", progress=0.62)
            metrics = render_plan(source, Path(music["path"]) if music else None, plan, options, _job_file(job, "result.mp4"), cancel=cancelled)
        _json_write(_job_file(job, "metrics.json"), {**metrics, "ai_usage": _json_read(_job_file(job, "ai-scene-selection.json"), {"status": "not_requested"})})
        _update(job, status="done", stage="Ready", progress=1.0, error="", repeat=False, finished_at=_now())
    except Cancelled:
        _update(job, status="cancelled", stage="Cancelled", progress=job.get("progress", 0), error="Cancelled by you.", repeat=False)
        _job_file(job, "result.mp4").unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        _json_write(_job_file(job, "error.json"), {"error": str(e)[:1000]})
        _update(job, status="error", stage="Stopped", error=str(e)[:500], repeat=False)


def _worker():
    while True:
        jid = _queue.get()
        try:
            job = _jobs.get(jid)
            if job:
                with ctx.as_user(job["user"]):
                    _run_job(job)
        except Exception:
            pass
        finally:
            _queue.task_done()


def start():
    global _started
    if _started:
        return
    _started = True
    _load_all()
    threading.Thread(target=_worker, daemon=True, name="cinematic-edit-worker").start()
    for job in _all_jobs():
        if job.get("status") == "queued":
            _queue.put(job["id"])
