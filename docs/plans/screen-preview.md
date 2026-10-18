# Live screen preview (`/preview`)

## Context
Developers and household members need to see what the 800×480 e-ink panel shows, live, in a browser. The preview shows the HTML layout and the exact 1-bit PNG side by side. It updates when the data or the code changes, and has "what if" controls that never affect the real panel.

Confirmed decisions:

| # | Decision |
|---|---|
| Content | Side by side, stacked on narrow screens. Left: the live HTML (`/display` in an iframe). Right: the 1-bit PNG |
| Where | `/preview`, in the app, local and on the deployed server. Linked **from Settings only** |
| Live triggers | Data changes (menu, recipes, calendar sync) and code changes (`display.html`, CSS, `.py` under `uvicorn --reload`) |
| Freshness | HTML side refreshes as soon as the version changes. PNG side re-renders **only when the version changed**, at most once per 5 s per view. The panel's 60 s cache/ETag is untouched |
| Transport | Poll `GET /preview/version?<params>` every 2 s (plain text) |
| Controls (all in the URL query, preview only) | Pretend date/time `at`; scenario `scenario`; forced warnings `warn` (repeatable); offline stamp `offline=HH:MM` |
| Scenarios | `quiet`, `busy`, `long-names`, `nothing`, `trip`, `month-boundary`; default `real` = live DB data |
| Out-of-range pretend date | Allowed, with a note "Calendar data only covers <from>–<to>" (real data only) |
| Panel look | Off-white "paper" background + bezel around the PNG; zoom `1×`, `2×`, `1:1 physical` |
| 1:1 physical | One-time calibration: drag a slider until an on-screen credit card (85.6 mm wide) matches a real card; px-per-mm stored in `localStorage["hcs.pxPerMm"]`; panel active area 163.2 × 97.92 mm |
| Offline stamp | One canonical implementation shared by the server and the Pi client |

Code facts (read this session):
- `server/app/display/data.py`: `build_display(session, now, uptime=None) -> DisplayModel(header, month_grid, agenda, dinners, warning)`. Private helpers: `_span(CalendarEvent, tz) -> _Event(title, first, last, start)`, `_agenda(events, today)`, `_month_grid(today, events)`, `_dinners(session, today)` (Saturday→"Takeaway" default, `DinnerRow(label, title, is_today)`, `Dinners(range_text, rows)`), `_warning(session, now, uptime)` (joins parts with " · ").
- `server/app/display/routes.py`: `GET /display` renders `display.html` with `m=build_display(session, clock.now())`. `GET /display/frame.png` uses `render.get_frame()`.
- `server/app/display/render.py`: `_screenshot()` hard-codes `{base_url_internal}/display`; `to_one_bit`, `etag_for`; `_lock`, `_cache`, `MAX_AGE_S=60`, `heavy_lock`.
- `server/app/calendars/sync.py`: `sync_window(today) -> (first_of_month-7d, today+62d)`.
- `display-client/frame.py`: `stamp(img, since)` (lines ~161–180, DejaVu Bold `FONT_PATH`/`FONT_SIZE`, black box bottom-left). `install.sh` installs `frame.py` into `/opt/home-calendar-frame/`.
- `server/app/templates/settings/index.html:58`: "Preview: /display · /display/frame.png" line.
- Docker build context is `server/`, so it cannot see `display-client/`.

## Approach

### 1. Split display building into inputs → model (no behaviour change)
In `server/app/display/data.py`:
- Add `@dataclass DisplayInputs(events: list[_Event], menu: dict[date, str | None], warning: str | None)`. `menu` maps each of the 7 week dates to the dinner title, `"Takeaway"`, or `None`.
- `load_inputs(session, now, uptime=None) -> DisplayInputs`: today's DB logic. `_span` for events; the `_dinners` row lookup produces the `menu` dict, including the Saturday default; `_warning`.
- `compose(inputs: DisplayInputs, now: datetime) -> DisplayModel`: header, grid, agenda, dinners built from `inputs.menu`, warning.
- `build_display(session, now, uptime=None)` becomes `compose(load_inputs(session, now, uptime), now)`. The existing tests in `tests/test_display_data.py` must stay green unchanged.

### 2. Scenarios
New `server/app/display/scenarios.py`:
- `SCENARIOS: dict[str, Callable[[datetime], DisplayInputs]]`, keys exactly `quiet`, `busy`, `long-names`, `nothing`, `trip`, `month-boundary`. Each is built relative to the given `now` (`today = now.date()`) and uses no DB.
  - `quiet`: 1 event today at 18:00, 2 events in the next 10 days, dinners Sun–Fri, Sat default Takeaway.
  - `busy`: 15 timed events today (08:00…, 30-min steps), 3 events tomorrow, full week of dinners.
  - `long-names`: 4 events with ~80-char titles; dinners with ~60-char titles; one all-day event.
  - `nothing`: no events, all dinners `None` except Saturday `"Takeaway"`.
  - `trip`: a 5-day all-day "Family trip to Cornwall" that started yesterday, plus 2 timed events.
  - `month-boundary`: ignores the date part of `now` and uses the last Thursday of `now`'s month at 08:00. The week spans two months. Events on the last and first days.
