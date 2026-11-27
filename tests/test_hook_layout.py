"""Hook boxes use visible font bounds, stay safe, and disappear before captions do."""
import os, re, sys, tempfile
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

one = render.build_ass(words, 0, 8, "Ты меня копируешь?", "bounce", [], 300, 1380, hook_look=ru, script="ru")
check("a one-line Russian hook uses Noto Sans", "\\fnNoto Sans" in one)
check("a hook ends at the default 2.5 seconds", all(",0:00:02.50,Hook,," in row for row in hook_rows(one)))
check("captions still carry on after the hook", any(",0:00:03.00," in row and ",Cap,," in row for row in one.splitlines()))

two = render.build_ass([], 0, 8, "В 15 лет попал в рехаб из-за шопоголизма?", "bounce", [], 300, 1380, hook_look=ru, script="ru")
check("a Russian hook is balanced into no more than two text rows", 1 <= len([x for x in hook_rows(two) if "\\p1" not in x]) <= 2)
xy = coords(two)
check("the hook box stays inside the safe frame", min(xy) >= 0 and max(xy[::2]) <= render.W and max(xy[1::2]) <= render.H, str(xy))

long = render.build_ass([], 0, 8, "Сверхдлинноесловобезпробеловкотороеникогданевлезетвбезопаснуюрамку", "bounce", [], 80, 1380,
                        hook_look=ru, script="ru")
long_text = [x for x in hook_rows(long) if "\\p1" not in x]
check("an overflowing hook is shrunk or safely shortened", len(long_text) <= 2 and any("…" in x or "\\fs56" in x for x in long_text))
xy = coords(long)
check("a top-positioned hook is clamped into the safe area", min(xy[1::2]) >= render.HOOK["safe_y"], str(xy))

latin = templates.clean_look({"hook": {"font": "Archivo Black", "bg": True, "outline": 8, "duration": 2.0}})["hook"]
en = render.build_ass([], 0, 8, "A mixed Latin hook with a thick outline", "bounce", [], 300, 1380, hook_look=latin)
check("Latin hook keeps its selected font and timed duration", "\\fnArchivo Black" in en and ",0:00:02.00,Hook,," in en)
check("outline is included in the safe box", min(coords(en)) >= 0 and max(coords(en)[::2]) <= render.W)

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
