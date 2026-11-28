"""Stage 6B.2 UI locale persistence, fallback and runtime asset provenance."""
import hashlib
import os
import re
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

# The dictionary alone is not coverage evidence: every literal key used by the
# app must exist, and the Russian path must not silently render its English
# fallback for the normal user-facing controls.
ru_start = HTML.index("const RU = {")
ru_end = HTML.index("};", ru_start) + 2
assign_start = HTML.index("Object.assign(RU, {", ru_end)
assign_end = HTML.index("});", assign_start) + 3
dictionary_text = HTML[ru_start:ru_end] + HTML[assign_start:assign_end]
translation_keys = set(re.findall(r'"([^"\\]+)"\s*:', dictionary_text))
literal_calls = re.findall(r'\btr\(\s*"([^"\\]+)"', HTML)
missing_keys = sorted(set(literal_calls) - translation_keys)
check("all literal tr() keys exist in the Russian dictionary", not missing_keys)
if missing_keys:
    print("  missing:", ", ".join(missing_keys))

required_sections = {
    "home": ["home.hero_title", "nav.home"],
    "library": ["nav.videos", "library.no_videos"],
    "upload": ["add.make_clips", "add.video_link"],
    "live": ["live.find_streamers", "live.start_clipping"],
    "campaigns": ["nav.campaigns", "campaign.setup_title"],
    "schedule": ["nav.schedule", "schedule.add_to_schedule"],
    "styles": ["nav.styles", "styles.new_style"],
    "ai": ["nav.ai", "ai.get_key"],
    "cinematic": ["nav.cinematic", "cinematic.create"],
    "settings": ["nav.settings", "settings.language"],
    "auth": ["Create your account", "Sign in to PeakCut"],
}
for section, keys in required_sections.items():
    if section == "auth":
        auth_text = (ROOT / "web" / "auth.html").read_text(encoding="utf-8")
        check("authentication screen has RU/EN locale helper", 'localStorage.getItem("peakcut_ui_locale")' in auth_text and 'const tr = (en, ru)' in auth_text)
        continue
    check(f"{section} has translated section keys", all(k in translation_keys for k in keys))

fallback_allowlist = {
    "common.seconds", "common.video", "common.open", "common.cancel",
    # Provider/platform names and the Content ID product label are intentionally
    # preserved; they are not interface-language prose.
    "campaign.content_rewards", "cinematic.aura_phonk", "cinematic.content_id",
    "cinematic.phonk_value", "cinematic.placeholder_platforms",
}
same_fallbacks = []
for key, fallback in re.findall(r'\btr\(\s*"([^"\\]+)"\s*,\s*"([^"\\]*)"', HTML):
    if key in translation_keys and not any("\u0400" <= ch <= "\u04ff" for ch in fallback):
        # Extract the last declared value for this key; duplicate declarations
        # are allowed for compatibility with the Stage 6B.2 dictionary merge.
        values = re.findall(r'"' + re.escape(key) + r'"\s*:\s*"([^"]*)"', dictionary_text)
        if values and values[-1] == fallback and key not in fallback_allowlist:
            same_fallbacks.append(key)
check("Russian UI calls do not use English fallbacks", not same_fallbacks)
if same_fallbacks:
    print("  English fallbacks:", ", ".join(sorted(set(same_fallbacks))))
check("Russian plural helper covers 1/2-4/11-14 forms", "function ruPlural" in HTML and "x >= 11 && x <= 14" in HTML and "y >= 2 && y <= 4" in HTML)

for asset in ("auth.html", "reset.html", "admin.html"):
    asset_text = (ROOT / "web" / asset).read_text(encoding="utf-8")
    check(f"{asset} keeps the selected interface locale", 'localStorage.getItem("peakcut_ui_locale")' in asset_text)

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