- `SCENARIO_LABELS`: `real` "Real data", `quiet` "Quiet week", `busy` "Busy day", `long-names` "Long names", `nothing` "Nothing planned", `trip` "Trip under way", `month-boundary` "Month boundary".
- `scenario_now(name, now) -> datetime`: returns `now` for every scenario except `month-boundary`, which returns the adjusted time.

### 3. Preview parameters shared by `/display`, the PNG and the version
New `server/app/display/preview.py`:
- `@dataclass(frozen=True) PreviewParams(at: datetime | None, scenario: str, warn: tuple[str, ...], offline: time | None)`.
- `parse_params(query) -> PreviewParams` handles each query parameter:

  | Parameter | Format | If invalid or empty |
  |---|---|---|
  | `at` | `YYYY-MM-DDTHH:MM`, interpreted in `clock.tz()` | ignored |
  | `scenario` | a `SCENARIOS` key or `real` | `real` |
  | `warn` | repeatable; values limited to `no-calendars`, `calendar-failing`, `backup` | unknown values dropped |
  | `offline` | `HH:MM` | ignored |

- `PreviewParams.query() -> str`: the canonical query string, with keys sorted and empty values omitted. It is used for URLs and cache keys.
- `FORCED_WARNINGS` gives the sample text for each `warn` value: "No calendars set up", "Calendar 'Family' not updating since 07:15", and "Backup not run since <fmt_day(today-3)>".
- `preview_model(session, params) -> tuple[DisplayModel, str | None]` returns the model and an optional range note:
  1. Set `now = params.at or clock.now()`.
  2. Get inputs: `load_inputs` for `real`, otherwise `SCENARIOS[name](now)`, with `now = scenario_now(...)`.
  3. If `params.warn` is set, replace `inputs.warning` with the forced texts joined by " · ".
  4. Return `compose(inputs, now)`.
  5. The note is "Calendar data only covers <fmt_day(from)>–<fmt_day(to)>" only when `scenario == "real"`, `params.at` is set, and `params.at.date()` lies outside `sync_window(clock.today())`.
- `GET /display` now accepts the same query and renders `preview_model(...)`. With no params, the result equals today's output, so the panel path is unchanged.
- `version(session, params) -> str` is the sha256 (first 16 hex chars) of:
  - `repr(preview_model(...)[0])`
  - `params.query()`
  - the mtimes of `templates/display.html` and `static/app.css`
  - `_PROCESS_ID`, a uuid4 hex created at import, which changes on every `uvicorn --reload` restart.

### 4. PNG for the preview (own cache, 5 s throttle)
In `server/app/display/render.py`:
- Change `_screenshot()` to `_screenshot(path: str = "/display")`; `get_frame` keeps calling it with the default.
- Add `get_preview_frame(params: PreviewParams, ver: str, *, now=time.monotonic, shoot=_screenshot) -> bytes`. It keeps a module dict `_preview_cache` of `query → {"ver", "png", "at"}`, capped at 8 entries; evict the oldest `at`. Rules:
  - The cached `ver` equals `ver` → return the cached PNG.
  - The cached entry exists and `now() - at < 5` → return the cached PNG, even if stale.
  - Otherwise, shoot `/display?<query>`, run `to_one_bit`, and apply the offline stamp from step 5 if `params.offline` is set. The stamp time is that time today in the preview's `now`.
  - If `heavy_lock.locked()` and a cached entry exists → return the cached PNG.
  - Never touch `_cache` or the panel ETag.
- Guard with its own `asyncio.Lock` (`_preview_lock`).

### 5. One shared offline stamp
- Move `stamp(img, since)` and its font constants from `display-client/frame.py` into the new `server/app/display/stamp.py`. It must use only the standard library and Pillow, with no `app.` imports.
- `display-client/stamp.py` is a **relative symlink** to `../server/app/display/stamp.py`, created with `ln -s ../server/app/display/stamp.py display-client/stamp.py`.
- In `frame.py`, replace the function with `from stamp import stamp`, resolved from the script directory (the same `sys.path` insert it already uses for `waveshare_epd`, done at import of the stamp).
- In `install.sh`, add `install -m 644 "$here/stamp.py" "$opt/stamp.py"`. `install` copies the symlink target's contents.
- Existing `display-client/test_frame.py` stamp tests must still pass.

### 6. Routes and page
New `server/app/display/preview_routes.py`, with `router` included in `server/app/main.py`'s router tuple:
- `GET /preview`: renders `templates/preview.html` with:
  - `params`
  - `query`
  - `labels`: `SCENARIO_LABELS`
  - `note`
  - `version`
  - `default_at`: the current local time as `YYYY-MM-DDTHH:MM`
