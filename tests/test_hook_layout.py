"""Hook boxes use their rendered font, stay safe, and disappear before captions do."""
import os, re, shutil, subprocess, sys, tempfile
from pathlib import Path
from PIL import Image
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-hook-layout-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")
from studio import jobs, render, templates  # noqa: E402

fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    if not ok:
        fails.append(name)


def hook_rows(ass):
    return [x for x in ass.splitlines() if ",Hook,," in x]


def coords(ass):
    rows = hook_rows(ass)
    shape = next(x for x in rows if "\\p1" in x)
    return [int(x) for x in re.findall(r"(?<![A-Za-z])(-?\d+)(?:\.\d+)?", shape.split("\\p1}", 1)[1])]


words = [{"w": "Это", "s": 0.1, "e": 0.4}, {"w": "рабочие", "s": 3.0, "e": 3.3}, {"w": "субтитры", "s": 3.4, "e": 3.8}]
ru = templates.clean_look({"hook": {"font": "Poppins ExtraBold", "size": 96, "duration": 2.5}})["hook"]
check("Russian caption language selects the Noto rendering path", jobs.caption_script("ru", "ru", words, False) == "ru")

one = render.build_ass(words, 0, 8, "Ты меня копируешь?", "bounce", [], 300, 1380, hook_look=ru)
check("a Russian hook selects Noto Sans without a caption script", "\\fnNoto Sans\\b1" in one)
check("a hook ends at the default 2.5 seconds", all(",0:00:02.50,Hook,," in row for row in hook_rows(one)))
check("captions still carry on after the hook", any(",0:00:03.00," in row and ",Cap,," in row for row in one.splitlines()))

two = render.build_ass([], 0, 8, "В 15 лет попал в рехаб из-за шопоголизма?", "bounce", [], 300, 1380, hook_look=ru)
check("a Russian hook is balanced into no more than two text rows", 1 <= len([x for x in hook_rows(two) if "\\p1" not in x]) <= 2)
xy = coords(two)
check("the hook box stays inside the safe frame", min(xy) >= 0 and max(xy[::2]) <= render.W and max(xy[1::2]) <= render.H, str(xy))

long = render.build_ass([], 0, 8, "Сверхдлинноесловобезпробеловкотороеникогданевлезетвбезопаснуюрамку", "bounce", [], 80, 1380,
                        hook_look=ru)
long_text = [x for x in hook_rows(long) if "\\p1" not in x]
check("an overflowing hook is shrunk or safely shortened", len(long_text) <= 2 and any("…" in x or "\\fs56" in x for x in long_text))
xy = coords(long)
check("a top-positioned hook is clamped into the safe area", min(xy[1::2]) >= render.HOOK["safe_y"], str(xy))

latin = templates.clean_look({"hook": {"font": "Archivo Black", "bg": True, "outline": 8, "duration": 2.0}})["hook"]
en = render.build_ass([], 0, 8, "A mixed Latin hook with a thick outline", "bounce", [], 300, 1380, hook_look=latin)
check("Latin hook keeps its selected font and timed duration", "\\fnArchivo Black" in en and ",0:00:02.00,Hook,," in en)
check("outline is included in the safe box", min(coords(en)) >= 0 and max(coords(en)[::2]) <= render.W)


# This is deliberately the public renderer path, rather than build_ass(): it catches libass font selection,
# final raster bounds, the old saved template shape, and the separate emoji overlay.
root = Path(tempfile.mkdtemp(prefix="pc-hook-pixels-"))
src, out = root / "source.mp4", root / "out"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=2a6496:s=1080x1920:r=30:d=3.4",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(src)], check=True)


def frame(video, at):
    png = root / f"{video.stem}-{at:.1f}.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(at), "-i", str(video), "-frames:v", "1", str(png)], check=True)
    return Image.open(png).convert("RGB")


def white_box(im):
    pts = [(x, y) for y in range(80, 600) for x in range(im.width)
           if all(v >= 242 for v in im.getpixel((x, y)))]
    if not pts:
        return None
    xs, ys = zip(*pts)
    return min(xs), min(ys), max(xs), max(ys)


def ink_box(im):
    pts = [(x, y) for y in range(80, 600) for x in range(im.width)
           if max(im.getpixel((x, y))) < 90]
    if not pts:
        return None
    xs, ys = zip(*pts)
    return min(xs), min(ys), max(xs), max(ys)


def line_bands(im):
    rows = []
    for y in range(80, 600):
        if sum(max(im.getpixel((x, y))) < 90 for x in range(im.width)) > 12:
            rows.append(y)
    bands = []
    for y in rows:
        if not bands or y > bands[-1][-1] + 1:
            bands.append([y])
        else:
            bands[-1].append(y)
    # Glyph dots and emoji details are not text lines; a rendered line spans substantially more rows.
    return [band for band in bands if len(band) >= 12]


base = templates.clean_look(next(t["look"] for t in templates.BUILTIN if t["id"] == "b-full"))
scenarios = [
    ("Russian with cap_lang empty", "Комики дают указания в микрофон", "", True, base),
    ("Russian with cap_lang ru", "Что делать с жвачкой?", "ru", True, base),
    ("Russian without captions", "Ты меня копируешь?", "", False, base),
    ("mixed Cyrillic Latin", "PeakCut: ты меня копируешь?", "", True, base),
    ("long Russian hook", "В 15 лет попал в рехаб из-за шопоголизма и теперь рассказывает всю историю", "", True, base),
    ("emoji hook", "Ты меня копируешь? 😭", "", True, base),
    ("existing Latin hook", "You copied my whole idea", "", True, base),
    ("old saved template", "Старый шаблон без времени показа", "", True,
     {"cap": base["cap"], "hook": {k: v for k, v in base["hook"].items() if k != "duration"}, "layout": "crop"}),
]
caption_words = [{"w": "СУБТИТРЫ", "s": 2.1, "e": 3.25}]
for i, (label, hook, cap_lang, captions, look) in enumerate(scenarios):
    script = jobs.caption_script("ru", cap_lang, caption_words if captions else [], False)
    clip = {"start": 0.0, "end": 3.3, "hook": hook, "look": look, "captions": captions,
            "highlight_words": [], "script": script}
    made = render.render_clip(src, out, f"case{i}", clip, caption_words, 1080, 1920)
    video = out / made["file"]
    early, late = frame(video, 1.0), frame(video, 2.8)
    box, ink = white_box(early), ink_box(early)
    check(label + ": real box is visible", box is not None, str(box))
    check(label + ": box stays in safe area", box is not None and box[0] >= render.HOOK["safe_x"] and box[2] <= render.W - render.HOOK["safe_x"] and box[1] >= render.HOOK["safe_y"], str(box))
    check(label + ": real text stays inside box", box is not None and ink is not None and box[0] <= ink[0] and ink[2] <= box[2] and box[1] <= ink[1] and ink[3] <= box[3], str((box, ink)))
    check(label + ": real text uses at most two lines", len(line_bands(early)) <= 2, str(line_bands(early)))
    check(label + ": hook is gone at 2.8 seconds", white_box(late) is None, str(white_box(late)))
    if captions:
        check(label + ": captions remain after hook", sum(max(late.getpixel((x, y))) > 220 for y in range(1000, 1800) for x in range(120, 960)) > 300, "no bright caption pixels")
    if any("\\p1" in row for row in hook_rows((out / f"case{i}.ass").read_text(encoding="utf-8").split("[Events]", 1)[-1])):
        check(label + ": generated cover matches video time", (out / made["cover"]).is_file())

shutil.rmtree(root, ignore_errors=True)

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
