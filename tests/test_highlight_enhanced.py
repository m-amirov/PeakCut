"""Stage 5B policy: legacy defaults and Enhanced options stay explicit and persistent."""
import os
import shutil
import sys
import tempfile

import numpy as np

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-enhanced-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")

from studio import auth, creators, ctx, db, jobs  # noqa: E402
from studio import brain  # noqa: E402

db.init()
fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    if not ok:
        fails.append(name)


legacy = jobs.selection_policy({})
check("legacy jobs retain Balanced Fast", legacy["highlight_profile"] == "balanced" and legacy["analysis_quality"] == "fast", str(legacy))

enhanced = jobs.clean_options({"highlight_profile": "meme_comedy", "analysis_quality": "enhanced",
                               "viewer_model": "google/gemini-2.5-flash", "min_viewer_score": 75,
                               "visual_analysis": True})
check("Enhanced options are retained", enhanced == {"highlight_profile": "meme_comedy", "analysis_quality": "enhanced",
                                                     "viewer_model": "google/gemini-2.5-flash", "min_viewer_score": 75,
                                                     "visual_analysis": True}, str(enhanced))

u, _ = auth._create("enhanced@example.com", "Enhanced", password_hash="x")
with ctx.as_user(u.id):
    creator = creators.create({"name": "Comedy creator", "highlight_profile": "meme_comedy", "analysis_quality": "enhanced",
                               "viewer_model": "google/gemini-2.5-flash", "min_viewer_score": 75, "visual_analysis": True})
    saved = creators.get(creator["id"])
    check("creator policy survives reload", all(saved.get(k) == v for k, v in {
        "highlight_profile": "meme_comedy", "analysis_quality": "enhanced", "viewer_model": "google/gemini-2.5-flash",
        "min_viewer_score": 75, "visual_analysis": True}.items()), str(saved))
    job = jobs.create({"type": "url", "url": "https://example.com/video", "name": "video"}, {}, enqueue=False, creator=creator["id"])
    check("creator policy is inherited by new jobs", all(job["options"].get(k) == v for k, v in {
        "highlight_profile": "meme_comedy", "analysis_quality": "enhanced", "viewer_model": "google/gemini-2.5-flash",
        "min_viewer_score": 75, "visual_analysis": True}.items()), str(job["options"]))


words = [{"w": "This", "s": 0.0, "e": 0.2}, {"w": "is", "s": 1.0, "e": 1.2}, {"w": "a", "s": 2.0, "e": 2.2},
         {"w": "ridiculous", "s": 3.0, "e": 3.3}, {"w": "punchline!", "s": 4.0, "e": 4.4}, {"w": "Haha!", "s": 5.0, "e": 5.3}]
calls = []


def fake_ask(prompt, schema=None, purpose="", **kwargs):
    calls.append((prompt, purpose, kwargs))
    if purpose.startswith("Video:"):
        return ([{"start": 0, "end": 6, "score": 91, "category": "funny", "hook": "A joke", "title": "Joke",
                  "description": "A joke lands.", "hashtags": [], "highlight_words": [], "reason": "Punchline."},
                 {"start": 0.1, "end": 6, "score": 85, "category": "funny", "hook": "Same joke", "title": "Same",
                  "description": "Duplicate.", "hashtags": [], "highlight_words": [], "reason": "Duplicate."}], "discovery", 10)
    return ({"humor_score": 90, "surprise_score": 80, "payoff_score": 90, "shareability_score": 90,
             "clarity_score": 90, "retention_score": 90, "setup_timestamp": 0, "payoff_timestamp": 5,
             "memorable_quote": "punchline", "humor_type": "absurdity", "reason": "The payoff lands.",
             "rejection_reason": "", "confidence": 90}, "viewer", 12)


real_ask = brain.ask_ai
brain.ask_ai = fake_ask
try:
    picked, diag = brain.find_clips(words, np.zeros(8), 8, max_clips=1, min_len=5, max_len=10,
                                    selection={"highlight_profile": "meme_comedy", "analysis_quality": "enhanced",
                                               "viewer_model": "google/gemini-2.5-flash", "min_viewer_score": 75})
finally:
    brain.ask_ai = real_ask
check("Enhanced judges one deduplicated candidate and gates selection", len(picked) == 1 and picked[0].get("original_ai_score") == 91
      and picked[0].get("viewer", {}).get("viewer_score", 0) >= 75 and len([x for x in calls if x[1] == "Viewer judge"]) == 1
      and diag.get("viewer_calls") == 1,
      str({"picked": picked, "calls": calls, "diag": diag}))
check("Meme discovery prompt names comedy signals", any("absurd" in x[0].lower() and "punchline" in x[0].lower() for x in calls), str(calls))

print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