- `GET /preview/version`: `PlainTextResponse(version(...))`, `Cache-Control: no-store`.
- `GET /preview/frame.png`: `image/png`, `Cache-Control: no-store`. It accepts an extra ignored `v` param for cache busting. A render exception returns 503 "render failed".

`templates/preview.html` extends `base.html`:
- **Control form** (`method=get`, auto-submits on change with `onchange="this.form.submit()"`):
  - a datetime-local input for `at`, plus a "Now" link that clears it
  - a scenario select
  - three warning checkboxes
  - an offline time input
  - a zoom radio: 1× / 2× / 1:1. Zoom is client-only; store it in `localStorage["hcs.zoom"]`.
- **Layout:** two panes in a CSS grid that wraps to one column under 1700 px.
  - **HTML pane:** `<iframe src="/display?{{query}}" width=800 height=480 scrolling=no>` with a 1 px border.
  - **PNG pane:** `.panel` with a dark-grey 24 px bezel, `background:#f4f1ea`, containing `<img src="/preview/frame.png?{{query}}&v={{version}}" width=800 height=480 style="image-rendering: pixelated">`.
  - Zoom sets the img and iframe CSS width to `800px`, `1600px`, or `163.2mm × pxPerMm` (height scales proportionally).
- **Note:** shown if present.
- **Calibration** (`<details>`): an 85.6 × 53.98 mm card outline whose width is `85.6 × pxPerMm`, and a range slider for 2–12 px/mm, default `96/25.4`, saved to `localStorage`.
- **Inline script, ~30 lines:** every 2 s, `fetch('/preview/version?'+query)`. If the text differs from the last version:
  - reload the iframe (`iframe.contentWindow.location.reload()`);
  - set the img `src` to `/preview/frame.png?{query}&v={new}` after 5 s, then again after 6 s, because the server throttles to 5 s and the second attempt picks up the render.
  - Use `fetch` with `cache:'no-store'`. On fetch errors, keep polling silently.
- **Settings:** replace line 58 of `settings/index.html` with `<p><a href="/preview">Screen preview</a> · <a href="/display/frame.png">current panel image</a></p>`.

## Critical files & anchors
- `server/app/display/data.py`: the `load_inputs`/`compose` split must keep `build_display` output identical.
- `server/app/display/render.py`: the preview cache must never touch the panel `_cache`; check the 5 s throttle.
- `display-client/stamp.py` (symlink) and `install.sh`: the Pi installs a real file copy.

## Verification
TDD at these seams, one test → code at a time:
- **`tests/test_display_data.py` (existing):** unchanged and green after step 1.
- **`tests/test_preview.py`:**
  - `parse_params`: valid values, invalid `at`/`scenario`/`warn`/`offline` fall back as specified; `query()` is canonical (sorted, empties dropped).
  - `preview_model` with `at=2026-10-17T09:00` (a Saturday) on real data: the header reads "SATURDAY 17"; the Saturday dinner row is today and shows "Takeaway".
  - Scenario `busy`: the agenda has 11 lines, the 11th is `+6 more today`. Scenario `nothing`: the first agenda line is `Today · nothing planned`. Scenario `trip`: the line contains `(until `. Scenario `month-boundary`: `dinners.range_text` spans two month names.
  - `warn=["backup","no-calendars"]` → the warning text joins both, in the order given by `FORCED_WARNINGS`.
  - The note appears for `at` 90 days ahead with `real`, and not for a scenario.
  - `version` changes when a menu entry is set, when `params` change, and when `display.html`'s mtime changes (touch a copy via monkeypatched path); it is stable otherwise.
- **`tests/test_preview_render.py`** (`get_preview_frame` with an injected `shoot` returning a fixed 800×480 PNG and an injected clock):
  - the first call shoots;
  - same `ver` → no shoot;
  - new `ver` at +2 s → no shoot, returns cached; at +5 s → shoots;
  - `offline=07:30` → the bottom-left pixel is black;
  - the panel `_cache` is untouched.
- **`display-client/test_frame.py`:** existing stamp/offline tests pass with the symlinked module.
- **Route smoke** (TestClient): `/preview` 200 contains the iframe and img; `/preview/version` returns 16 hex chars; `/display?scenario=busy` contains `+6 more today`.
- **End-to-end:**
  1. Locally run `DATA_DIR=... .venv/bin/uvicorn app.main:app --reload --port 8000`, then open `http://localhost:8000/preview` in a browser (Playwright).
  2. Change a dinner in another tab. Within about 7 s the PNG `src` version changes.
  3. Edit `display.html` (add a visible marker), confirm the iframe shows it within about 3 s, then revert.
  4. Pick `busy` and confirm both panes show `+6 more today`.

Full suite once at the end.
