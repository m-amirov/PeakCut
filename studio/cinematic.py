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
PLANNER_VERSION = "cinematic-planner-2"
SEMANTIC_VERSION = "cinematic-semantic-1"
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
    "edit_focus": "auto",
    "edit_focus_text": "",
    "ai_model": "google/gemini-2.5-flash-lite",
}
ALLOWED_ATMOSPHERES = set(ATMOSPHERE_TAGS)
ALLOWED_INTENSITY = set(INTENSITY)
EDIT_FOCUS = {"auto", "character", "event", "emotional", "action"}
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


class MusicRequired(RuntimeError):
    """The requested Auto edit cannot start without a confirmed music track."""

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
    out["edit_focus"] = str(out.get("edit_focus") or "auto").lower()
    if out["edit_focus"] not in EDIT_FOCUS:
        raise ValueError("Choose a supported Edit Focus.")
    out["edit_focus_text"] = str(out.get("edit_focus_text") or "").strip()[:240]
    model = str(out.get("ai_model") or DEFAULT_OPTIONS["ai_model"])
    if not re.fullmatch(r"[A-Za-z0-9._/-]{3,140}", model):
        raise ValueError("The cinematic AI model name is not valid.")
    out["ai_model"] = model
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


def verify_music_decode(path: Path, cancel=None) -> dict:
    """Decode the selected track before rendering and return its probe metadata."""
    info = validate_music(path)
    _ffmpeg_cancel(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-f", "null", "-"], cancel=cancel)
    return {**info, "sha256": _sha256(path)}


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


def _saliency_center(path: Path) -> tuple[float, float, str]:
    """Small local saliency heuristic used for crop bias; never presented as face tracking."""
    try:
        from PIL import Image
        image = np.asarray(Image.open(path).convert("L").resize((96, 54)), dtype=np.float32) / 255.0
        gx = np.abs(np.diff(image, axis=1, prepend=image[:, :1]))
        gy = np.abs(np.diff(image, axis=0, prepend=image[:1, :]))
        weights = 0.35 * image + 0.65 * np.sqrt(gx * gx + gy * gy) + 0.02
        yy, xx = np.mgrid[0:weights.shape[0], 0:weights.shape[1]]
        total = float(weights.sum()) or 1.0
        cx = float((weights * xx).sum() / total / max(1, weights.shape[1] - 1))
        cy = float((weights * yy).sum() / total / max(1, weights.shape[0] - 1))
        return round(max(0.2, min(0.8, cx)), 4), round(max(0.2, min(0.8, cy)), 4), "saliency_heuristic"
    except Exception:  # noqa: BLE001 - centered crop is the explicit safe fallback
        return 0.5, 0.5, "center_fallback"


def _focus_center(path: Path) -> tuple[float, float, str, bool]:
    """Use a light OpenCV face detector when available, then saliency/center fallback."""
    saliency_x, saliency_y, saliency_mode = _saliency_center(path)
    try:
        import cv2
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is not None:
            cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
            faces = cascade.detectMultiScale(image, scaleFactor=1.1, minNeighbors=4, minSize=(18, 18))
            if len(faces):
                face = max(faces, key=lambda x: int(x[2]) * int(x[3]))
                x, y, w, h = [int(v) for v in face]
                return round((x + w / 2) / max(1, image.shape[1]), 4), round((y + h / 2) / max(1, image.shape[0]), 4), "face_haar", True
    except Exception:  # noqa: BLE001 - optional local detector, never a hard dependency
        pass
    return saliency_x, saliency_y, saliency_mode, False


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
        reframe_x, reframe_y, reframe_mode, face_detected = _focus_center(frame_dir / frame_name) if frame_name else (0.5, 0.5, "center_fallback", False)
        scenes.append({"id": f"scene-{n:04d}", "start": round(a, 3), "end": round(b, 3), "duration": round(b - a, 3),
                       "representative_frame": str((Path("frames") / frame_name).as_posix()) if frame_name else "",
                       "motion_intensity": round(float(np.mean([x["motion"] for x in rows])), 6),
                       "sharpness": round(float(np.mean([x["sharpness"] for x in rows])), 4),
                       "brightness": round(float(np.mean([x["brightness"] for x in rows])), 6),
                       "reframe_center_x": reframe_x, "reframe_center_y": reframe_y, "reframe_mode": reframe_mode,
                       "focus_mode": reframe_mode, "face_detected": face_detected,
                       "black_frame": black >= 0.94, "excluded": excluded,
                       "exclusion_reason": "black-frame" if black >= 0.94 else "too-short" if b - a < 0.65 else ""})
    return scenes


def analyze_scenes(path: Path, output_dir: Path, cancel=None) -> list[dict]:
    scenes = _frame_features(path, output_dir, cancel=cancel)
    _json_write(output_dir / "scene-analysis.json", {"version": "cinematic-scenes-1", "source": path.name, "scenes": scenes})
    return scenes


def _read_existing_dialogue(job: dict) -> list[dict]:
    """Use already persisted words/captions when present; never trigger ASR here."""
    root = Path(job.get("dir") or "")
    for name in ("words.json", "transcript.json", "captions.json"):
        data = _json_read(root / name, None)
        rows = data.get("words") if isinstance(data, dict) else data
        if isinstance(data, dict) and not isinstance(rows, list):
            rows = data.get("segments")
        if not isinstance(rows, list):
            continue
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                start = float(row.get("start", row.get("start_time")))
                end = float(row.get("end", row.get("end_time", start)))
            except (TypeError, ValueError):
                continue
            text = str(row.get("text", row.get("word", row.get("content", ""))) or "").strip()
            if text and end >= start:
                out.append({"start": start, "end": end, "text": text})
        if out:
            return out
    return []


def _dialogue_between(words: list[dict], start: float, end: float) -> str:
    return " ".join(str(x.get("text") or "") for x in words if float(x.get("end") or 0) >= start and float(x.get("start") or 0) <= end).strip()[:600]


def analyze_events(scenes: list[dict], output_dir: Path, dialogue: list[dict] | None = None, cancel=None) -> list[dict]:
    """Group adjacent detected shots into bounded events with several frame anchors."""
    usable = [s for s in scenes if not s.get("excluded") and float(s.get("duration") or 0) >= 0.65]
    events, current = [], []

    def flush():
        if not current:
            return
        shots = list(current)
        start, end = float(shots[0]["start"]), float(shots[-1]["end"])
        picks = [shots[0], shots[len(shots) // 2], shots[-1]]
        frames, frame_times = [], []
        for shot in picks:
            frame = str(shot.get("representative_frame") or "")
            if frame and frame not in frames:
                frames.append(frame)
                frame_times.append(round((float(shot["start"]) + float(shot["end"])) / 2, 3))
        brightness = np.asarray([float(x.get("brightness") or 0) for x in shots], dtype=np.float32)
        motion = np.asarray([float(x.get("motion_intensity") or 0) for x in shots], dtype=np.float32)
        dialogue_text = _dialogue_between(dialogue or [], start, end)
        focus_x = [float(x["reframe_center_x"]) for x in shots if x.get("reframe_center_x") is not None]
        focus_y = [float(x["reframe_center_y"]) for x in shots if x.get("reframe_center_y") is not None]
        face_count = sum(1 for x in shots if x.get("focus_mode") == "face_haar")
        # This is only a bounded local discovery hint. It is deliberately not
        # described as semantic understanding; AI or the explicit focus text
        # supplies that layer when available.
        variation = min(1.0, float(np.std(brightness)) * 4 + float(np.std(motion)) * 6) if len(shots) > 1 else 0.0
        local_score = min(1.0, (0.45 if dialogue_text else 0.0) + 0.25 * variation + 0.2 * min(1.0, face_count / max(1, len(shots))) + 0.1 * min(1.0, len(shots) / 5))
        events.append({"id": f"event-{len(events) + 1:04d}", "start": round(start, 3), "end": round(end, 3),
                       "duration": round(end - start, 3), "shot_ids": [str(x["id"]) for x in shots],
                       "representative_frames": frames[:3], "representative_frame_times": frame_times[:3],
                       "dialogue": dialogue_text, "face_shot_count": face_count,
                       "reframe_center_x": round(float(np.median(focus_x)) if focus_x else 0.5, 4),
                       "reframe_center_y": round(float(np.median(focus_y)) if focus_y else 0.5, 4),
                       "focus_mode": "face_haar" if face_count else "saliency_or_center",
                       "local_discovery_score": round(local_score, 6), "shot_count": len(shots)})
        current.clear()

    for shot in usable:
        if cancel and cancel():
            raise Cancelled()
        if current and (float(shot["end"]) - float(current[0]["start"]) > 11.0 or len(current) >= 6):
            flush()
        current.append(shot)
    flush()
    result = {"version": SEMANTIC_VERSION, "dialogue_source": "existing-local-files-only" if dialogue else "not_available",
              "events": events}
    _json_write(output_dir / "event-analysis.json", result)
    return events


def _event_candidates(events: list[dict], limit: int = 8) -> list[dict]:
    if len(events) <= limit:
        return list(events)
    selected, seen = [], set()
    # Coverage is intentional: the AI sees the whole film's possible themes,
    # not only the sharpest or fastest frames.
    for n in range(min(4, limit)):
        index = min(len(events) - 1, round((len(events) - 1) * n / max(1, min(4, limit) - 1)))
        event = events[int(index)]
        if event["id"] not in seen:
            selected.append(event)
            seen.add(event["id"])
    ranked = sorted(events, key=lambda x: (float(x.get("local_discovery_score") or 0), bool(x.get("dialogue"))), reverse=True)
    for event in ranked:
        if len(selected) >= limit:
            break
        if event["id"] not in seen:
            selected.append(event)
            seen.add(event["id"])
    return sorted(selected, key=lambda x: float(x["start"]))


def _validated_event_recommendations(notes: dict | None, events: list[dict]) -> dict[str, dict]:
    if not isinstance(notes, dict) or notes.get("status") != "available":
        return {}
    known = {str(x.get("id")) for x in events}
    rows = notes.get("recommendations")
    if not isinstance(rows, list):
        return {}
    out, seen = {}, set()
    for rank, item in enumerate(rows):
        if not isinstance(item, dict):
            continue
        event_id = str(item.get("event_id") or item.get("source_id") or "")
        if event_id not in known or event_id in seen:
            continue
        try:
            score = float(item.get("score"))
        except (TypeError, ValueError):
            continue
        if not 0 <= score <= 100:
            continue
        phase = str(item.get("phase") or "")[:40]
        out[event_id] = {"score": score, "rank": rank, "phase": phase,
                         "reason": str(item.get("reason") or "AI event recommendation")[:240]}
        seen.add(event_id)
    return out


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


def choose_music(options: dict, duration: float, record_usage: bool = True) -> tuple[dict | None, str, list[str]]:
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
        remaining = float(track["analysis"]["duration"]) - float(options.get("music_start_offset") or 0)
        if remaining + 0.25 < duration:
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
    if record_usage:
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


def _nearest_music_marker(analysis: dict | None, at: float, music_offset: float = 0.0,
                          beat_sync: bool = True) -> float | None:
    """Return the source-time marker nearest to a timeline position.

    The old planner used this value as decoration.  The new planner uses the
    same conversion when it creates the actual timeline boundary, so keeping
    the raw music timestamp in the plan makes the offset auditable.
    """
    if not beat_sync:
        return None
    if not analysis:
        return None
    confidence = float(analysis.get("rhythm_confidence") or 0.0)
    points = (analysis.get("beat_timestamps") if confidence >= 0.35 else None) or analysis.get("strong_accents") or analysis.get("phrase_timestamps") or []
    if not points:
        return None
    return min((float(x) for x in points), key=lambda x: abs(x - (at + music_offset)))


def _music_sync_profile(analysis: dict | None, offset: float, duration: float, intensity: str) -> dict:
    """Build bounded timeline markers without treating every onset as a cut."""
    if not analysis:
        return {"mode": "none", "confidence": 0.0, "markers": []}
    confidence = max(0.0, min(1.0, float(analysis.get("rhythm_confidence") or 0.0)))
    beats = [float(x) for x in (analysis.get("beat_timestamps") or [])]
    accents = [float(x) for x in (analysis.get("strong_accents") or [])]
    phrases = [float(x) for x in (analysis.get("phrase_timestamps") or [])]
    if confidence >= 0.35 and beats:
        base, mode = beats, "beats"
    elif accents:
        base, mode = accents, "accents-fallback"
    elif phrases:
        base, mode = phrases, "phrases-fallback"
    else:
        return {"mode": "natural-fallback", "confidence": confidence, "markers": []}
    # Strong accents select the expressive boundaries for strong edits.  For
    # other intensities they remain a secondary cue instead of forcing a cut
    # on every onset.
    preferred = accents if intensity == "strong" and accents else base
    markers = []
    for raw in sorted(set(round(x, 6) for x in preferred)):
        timeline = raw - float(offset)
        if 0.25 <= timeline <= duration - 0.25:
            markers.append({"timeline": round(timeline, 6), "music": round(raw, 6), "kind": "accent" if raw in accents else mode})
    return {"mode": mode, "confidence": round(confidence, 3), "markers": markers}


def _choose_boundaries(duration: float, count: int, profile: dict) -> tuple[list[float], list[dict | None]]:
    boundaries, references = [0.0], [None]
    used = set()
    markers = list(profile.get("markers") or [])
    for index in range(1, count):
        desired = duration * index / count
        remaining = count - index
        available = [m for m in markers if m["music"] not in used and
                     m["timeline"] > boundaries[-1] + 0.8 and
                     m["timeline"] < duration - remaining * 0.8]
        marker = min(available, key=lambda m: abs(m["timeline"] - desired), default=None)
        if marker is not None and abs(float(marker["timeline"]) - desired) <= 1.35:
            point = float(marker["timeline"])
            used.add(marker["music"])
        else:
            point, marker = desired, None
        boundaries.append(round(point, 6))
        references.append(marker)
    boundaries.append(round(duration, 6))
    references.append(None)
    return boundaries, references


def _validated_ai_recommendations(notes: dict | None, scenes: list[dict]) -> dict[str, dict]:
    if not isinstance(notes, dict) or notes.get("status") != "available":
        return {}
    known = {str(s.get("id")) for s in scenes}
    bounded = notes.get("candidate_ids")
    if isinstance(bounded, list):
        known &= {str(x) for x in bounded}
    out, seen = {}, set()
    recommendations = notes.get("recommendations")
    if not isinstance(recommendations, list):
        return {}
    for rank, item in enumerate(recommendations):
        if not isinstance(item, dict) or str(item.get("source_id")) not in known or str(item.get("source_id")) in seen:
            continue
        try:
            score = float(item.get("score"))
        except (TypeError, ValueError):
            continue
        if not 0.0 <= score <= 100.0:
            continue
        sid = str(item["source_id"])
        seen.add(sid)
        out[sid] = {"score": score, "rank": rank, "reason": str(item.get("reason") or "AI recommendation")[:240]}
    return out


def _music_phases(analysis: dict | None, duration: float) -> list[dict]:
    """Return an intentionally uneven montage rhythm, not one cut per beat."""
    if not analysis:
        return [{"name": "natural", "start": 0.0, "end": duration, "pace": 2.6}]
    return [{"name": "hook", "start": 0.0, "end": duration * 0.18, "pace": 1.8},
            {"name": "build_up", "start": duration * 0.18, "end": duration * 0.58, "pace": 2.8},
            {"name": "climax", "start": duration * 0.58, "end": duration * 0.84, "pace": 1.25},
            {"name": "resolution", "start": duration * 0.84, "end": duration, "pace": 2.8}]


def _phase_at(phases: list[dict], at: float) -> dict:
    return next((x for x in phases if float(x["start"]) <= at < float(x["end"])), phases[-1])


def _select_event_arc(events: list[dict], notes: dict | None, duration: float) -> tuple[list[dict], str, str]:
    if not events:
        return [], "", "fallback_local"
    recommendations = _validated_event_recommendations(notes, events)
    if recommendations:
        target = duration + 1.0
        positions = {str(event["id"]): index for index, event in enumerate(events)}
        recommendation_positions = sorted(positions[event_id] for event_id in recommendations)
        best = None
        for left in range(len(events)):
            total = 0.0
            for right in range(left, min(len(events), left + 40)):
                total += float(events[right]["duration"])
                if total < target:
                    continue
                window = events[left:right + 1]
                hits = sorted(positions[event_id] for event_id in recommendations if left <= positions[event_id] <= right)
                score = sum(float(recommendations[events[index]["id"]]["score"]) / 100.0 for index in hits)
                score += 0.22 * len(hits)
                # Prefer recommendations that form a compact, related run;
                # two distant high-scoring frames should not make a random
                # cross-film montage beat a nearby event sequence.
                for first, second in zip(hits, hits[1:]):
                    score += 0.45 * max(0.0, 1.0 - (second - first) / 24.0)
                score += 0.04 * sum(float(x.get("local_discovery_score") or 0) for x in window) / len(window)
                score -= 0.002 * max(0.0, total - target)
                key = (round(score, 6), len(hits), -left)
                if best is None or key > best[0]:
                    best = (key, window)
                future_recommendations = [position for position in recommendation_positions if position > right]
                close_future = future_recommendations and future_recommendations[0] <= right + 12
                if total >= target + 12.0 and not close_future:
                    break
        arc = best[1] if best else events[:]
        concept = str(notes.get("concept") or "Coherent event sequence")[:300]
        return arc, concept, "ai_semantic"
    target = duration + 1.0
    best = None
    for start in range(len(events)):
        total = 0.0
        for end in range(start, min(len(events), start + 18)):
            total += float(events[end]["duration"])
            if total >= target:
                window = events[start:end + 1]
                score = sum(float(x.get("local_discovery_score") or 0) for x in window) / len(window)
                score += 0.15 if any(x.get("dialogue") for x in window) else 0.0
                candidate = (score, window)
                if best is None or candidate[0] > best[0]:
                    best = candidate
                break
    arc = best[1] if best else events[:]
    return arc, "Local coherent event sequence; semantic AI review was not used.", "fallback_local"


def _make_semantic_plan(source: Path, source_info: dict, scenes: list[dict], events: list[dict], options: dict,
                        music: dict | None, music_analysis: dict | None, reason: str, warnings: list[str],
                        semantic_notes: dict | None) -> dict:
    duration = min(float(options["duration"]), float(source_info["duration"]))
    arc, concept, semantic_status = _select_event_arc(events, semantic_notes, duration)
    by_id = {str(x["id"]): x for x in scenes}
    shot_ids = [shot_id for event in arc for shot_id in event.get("shot_ids") or []]
    shots = [by_id[x] for x in shot_ids if x in by_id and not by_id[x].get("excluded") and float(by_id[x].get("duration") or 0) >= 0.65]
    if not shots:
        raise RuntimeError("No approved shots were found in the selected semantic event sequence.")
    transition = 0.3 if options["smooth_transitions"] and len(shots) > 1 else 0.0
    available = sum(float(x["duration"]) for x in shots)
    if available - transition * max(0, len(shots) - 1) < duration - 0.05:
        transition = 0.0
        warnings.append("The selected coherent event sequence does not contain enough approved shot material for the requested duration; the result was shortened.")
    phases = _music_phases(music_analysis, duration)
    sync_profile = _music_sync_profile(music_analysis, float(options["music_start_offset"]), duration, options["effects_intensity"])
    if not options["beat_sync"]:
        sync_profile = {"mode": "off", "confidence": sync_profile.get("confidence", 0.0), "markers": []}
    markers = list(sync_profile.get("markers") or [])
    # First build visible timeline boundaries.  With Beat Sync enabled a
    # usable marker is the boundary itself; the following segment begins at
    # exactly that frame.  The preceding segment gets the crossfade overlap,
    # so validation and FFmpeg use the same definition of the sync moment.
    boundaries, boundary_refs, speeds, selected_shots = [0.0], [None], [], []
    rendered = 0.0
    for shot in shots:
        if rendered >= duration - 0.02:
            break
        phase = _phase_at(phases, rendered)
        speed = 1.0
        if options["speed_ramp"] and options["effects_intensity"] == "strong":
            speed = 1.04 if phase["name"] == "climax" else 0.98
        max_visible_end = rendered + float(shot["duration"]) / speed - transition
        target_end = min(duration, rendered + float(phase["pace"]))
        marker = None
        if options["beat_sync"] and markers:
            candidates = [x for x in markers if float(x["timeline"]) > rendered + 0.65 and
                          float(x["timeline"]) < duration - 0.15 and
                          float(x["timeline"]) <= max_visible_end + 0.02]
            if candidates:
                nearest = min(candidates, key=lambda x: abs(float(x["timeline"]) - target_end))
                if abs(float(nearest["timeline"]) - target_end) <= max(1.35, float(phase["pace"]) * 2.0):
                    target_end, marker = float(nearest["timeline"]), nearest
        target_end = min(duration, target_end, max_visible_end)
        if target_end - rendered < 0.65:
            continue
        boundaries.append(round(target_end, 6))
        boundary_refs.append(marker if marker and abs(float(marker["timeline"]) - target_end) <= 0.02 else None)
        speeds.append(speed)
        selected_shots.append(shot)
        rendered = target_end
    if not speeds:
        raise RuntimeError("No approved shot contained enough source material for the selected duration.")
    segments, rendered = [], 0.0
    for index, shot in enumerate(selected_shots):
        timeline_in = float(boundaries[index])
        visible_end = float(boundaries[index + 1])
        has_next = index < len(speeds) - 1
        timeline_out = visible_end + (transition if has_next else 0.0)
        speed = speeds[index]
        phase = _phase_at(phases, timeline_in)
        source_in = float(shot["start"])
        source_out = min(float(shot["end"]), source_in + (timeline_out - timeline_in) * speed)
        actual = (source_out - source_in) / speed
        if actual < 0.65:
            continue
        marker = boundary_refs[index]
        event_id = next((event["id"] for event in arc if shot["id"] in (event.get("shot_ids") or [])), "")
        event = next((event for event in arc if event["id"] == event_id), {})
        focus_x = float(shot.get("reframe_center_x") or event.get("reframe_center_x") or 0.5)
        focus_y = float(shot.get("reframe_center_y") or event.get("reframe_center_y") or 0.5)
        ai_rec = _validated_event_recommendations(semantic_notes, events).get(event_id, {}) if semantic_status == "ai_semantic" else {}
        segments.append({"source_id": shot["id"], "event_id": event_id, "source_in": round(source_in, 3), "source_out": round(source_out, 3),
                         "approved_source_start": round(float(shot["start"]), 3), "approved_source_end": round(float(shot["end"]), 3),
                         "timeline_in": round(timeline_in, 3), "timeline_out": round(timeline_out, 3), "playback_speed": speed,
                         "crop": {"mode": "face_or_saliency" if options["smart_reframe"] else "center", "smart_reframe": options["smart_reframe"],
                                  "reframe_limit": "Light Haar face detection when visible; saliency/center fallback, no full-body tracker",
                                  "center_x": round(focus_x, 4), "center_y": round(focus_y, 4), "zoom": 1.05 if options["dynamic_zoom"] else 1.0},
                         "transition": {"type": "crossfade" if transition and index else "hard_cut", "duration": transition if index else 0.0},
                         "effects": {"color_grading": options["color_grading"], "intensity": options["effects_intensity"],
                                     "dynamic_zoom": "time-varying-scale" if options["dynamic_zoom"] else "off",
                                     "speed_ramp": "per-segment-speed-variation" if options["speed_ramp"] else "off"},
                         "music_phase": phase["name"], "music_beat_reference": marker.get("music") if marker else None,
                         "sync_point": round(timeline_in, 3) if marker else None,
                         "sync_point_type": "crossfade_start" if marker and transition else "hard_cut" if marker else "natural",
                         "sync_error_frames": round((timeline_in - float(marker["timeline"])) * 30, 3) if marker else None,
                         "selection_reason": f"{semantic_status}; concept={concept}; event={event_id}; phase={phase['name']}; "
                                             f"ai={ai_rec.get('score')}; dialogue={bool(event.get('dialogue'))}; face_mode={event.get('focus_mode')}"})
        rendered += actual - (transition if index else 0.0)
    # A final crossfade-capacity estimate can leave a few frames unused even
    # though the last approved shot still has source headroom.  Extend only
    # inside that shot; never borrow an unapproved neighbour to hit duration.
    if segments and rendered < duration - 0.001:
        deficit = duration - rendered
        last = segments[-1]
        spare = max(0.0, (float(last["approved_source_end"]) - float(last["source_out"])) / float(last["playback_speed"] or 1.0))
        if spare + 0.02 >= deficit:
            last["source_out"] = round(float(last["source_out"]) + deficit * float(last["playback_speed"] or 1.0), 3)
            last["timeline_out"] = round(float(last["timeline_out"]) + deficit, 3)
            rendered = duration
    if rendered < duration - (2.0 / 30.0 + 0.01):
        warnings.append("The selected event sequence ended before the requested duration; no unapproved neighboring shots were added.")
    total = round(min(rendered, duration), 3)
    return {"planner_version": PLANNER_VERSION, "semantic_version": SEMANTIC_VERSION, "source_name": source.name,
            "source_duration": round(source_info["duration"], 3), "track_id": (music or {}).get("track_id", ""),
            "track_title": (music or {}).get("title", "Original Audio" if options["music_source"] == "original_audio" else ""),
            "music_source": options["music_source"], "music_offset": round(float(options["music_start_offset"]), 3),
            "music_duration": round(float((music_analysis or {}).get("duration") or 0), 3),
            "music_segment": {"start": round(float(options["music_start_offset"]), 3), "end": round(float(options["music_start_offset"]) + total, 3)} if music else None,
            "atmosphere": options["atmosphere"], "intensity": options["effects_intensity"], "requested_duration": options["duration"],
            "total_duration": total, "transition_overlap": transition, "selection_reason": reason,
            "semantic_concept": concept, "semantic_status": semantic_status, "selected_event_ids": [x["id"] for x in arc],
            "warnings": warnings, "segments": segments, "beat_sync": bool(options["beat_sync"]),
            "sync_mode": sync_profile.get("mode", "none") if options["beat_sync"] else "off",
            "rhythm_confidence": sync_profile.get("confidence", 0.0),
            "crossfade_sync_definition": "beat-aligned at crossfade start; visual blend spans the following overlap" if transition else "not applicable",
            "ai_scene_selector": bool(options.get("ai_scene_selector")),
            "ai_recommendations_applied": semantic_status == "ai_semantic"}


def make_plan(source: Path, source_info: dict, scenes: list[dict], options: dict, music: dict | None, music_analysis: dict | None,
              reason: str, warnings: list[str], ai_notes: dict | None = None, events: list[dict] | None = None) -> dict:
    if events:
        return _make_semantic_plan(source, source_info, scenes, events, options, music, music_analysis, reason, warnings, ai_notes)
    duration = min(float(options["duration"]), float(source_info["duration"]))
    if float(source_info["duration"]) + 0.05 < float(options["duration"]):
        warnings.append("The source is shorter than the requested edit; the output duration was capped to the source.")
    usable = [dict(s) for s in scenes if not s.get("excluded") and float(s.get("duration") or 0) >= 0.65]
    if not usable:
        raise RuntimeError("No usable scenes were found in this video.")
    max_motion = max(float(s.get("motion_intensity") or 0) for s in usable) or 1.0
    max_sharp = max(float(s.get("sharpness") or 0) for s in usable) or 1.0
    for scene in usable:
        scene["selection_score"] = _scene_score(scene, options["atmosphere"], max_motion, max_sharp)
    ai_map = _validated_ai_recommendations(ai_notes, usable)
    if ai_notes and ai_notes.get("status") == "available" and not ai_map:
        warnings.append("AI scene recommendations were invalid or referred to unknown scenes; local selection was used.")
    for scene in usable:
        rec = ai_map.get(str(scene.get("id")))
        scene["ai_score"] = round(float(rec["score"]), 3) if rec else None
        scene["ai_rank"] = rec["rank"] if rec else None
        scene["combined_selection_score"] = round(scene["selection_score"] * 0.4 + (float(rec["score"]) / 100.0) * 0.6, 6) if rec else scene["selection_score"] * 0.4
    usable.sort(key=lambda s: (float(s["combined_selection_score"]), float(s["selection_score"])), reverse=True)
    target_len = INTENSITY[options["effects_intensity"]]
    selected = []
    required_count = max(1, int(math.ceil(duration / target_len)))
    transition = 0.3 if options["smooth_transitions"] and len(usable) > 1 else 0.0
    for scene in usable:
        if any(not (scene["end"] <= x["start"] or scene["start"] >= x["end"]) for x in selected):
            continue
        selected.append(scene)
        estimated = sum(min(float(x["duration"]), target_len + transition) for x in selected) - transition * max(0, len(selected) - 1)
        if len(selected) >= required_count and estimated >= duration:
            break
    if not selected:
        selected = [usable[0]]
    if transition and sum(float(x["duration"]) for x in selected) - transition * (len(selected) - 1) < duration - 0.05:
        transition = 0.0
        warnings.append("Smooth transitions were disabled because their overlap would exceed the available source material.")
    if ai_map and sum(1 for s in selected if s.get("ai_rank") is not None) >= 2:
        selected.sort(key=lambda s: (s.get("ai_rank") if s.get("ai_rank") is not None else 9999, s["start"]))
    else:
        selected.sort(key=lambda s: s["start"])
    sync_profile = _music_sync_profile(music_analysis, float(options["music_start_offset"]), duration, options["effects_intensity"])
    sync_enabled = bool(options["beat_sync"] and music_analysis and sync_profile.get("markers"))
    if options["beat_sync"] and not sync_enabled:
        sync_profile["mode"] = "natural-fallback"
        warnings.append("Music rhythm confidence was too low for beat cuts; natural scene pacing was used.")
    boundaries, references = _choose_boundaries(duration, len(selected), sync_profile if sync_enabled else {"markers": []})
    segments = []
    previous_source_out = 0.0
    previous_scene_start = -1.0
    for index, scene in enumerate(selected):
        speed = 1.0
        if options["speed_ramp"] and options["effects_intensity"] == "strong":
            speed = 1.08 if index % 2 else 0.94
        visible_start, visible_end = boundaries[index], boundaries[index + 1]
        planned_output = (visible_end - visible_start) + (transition if index < len(selected) - 1 else 0.0)
        # A beat can fall slightly inside a detected scene.  Keep the scene
        # identity for selection, but let the source window end at the exact
        # planned boundary; this is what makes a hard cut frame-accurate.
        source_in = float(scene["start"])
        if index and float(scene["start"]) >= previous_scene_start:
            source_in = max(source_in, previous_source_out)
        source_out = min(float(scene["end"]), float(source_info["duration"]), source_in + planned_output * speed)
        output_len = (source_out - source_in) / speed
        if output_len + 0.08 < planned_output:
            warnings.append(f"Source material ended before segment {index + 1}; the edit is shorter than requested.")
        if not segments:
            timeline_in = 0.0
        else:
            previous_end = float(segments[-1]["timeline_out"])
            expected_in = max(0.0, previous_end - transition)
            timeline_in = visible_start if abs(expected_in - visible_start) <= 0.08 else expected_in
        timeline_out = timeline_in + output_len
        reference = references[index] if index > 0 and sync_enabled else None
        crop_center_x = float(scene.get("reframe_center_x") or 0.5)
        crop_center_y = float(scene.get("reframe_center_y") or 0.5)
        segments.append({"source_id": scene["id"], "source_in": round(source_in, 3), "source_out": round(source_out, 3),
                        "timeline_in": round(timeline_in, 3), "timeline_out": round(timeline_out, 3), "playback_speed": speed,
                        "crop": {"mode": "saliency_heuristic" if options["smart_reframe"] else "center",
                                 "smart_reframe": options["smart_reframe"], "reframe_limit": "No face/character detector; local visual-saliency center only",
                                 "center_x": round(crop_center_x, 4), "center_y": round(crop_center_y, 4),
                                 "zoom": 1.05 if options["dynamic_zoom"] else 1.0},
                        "transition": {"type": "crossfade" if transition and index else "hard_cut", "duration": transition if index else 0.0},
                        "effects": {"color_grading": options["color_grading"], "intensity": options["effects_intensity"],
                                    "dynamic_zoom": "time-varying-scale" if options["dynamic_zoom"] else "off",
                                    "speed_ramp": "per-segment-speed-variation" if options["speed_ramp"] else "off"},
                        "music_beat_reference": reference.get("music") if reference else None,
                        "sync_point": round(timeline_in, 3) if reference else None,
                        "sync_point_type": "crossfade_start" if reference and transition else "hard_cut" if reference else "natural",
                        "sync_error_frames": round((timeline_in - float(reference["timeline"])) * 30, 3) if reference else None,
                        "ai_score": scene.get("ai_score"),
                        "selection_reason": f"local={scene['selection_score']}; ai={scene.get('ai_score')}; motion={scene['motion_intensity']}; sharpness={scene['sharpness']}"})
        previous_source_out, previous_scene_start = source_out, float(scene["start"])
    rendered_duration = sum(float(seg["timeline_out"]) - float(seg["timeline_in"]) for seg in segments) - transition * max(0, len(segments) - 1)
    if rendered_duration < duration - 0.5:
        warnings.append("The source did not contain enough distinct usable scenes; the edit is shorter than requested.")
    total = round(min(rendered_duration, duration), 3)
    return {"planner_version": PLANNER_VERSION, "source_name": source.name, "source_duration": round(source_info["duration"], 3),
            "track_id": (music or {}).get("track_id", ""), "music_offset": round(float(options["music_start_offset"]), 3),
            "track_title": (music or {}).get("title", "Original Audio" if options["music_source"] == "original_audio" else ""),
            "music_source": options["music_source"],
            "music_duration": round(float((music_analysis or {}).get("duration") or 0), 3), "atmosphere": options["atmosphere"],
            "intensity": options["effects_intensity"], "requested_duration": options["duration"], "total_duration": total,
            "music_segment": {"start": round(float(options["music_start_offset"]), 3), "end": round(float(options["music_start_offset"]) + total, 3)} if music else None,
            "transition_overlap": transition, "selection_reason": reason, "warnings": warnings, "segments": segments,
            "beat_sync": bool(options["beat_sync"]), "sync_mode": sync_profile.get("mode", "none") if options["beat_sync"] else "off",
            "rhythm_confidence": sync_profile.get("confidence", 0.0),
            "crossfade_sync_definition": "beat-aligned at crossfade start; visual blend spans the following overlap" if transition else "not applicable",
            "ai_scene_selector": bool(options.get("ai_scene_selector")),
            "ai_recommendations_applied": bool(ai_map)}


def validate_plan(plan: dict, source_duration: float) -> list[str]:
    if str(plan.get("planner_version", "")).endswith("-2"):
        errors, rendered, previous_out = [], 0.0, 0.0
        segments = plan.get("segments") if isinstance(plan, dict) else None
        if not isinstance(segments, list) or not segments:
            return ["The edit plan has no segments."]
        for i, seg in enumerate(segments):
            try:
                a, b = float(seg["source_in"]), float(seg["source_out"])
                ti, to = float(seg["timeline_in"]), float(seg["timeline_out"])
                speed = float(seg.get("playback_speed") or 1.0)
                trans = float((seg.get("transition") or {}).get("duration") or 0.0) if i else 0.0
            except (KeyError, TypeError, ValueError):
                errors.append(f"Segment {i + 1} has invalid time values.")
                continue
            if not (0 <= a < b <= source_duration + 0.05) or speed <= 0:
                errors.append(f"Segment {i + 1} leaves the source boundaries.")
            approved_start = seg.get("approved_source_start")
            approved_end = seg.get("approved_source_end")
            if approved_start is not None and approved_end is not None:
                try:
                    if a < float(approved_start) - 0.02 or b > float(approved_end) + 0.02:
                        errors.append(f"Segment {i + 1} leaves its approved shot/event boundary.")
                except (TypeError, ValueError):
                    errors.append(f"Segment {i + 1} has invalid approved shot/event boundaries.")
            if to <= ti or ti < -0.01:
                errors.append(f"Segment {i + 1} has an invalid timeline range.")
            if i and ti > previous_out + 0.08:
                errors.append(f"Segment {i + 1} leaves a timeline gap.")
            if i and abs((previous_out - ti) - trans) > 0.08:
                errors.append(f"Segment {i + 1} transition overlap does not match its timeline.")
            span = to - ti
            if abs((b - a) / speed - span) > 0.12:
                errors.append(f"Segment {i + 1} source speed does not match its timeline span.")
            rendered += span - trans
            previous_out = to
        if abs(float(plan.get("total_duration") or 0) - rendered) > 0.08:
            errors.append("Plan duration does not match its rendered timeline after transition overlap.")
        return errors
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


def _audio_samples(path: Path, start: float = 0.0, duration: float | None = None) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{max(0.0, duration):.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    raw, err = proc.communicate(timeout=180)
    if proc.returncode:
        raise RuntimeError(f"Audio verification failed: {err.decode('utf-8', 'replace')[-800:]}")
    return np.frombuffer(raw, dtype=np.float32)


def verify_rendered_music(output: Path, music_path: Path, music_start: float, duration: float) -> dict:
    """Confirm that the selected track's decoded spectrum is present in the result."""
    selected = _audio_samples(music_path, music_start, duration)
    rendered = _audio_samples(output, 0.0, duration)
    sample_count = min(len(selected), len(rendered))
    if sample_count < 8000:
        return {"status": "not_enough_audio", "selected_sha256": _sha256(music_path), "matched_fraction": 0.0}
    selected, rendered = selected[:sample_count], rendered[:sample_count]
    selected_rms = float(np.sqrt(np.mean(selected * selected) + 1e-12))
    if selected_rms < 1e-5:
        return {"status": "selected_track_is_silent", "selected_sha256": _sha256(music_path), "matched_fraction": 0.0}
    chunk = 4000
    similarities = []
    window = np.hanning(chunk).astype(np.float32)
    for start in range(0, sample_count - chunk + 1, chunk):
        a, b = selected[start:start + chunk] * window, rendered[start:start + chunk] * window
        fa, fb = np.abs(np.fft.rfft(a))[1:], np.abs(np.fft.rfft(b))[1:]
        denom = float(np.linalg.norm(fa) * np.linalg.norm(fb))
        if denom > 1e-8:
            similarities.append(float(np.dot(fa, fb) / denom))
    mean_similarity = float(np.mean(similarities)) if similarities else 0.0
    matched_fraction = float(np.mean(np.asarray(similarities) >= 0.35)) if similarities else 0.0
    return {"status": "present" if mean_similarity >= 0.35 and matched_fraction >= 0.5 else "not_confirmed",
            "selected_sha256": _sha256(music_path), "mean_spectral_similarity": round(mean_similarity, 4),
            "matched_fraction": round(matched_fraction, 4), "checked_seconds": round(sample_count / 8000, 3)}


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
        # A finite input plus explicit silence padding avoids silently looping
        # a short track.  The plan owns the effective output duration.
        cmd += ["-ss", f"{float(plan.get('music_offset') or 0):.3f}", "-i", str(music_path)]
    filters, video_labels, audio_labels = [], [], []
    grading = {"dramatic": (1.08, 0.94, -0.015), "dark_cinematic": (1.12, 0.82, -0.05), "emotional": (1.03, 0.92, 0.01),
               "epic": (1.10, 1.10, 0.0), "natural_film": (1.02, 1.0, 0.0), "phonk": (1.15, 1.16, -0.01)}[options["atmosphere"]]
    for i, seg in enumerate(segments):
        speed = float(seg.get("playback_speed") or 1.0)
        zoom = float((seg.get("crop") or {}).get("zoom") or 1.0)
        clip_duration = max(0.25, float(seg["timeline_out"]) - float(seg["timeline_in"]))
        if zoom > 1:
            if options["dynamic_zoom"]:
                growth = f"(1+0.025*sin(2*PI*t/{clip_duration:.5f}))"
                scale = f"scale=w='1080*{zoom:.4f}*{growth}':h='1920*{zoom:.4f}*{growth}':eval=frame:force_original_aspect_ratio=increase:flags=lanczos"
            else:
                scale = f"scale=1080*{zoom:.4f}:1920*{zoom:.4f}:force_original_aspect_ratio=increase:flags=lanczos"
        else:
            scale = "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos"
        crop_meta = seg.get("crop") or {}
        center_x = float(crop_meta.get("center_x") or 0.5)
        center_y = float(crop_meta.get("center_y") or 0.5)
        if options["dynamic_zoom"]:
            crop = f"crop=1080:1920:(in_w-out_w)*{center_x:.4f}+(in_w-out_w)*0.04*sin(t*0.7):(in_h-out_h)*{center_y:.4f}+(in_h-out_h)*0.03*cos(t*0.5)"
        else:
            crop = f"crop=1080:1920:(in_w-out_w)*{center_x:.4f}:(in_h-out_h)*{center_y:.4f}"
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
        trans = float((segments[i].get("transition") or {}).get("duration") or 0.0) if str(plan.get("planner_version", "")).endswith("-2") else min(0.3 if options["smooth_transitions"] else 0.0, accumulated / 3, length / 3)
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
        audio_accumulated = audio_labels[0][1]
        for i in range(1, len(audio_labels)):
            label, length = audio_labels[i]
            trans = float((segments[i].get("transition") or {}).get("duration") or 0.0) if str(plan.get("planner_version", "")).endswith("-2") else min(0.3 if options["smooth_transitions"] else 0.0, audio_accumulated / 3, length / 3)
            out = f"ax{i}"
            if trans:
                filters.append(f"[{audio}][{label}]acrossfade=d={trans:.3f}:c1=tri:c2=tri[{out}]")
            else:
                filters.append(f"[{audio}][{label}]concat=n=2:v=0:a=1[{out}]")
            audio = out
            audio_accumulated = audio_accumulated + length - trans
        if music_path:
            filters.append(f"[{music_index}:a]asetpts=PTS-STARTPTS,apad=pad_dur={accumulated:.3f},atrim=duration={accumulated:.3f},afade=t=in:st=0:d=0.35,afade=t=out:st={max(0.0, accumulated - 0.5):.3f}:d=0.5[music]")
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
        filters.append(f"[{music_index}:a]asetpts=PTS-STARTPTS,apad=pad_dur={accumulated:.3f},atrim=duration={accumulated:.3f},afade=t=in:st=0:d=0.35,afade=t=out:st={max(0.0, accumulated - 0.5):.3f}:d=0.5,loudnorm=I=-14:TP=-1.0:print_format=none[aout]")
        audio = "aout"
    cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]"]
    if audio:
        cmd += ["-map", f"[{audio}]"]
    else:
        cmd += ["-an"]
    output.parent.mkdir(parents=True, exist_ok=True)
    planned_duration = float(plan.get("total_duration") or accumulated)
    cmd += ["-t", f"{planned_duration:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-r", "30", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    cmd += ["-movflags", "+faststart", str(output)]
    began = time.monotonic()
    _ffmpeg_cancel(cmd, cancel=cancel)
    if music_path:
        selected_duration = max(0.0, float(validate_music(music_path)["duration"]) - float(plan.get("music_offset") or 0))
        # A deliberately short explicit track may be followed by silence, but
        # it must never be looped.  Verify the exact selected bytes for the
        # portion that can actually be present in the output.
        music_verification = verify_rendered_music(output, music_path, float(plan.get("music_offset") or 0),
                                                   min(planned_duration, selected_duration))
    else:
        music_verification = None
    if music_path and music_verification.get("status") != "present":
        output.unlink(missing_ok=True)
        raise RuntimeError("The selected music could not be confirmed in the rendered audio; the edit was not published as ready.")
    return {"render_seconds": round(time.monotonic() - began, 3), "duration": round(planned_duration, 3), "width": 1080, "height": 1920,
            "fps": 30, "audio": bool(audio), "music_verification": music_verification,
            "cpu_ram_vram": "NOT AVAILABLE: runtime metrics are not recorded"}


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
    selected = dict(job.get("selected_music") or {})
    selected.pop("path", None)
    out["selected_music"] = selected
    out["effective_options"] = job.get("effective_options") or job.get("options") or {}
    out["music_status"] = "required" if job.get("status") == "music_required" else "selected" if selected.get("title") else "original_audio" if (job.get("options") or {}).get("music_source") == "original_audio" else "pending"
    out["files"] = {}
    for name, public_name in (("result.mp4", "video"), ("edit-plan.json", "plan"), ("music-analysis.json", "music_analysis"),
                              ("scene-analysis.json", "scenes"), ("event-analysis.json", "events"), ("ai-scene-selection.json", "ai_scene_selection"),
                              ("license.json", "license"), ("metrics.json", "metrics")):
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
    music, reason, warnings = choose_music(opts, opts["duration"])
    selected = {}
    music_required = False
    if music:
        verify_music_decode(Path(music["path"]))
    elif opts["music_source"] == "auto":
        music_required = True
        warnings.append("Music is required for Auto. Choose a confirmed library track or upload music; Original Audio requires an explicit choice.")
    jid = "cin-" + uuid.uuid4().hex[:14]
    root = config.user_dir(uid) / "cinematic" / jid
    root.mkdir(parents=True, exist_ok=False)
    dest = root / ("source" + source_path.suffix.lower())
    shutil.move(str(source_path), dest)
    if music:
        selected = _snapshot_music(root, music)
    elif opts["music_source"] == "original_audio":
        selected = {"track_id": "", "title": "Original Audio", "source": "original_audio"}
    effective = _effective_options(opts, music)
    job = {"id": jid, "user": uid, "dir": str(root), "created": _now(), "kind": "cinematic_edit", "status": "music_required" if music_required else "queued",
           "stage": "Music required" if music_required else "Waiting", "progress": 0.0, "error": "", "options": opts, "effective_options": effective,
           "source": {"name": _clean_name(filename, "source"), "duration": round(info["duration"], 3)}, "source_path": str(dest),
           "warnings": warnings, "selection_reason": reason, "selected_music": selected, "repeat": False,
           "music_required": music_required}
    with _lock:
        _jobs[jid] = job
    _save_job(job)
    if not music_required:
        _queue.put(jid)
    return public_job(job)


def cancel_job(uid: str, jid: str) -> dict:
    job = get_job(uid, jid)
    if not job:
        raise FileNotFoundError("Cinematic edit not found.")
    if job.get("status") in ("done", "error", "cancelled", "music_required"):
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


def _ai_scene_notes(job: dict, events: list[dict], options: dict, cancel=None) -> dict:
    primary_model = options.get("ai_model") or "google/gemini-2.5-flash-lite"
    if not options.get("ai_scene_selector"):
        return {"status": "not_requested", "model": "", "calls": 0, "estimated_cost_usd": None,
                "semantic": False}
    candidates = _event_candidates(events, 8)
    if not candidates:
        return {"status": "fallback_local", "model": primary_model, "calls": 0,
                "estimated_cost_usd": None, "semantic": False, "error": "No grouped events for AI review."}
    if cancel and cancel():
        raise Cancelled()
    rows, images = [], []
    job_dir = Path(job["dir"]).resolve()
    for event in candidates:
        frame_labels = []
        for index, rel_value in enumerate(event.get("representative_frames") or []):
            rel = Path(str(rel_value))
            frame = (job_dir / rel).resolve()
            if job_dir not in frame.parents or not frame.is_file() or frame.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                continue
            try:
                raw = frame.read_bytes()
            except OSError:
                continue
            if 0 < len(raw) <= 512 * 1024:
                frame_labels.append(f"{event['id']}:frame-{index + 1}")
                images.append((raw, "image/jpeg" if frame.suffix.lower() in (".jpg", ".jpeg") else "image/png"))
        rows.append({"event_id": event["id"], "start": event["start"], "end": event["end"], "duration": event["duration"],
                     "shot_count": event["shot_count"], "dialogue": event.get("dialogue", ""),
                     "focus_mode": event.get("focus_mode"), "frame_labels": frame_labels})
    if any(not row["frame_labels"] for row in rows):
        return {"status": "fallback_local", "model": primary_model, "calls": 0,
                "estimated_cost_usd": None, "semantic": False, "error": "Grouped-event frames were not available for the bounded AI request."}
    focus = options.get("edit_focus") or "auto"
    focus_text = options.get("edit_focus_text") or ""
    prompt = ("Create one coherent cinematic montage from the bounded event batches below. Do not invent content, "
              "do not use local paths, and do not select an event unless its attached frames support the decision. "
              "Return an object with concept, recommendations (event_id, score 0-100, phase hook/build_up/climax/resolution, "
              "reason), and structure. Prefer character/event continuity over motion or sharpness. The attached images are "
              "in the frame_labels order in each row. Atmosphere=" + options["atmosphere"] + "; Edit Focus=" + focus +
              (f" ({focus_text})" if focus_text else "") + ". Events:\n" + json.dumps(rows, ensure_ascii=False))
    schema = {"type": "object", "properties": {"concept": {"type": "string"},
                                                   "recommendations": {"type": "array", "items": {"type": "object", "properties": {
                                                       "event_id": {"type": "string"}, "score": {"type": "number"},
                                                       "phase": {"type": "string"}, "reason": {"type": "string"}}}},
                                                   "structure": {"type": "array"}}}
    try:
        from . import brain
        audit = {}
        answer, model, spent = brain.ask_ai(prompt, schema, purpose="Cinematic semantic montage", timeout=45, cheap=True,
                                            images=images, audit=audit, model_override=primary_model)
        if isinstance(answer, list):
            answer = {"concept": "", "recommendations": answer, "structure": []}
        recs = _validated_event_recommendations({"status": "available", "recommendations": (answer or {}).get("recommendations") if isinstance(answer, dict) else []}, events)
        if not recs:
            return {"status": "fallback_local", "model": model, "calls": 1, "tokens": spent,
                    "estimated_cost_usd": None, "semantic": False, "error": "The AI response did not contain valid event IDs and scores."}
        prices = getattr(brain, "VIEWER_TOKEN_PRICES", {})
        def cost(audit_data, model_name):
            rate = prices.get(model_name)
            return (audit_data.get("input_tokens", 0) * rate[0] + audit_data.get("output_tokens", 0) * rate[1]) if rate else 0.0
        estimated = cost(audit, model)
        concept = str((answer or {}).get("concept") or "").strip()[:300] if isinstance(answer, dict) else ""
        final_model = str(os.getenv("PEAKCUT_CINEMATIC_FINAL_MODEL") or "").strip()
        final_error = ""
        if final_model and final_model != model:
            if cancel and cancel():
                raise Cancelled()
            final_audit = {}
            try:
                final_prompt = prompt + "\nPrimary review (use only as a hint; re-check the attached frames):\n" + json.dumps(answer, ensure_ascii=False)
                final_answer, final_used, final_spent = brain.ask_ai(final_prompt, schema, purpose="Cinematic final semantic plan",
                                                                     timeout=45, cheap=False, images=images, audit=final_audit,
                                                                     model_override=final_model)
                final_answer = final_answer if isinstance(final_answer, dict) else {"recommendations": final_answer}
                final_recs = _validated_event_recommendations({"status": "available", "recommendations": final_answer.get("recommendations")}, events)
                if final_recs:
                    answer, recs, model, spent, concept = final_answer, final_recs, final_used, spent + final_spent, str(final_answer.get("concept") or concept)[:300]
                    estimated += cost(final_audit, final_used)
                else:
                    final_error = "Final semantic model returned no valid event IDs; primary review was retained."
            except Exception as e:  # noqa: BLE001 - primary review remains usable
                final_error = f"Final semantic model unavailable: {str(e)[:180]}"
        result = {"status": "available", "model": model, "calls": 2 if final_model and not final_error else 1, "tokens": spent,
                  "input_tokens": audit.get("input_tokens", 0) + (final_audit.get("input_tokens", 0) if 'final_audit' in locals() else 0),
                  "output_tokens": audit.get("output_tokens", 0) + (final_audit.get("output_tokens", 0) if 'final_audit' in locals() else 0),
                  "estimated_cost_usd": round(estimated, 8),
                  "cost_note": "Estimated text-token cost; provider billing and image-token charges are not returned.",
                  "candidate_event_ids": [x["id"] for x in candidates], "recommendations": (answer or {}).get("recommendations", []),
                  "structure": (answer or {}).get("structure", []), "concept": concept, "semantic": True}
        if final_error:
            result["final_model_note"] = final_error
        return result
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        return {"status": "fallback_local", "model": primary_model, "calls": 1, "error": str(e)[:240],
                "estimated_cost_usd": None, "semantic": False}


def _snapshot_music(root: Path, music: dict) -> dict:
    """Keep the exact selected bytes beside the plan so repeat render is deterministic."""
    source = Path(str(music.get("path") or "")).resolve()
    verify_music_decode(source)
    destination = root / f"music-source{source.suffix.lower()}"
    shutil.copy2(source, destination)
    out = {k: v for k, v in music.items() if k not in ("path", "analysis")}
    out["path"] = str(destination)
    out["sha256"] = _sha256(destination)
    out["duration"] = round(float((music.get("analysis") or {}).get("duration") or music.get("duration") or 0), 3)
    return out


def _effective_options(options: dict, music: dict | None) -> dict:
    out = normalize_options(options)
    if music:
        out["music_track_id"] = str(music.get("track_id") or "")
        out["music_upload_id"] = ""
        out["music_source"] = "upload" if music.get("track_id") == "upload" else "library"
        out["music_title"] = str(music.get("title") or "Selected music")[:200]
    elif out["music_source"] == "original_audio":
        out["music_title"] = "Original Audio"
    return out


def _run_job(job: dict):
    uid = job["user"]
    source = Path(job["source_path"])
    options = normalize_options(job.get("effective_options") or job.get("options"))
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
            if music_path:
                verify_music_decode(music_path, cancel=cancelled)
            elif options["music_source"] != "original_audio":
                raise MusicRequired("Music is required for Auto, Library or Upload. The saved job has no selected music.")
            metrics = render_plan(source, music_path, plan, options, _job_file(job, "result.mp4"), cancel=cancelled)
        else:
            _update(job, status="running", stage="Detecting scenes", progress=0.1)
            scenes = analyze_scenes(source, Path(job["dir"]), cancel=cancelled)
            dialogue = _read_existing_dialogue(job)
            events = analyze_events(scenes, Path(job["dir"]), dialogue=dialogue, cancel=cancelled)
            ai_notes = _ai_scene_notes(job, events, options, cancel=cancelled)
            _json_write(_job_file(job, "ai-scene-selection.json"), ai_notes)
            _update(job, stage="Selecting music", progress=0.28)
            selected = job.get("selected_music") or {}
            music = None
            reason = job.get("selection_reason") or ""
            warnings = list(job.get("warnings") or [])
            if options["music_source"] != "original_audio":
                music_path = Path(str(selected.get("path") or ""))
                if not selected or not music_path.is_file():
                    raise MusicRequired("Music is required for Auto, Library or Upload. Choose a confirmed local track or upload music.")
                decoded = verify_music_decode(music_path, cancel=cancelled)
                music = {**selected, "path": str(music_path), "analysis": None, "sha256": decoded["sha256"]}
            music_analysis = (music or {}).get("analysis")
            if music and not music_analysis:
                music_analysis = analyze_audio(Path(music["path"]))
                music["analysis"] = music_analysis
            if music_analysis:
                _json_write(_job_file(job, "music-analysis.json"), {"track_id": music.get("track_id"), **music_analysis})
            else:
                _json_write(_job_file(job, "music-analysis.json"), {"source": "original_audio", "reason": reason, "analysis": "NOT APPLICABLE"})
            _update(job, stage="Building edit plan", progress=0.45, warnings=warnings, selection_reason=reason,
                    selected_music={k: v for k, v in (music or selected or {}).items() if k != "analysis"})
            plan = make_plan(source, source_info, scenes, options, music, music_analysis, reason, warnings, ai_notes=ai_notes, events=events)
            if music:
                selected_with_segment = dict(music)
                selected_with_segment["segment"] = plan.get("music_segment")
                _update(job, selected_music={k: v for k, v in selected_with_segment.items() if k != "analysis"})
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
    except MusicRequired as e:
        _update(job, status="music_required", stage="Music required", progress=0.0, error=str(e), repeat=False)
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
