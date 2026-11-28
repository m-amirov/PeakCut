"""Small, dependency-free mechanical contracts for cinematic edits."""
import math
from pathlib import Path


def stream_duration(stream):
    if not stream:
        return None
    try:
        duration = float(stream.get("duration"))
    except (ValueError, TypeError):
        try:
            numerator, denominator = stream["time_base"].split("/")
            duration = float(stream["duration_ts"]) * float(numerator) / float(denominator)
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            return None
    return duration if math.isfinite(duration) and duration > 0 else None


def duration_matches(actual, target, tolerance):
    return actual is not None and math.isfinite(actual) and abs(actual - target) <= tolerance


def ass_dialogue_count(path):
    if not path or not Path(path).is_file():
        return 0
    return sum(1 for line in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines()
               if line.lstrip().startswith("Dialogue:") and line.split(",", 9)[-1].strip())


def merge_speech_windows(windows, limit, gap=0.35, padding=0.08):
    """Keep natural intra-phrase gaps, but do not extend beyond the shot."""
    out = []
    for start, end in sorted(windows):
        start, end = max(0.0, start - padding), min(limit, end + padding)
        if end <= start:
            continue
        if out and start <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def safe_face_center(boxes, width, height, output_width=1080, output_height=1920):
    """Conservative static crop for every sampled face box, not identity tracking.

    Return normalized source coordinates, or None when containment is not
    established. No movement or extra zoom may be added after this check.
    """
    if not boxes or width <= 0 or height <= 0:
        return None
    aspect = output_width / output_height
    visible_w = min(1.0, aspect * height / width)
    visible_h = min(1.0, width / (aspect * height))
    rects = []
    for box in boxes:
        try:
            x, y, w, h = (float(box[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(v) for v in (x, y, w, h)) or min(x, y) < 0 or min(w, h) <= 0 or x+w > 1.001 or y+h > 1.001:
            return None
        # Headroom and a small rounding reserve; fall back rather than cut it.
        mx, my = max(0.012, w * 0.22), max(0.012, h * 0.28)
        rects.append((max(0.0, x-mx), max(0.0, y-my), min(1.0, x+w+mx), min(1.0, y+h+my)))
    left, top = min(r[0] for r in rects), min(r[1] for r in rects)
    right, bottom = max(r[2] for r in rects), max(r[3] for r in rects)
    def origin(lo, hi, span):
        # Reserve a little extra space against scaling/chroma rounding.
        lower, upper = max(0.0, hi - span), min(lo, 1.0 - span)
        if lower > upper + 1e-9:
            return None
        return max(lower, min(upper, (lo+hi-span)/2))
    x, y = origin(left, right, visible_w), origin(top, bottom, visible_h)
    if x is None or y is None:
        return None
    return (x + visible_w/2, y + visible_h/2)


def crop_filter(center_x, center_y, width=1080, height=1920):
    """FFmpeg normalized source centre -> clamped crop origin."""
    return (f"crop={width}:{height}:x='clip(in_w*{center_x:.8f}-out_w/2,0,in_w-out_w)'"
            f":y='clip(in_h*{center_y:.8f}-out_h/2,0,in_h-out_h)'")


# All branches entering concat/acrossfade use identical PCM geometry and a
# sample-count clock. In particular, a 22050 Hz source and a 44100 Hz silent
# segment must not keep incompatible timestamps through FFmpeg negotiation.
AUDIO_RATE = 48000


def canonical_audio():
    return "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,asetpts=N/SR/TB"


def fit_audio_segment(duration):
    samples = max(1, round(float(duration) * AUDIO_RATE))
    # Silence is intentional outside retained speech; fit individual segments
    # before concat, not the finished mix. Never mask a truncated music track.
    return f"apad=whole_len={samples},atrim=end_sample={samples},asetpts=N/SR/TB"


def normalized_audio():
    # loudnorm's dynamic mode works at 192 kHz. Explicitly establish its
    # input sample rate and clock after concat/amix/acrossfade: implicit
    # negotiation can otherwise consume samples at the wrong rate.
    return ("aresample=192000,aformat=sample_fmts=dbl:sample_rates=192000:channel_layouts=stereo,"
            "asettb=1/192000,asetpts=N,loudnorm=I=-14:TP=-1.0:print_format=none,"
            + canonical_audio() + ",asettb=1/48000,asetpts=N")
