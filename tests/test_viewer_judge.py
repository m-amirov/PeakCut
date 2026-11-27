"""The experimental viewer judge has a stable, local score and cannot affect clip selection."""
import os
import sys

sys.path.insert(0, os.getcwd())
from studio import brain  # noqa: E402

fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(name)


words = [{"w": "setup", "s": 10.0, "e": 10.4}, {"w": "punchline!", "s": 14.0, "e": 14.8},
         {"w": "laughs", "s": 15.0, "e": 15.4}]
seen = {}


def fake(prompt, schema=None, purpose="", **kwargs):
    seen.update(prompt=prompt, schema=schema, purpose=purpose, kwargs=kwargs)
    return ({"humor_score": 100, "surprise_score": 50, "payoff_score": 80, "shareability_score": 60,
             "clarity_score": 90, "retention_score": 70, "setup_timestamp": 10, "payoff_timestamp": 15,
             "memorable_quote": "punchline", "humor_type": "wordplay", "reason": "payoff is clear",
             "rejection_reason": "", "confidence": 88}, "fake-viewer", 123)


brain.ask_ai = fake
out = brain.viewer_judge(words, 10, 16, context="ordinary context", signals={"loudness": {"max": 2}},
                         profile="meme_comedy", images=[(b"jpeg", "image/jpeg")], frame_timestamps=[10, 15])
check("weighted score is calculated locally", out["viewer_score"] == 77.0, str(out))
check("all viewer components and provenance survive", out["profile"] == "meme_comedy" and out["model"] == "fake-viewer"
      and out["tokens"] == 123 and out["confidence"] == 88)
check("meme profile is explicit and original score cannot bias the judge", "Profile: meme_comedy" in seen["prompt"]
      and "original score" not in seen["prompt"].lower())
check("structured schema and frames use the existing AI client", seen["schema"] is brain.VIEWER_SCHEMA
      and seen["kwargs"]["images"] == [(b"jpeg", "image/jpeg")] and "10.0s, 15.0s" in seen["prompt"]
      and seen["purpose"] == "Research: viewer judge")
try:
    brain.viewer_judge(words, 10, 16, profile="gaming")
    check("unknown profile is rejected", False)
except ValueError:
    check("unknown profile is rejected", True)

print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
