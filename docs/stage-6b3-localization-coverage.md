# Stage 6B.3 — localization coverage

## Scope

The signed-in application keeps one frontend and one translation helper in
`web/index.html`. Russian strings are keyed in `RU`, English remains the
fallback, and parameterized text uses `tr()`/`trn()` plus the Russian
`ruPlural()` rules. The selected interface locale is stored in
`peakcut_ui_locale` and is reused by the authentication, reset and admin
pages.

## Operational UI coverage

| Section | RU coverage | Evidence |
| --- | --- | --- |
| Home / overview | Complete | shell, upload composer, live rail, campaigns, activity, AI card, next steps, streamer table and post queue use translation keys |
| All Videos / Upload | Complete | source link, upload, options, captions, processing stages, clip selection and errors |
| Live / Streamers | Complete | live cards, discovery, recording controls, frequency, statuses, dialogs and plural counts |
| Campaigns | Complete | search, campaign setup, rules, logo placement, posting and start dialogs |
| Schedule / Posting | Complete | filters, calendar labels, statuses, dialogs and locale-aware dates |
| Styles / Templates | Complete | style list, preview, template editor, camera placement, controls and tooltips |
| AI / Usage | Complete | provider card, key flow, usage charts, statuses and usage table |
| Cinematic Edit / Music Library | Complete | planner labels, music library, license fields, job states, quality-gate messages and errors |
| Settings / Connected Apps / Storage / Updates | Complete | settings navigation, locale persistence, apps, speech models, storage and update panels |
| Admin | Complete | overview, users, feedback, activity, dialogs, empty states and charts |
| Authentication / reset | Complete in the available assets | RU/EN helper, locale persistence, labels, placeholders and known auth errors |

## Justified allowlist

These strings are intentionally not translated because they are not interface
labels or are not served in the current runtime configuration:

- provider names, model IDs, platform names, API enum values and file formats;
- ASR language values such as `English`, `Arabic` and `Russian`, which remain
  independent from the interface locale;
- user content: creator, campaign, video, stream, music and channel names,
  titles, descriptions, hashtags and imported metadata;
- embedded Twitch player controls and remote platform content;
- AI-generated cinematic concepts and backend evidence identifiers, except for
  known UI status prefixes;
- `landing.html` marketing/legal prose and `legal.html` policy prose. With the
  current `PEAKCUT_ACCOUNTS=0` runtime, `/` returns `303 /app`, so these public
  pages are not part of the active signed-in interface. They remain a separate
  public-copy localization project rather than silently receiving a partial
  UI translation.

## Regression and browser evidence

`tests/test_ui_i18n.py` checks literal translation keys, required section
coverage, Russian English-fallback calls, plural rules, locale persistence and
the separation from ASR/caption contracts. Final checks also include JavaScript
parsing for `web/index.html`, `web/auth.html`, `web/reset.html` and
`web/admin.html`.

Desktop browser QA was run against `127.0.0.1:8765` in RU and EN for Home,
All Videos, Campaigns, Schedule, Styles, Cinematic Edit, Settings, Connected
Apps, Storage, Updates and Admin. At the tested 967 px viewport, each checked
route had `documentElement.scrollWidth === clientWidth` and no browser console
errors. RU Home, EN Settings, RU Admin and the RU options menu screenshots were
captured in the QA session.

The available in-app browser exposes no viewport-resize/mobile emulation API;
mobile viewport QA is therefore `NOT RUN`, not a claimed PASS. File selection
was not exercised because no user file was authorized for upload; the file
input and upload controls were inspected without sending a file.
