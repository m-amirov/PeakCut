"""Stage 6B.2 UI locale persistence, fallback and runtime asset provenance."""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="peakcut-ui-locale-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studio import accounts, ctx, server  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
fails = []


def check(label, condition):
    print("PASS" if condition else "FAIL", label)
    if not condition:
        fails.append(label)


with ctx.as_user("u_local"):
    check("new local installs default to Russian", accounts.settings()["ui_locale"] == "ru")
    accounts.save_settings({"ui_locale": "en"})
    check("English locale persists to settings", accounts.settings()["ui_locale"] == "en")
    accounts._settings_mem.pop("u_local", None)
    check("English locale survives a fresh settings read", accounts.settings()["ui_locale"] == "en")
    accounts.save_settings({"ui_locale": "ru"})
    try:
        accounts.save_settings({"ui_locale": "de"})
    except ValueError:
        invalid_rejected = True
    else:
        invalid_rejected = False
    check("unsupported locale values are rejected", invalid_rejected)

required_controls = [
    "ce_edit_mode", "ce_focus", "ce_discovery_model", "ce_director_model", "ce_audio_mode",
    "ce_caption_mode", "ce_caption_style", "ce_caption_language", "ce_caption_preview", "ce_hard_cut",
]
for control in required_controls:
    check(f"Cinematic control is present: {control}", f'id="{control}"' in HTML)
check("translation helper has an English fallback", "UI_LOCALE === \"ru\"" in HTML and "fallback = key" in HTML)
check("interface locale is separate from media language contracts", "ui_locale" in HTML and "ce_caption_language" in HTML and "o_lang" in HTML)
check("Cinematic API enum values remain stable", 'value="cinematic_story"' in HTML and 'value="original_audio"' in HTML and 'value="google/gemini-2.5-flash-lite"' in HTML)
check("stale Auto to Original Audio wording is absent", "Auto → Original Audio" not in HTML)

response = server._page("index.html")
digest = hashlib.sha256((ROOT / "web" / "index.html").read_bytes()).hexdigest()
check("runtime page points at the repository web directory", server.WEB.resolve() == (ROOT / "web").resolve())
check("runtime page exposes exact UI SHA", response.headers.get("x-peakcut-ui-sha256") == digest)
check("runtime app page is not stored in browser caches", response.headers.get("cache-control") == "private, no-store")

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
