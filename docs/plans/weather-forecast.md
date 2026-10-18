# Weather forecast beneath the calendar

## Status and context
**Implementation-ready specification; not authority to build or deploy.** The user confirmed the choices below after grilling. Add optional weather below the month grid on the existing 800×480 monochrome e-ink screen, configured through the phone-friendly web app. Both the HTML and 1-bit screen preview must reflect the same forecast without making upstream requests during rendering.

## Confirmed behavior
| Topic | Decision |
|---|---|
| Provider | WeatherAPI.com permanent Free plan, API key, three-day forecast |
| Location | Search and explicitly select one household location; no automatic geolocation |
| Activation | Disabled initially; selecting a location alone does not enable weather |
| Modes | `daily` (default), `2h`, `3h` |
| Daily | Three dates: today and the next two days in the selected location's timezone; high/low temperature; today remains until local midnight |
| Subdaily | Four hourly forecasts sampled at fixed local 2-/3-hour boundaries, starting at the next boundary; not whole-interval probabilities |
| Units | `celsius` default; `fahrenheit` selectable; unit in the heading, degree symbol per temperature |
| Probability | Rain chance normally; snow chance when condition is snowy; explicit Rain/Snow labels, never add percentages |
| Fetch | Immediately scheduled when enabled, then every 30 minutes; no upstream request for unit/mode change or a screen/preview render |
| Stale | Fresh below 1 hour since successful fetch; stale from 1 hour to below 6 hours; unavailable from 6 hours or when no valid forecast slots remain |
| Timezone | Weather uses location timezone. Calendar/menu keep app timezone. Show weather timezone when different |
| Layout | Weather below calendar, icons + temperatures + percentages + source credit; modest reduction of large date/header permitted, grid and right column unchanged |

## Grounded integration points
- `server/app/config.py`: frozen `Config` loaded from environment.
- `server/app/settings_store.py`: existing `AppSetting` key/value helpers; `put` does not commit.
- `server/app/models.py`: `AppSetting` already supports JSON encoded as text; no weather table exists. Use the existing store for this single location/cache; no schema migration needed.
- `server/app/main.py`: starts/cancels lifespan background tasks and includes module routers.
- `server/app/calendars/routes.py`: owns `/settings`; `_page` provides template context.
- `server/app/templates/settings/index.html`: existing phone-friendly sections and Screen preview link.
- `server/app/display/data.py`: `load_inputs` → `DisplayInputs` → `compose` → `DisplayModel`. Optional weather fields must have defaults so existing scenario constructors keep working.
- `server/app/templates/display.html`: left column width 280 px, inner width 250 px; five-week calendar currently ends around y=350, six-week around y=378. Only 102 px remains in the worst case before the 480 px boundary.
- `server/app/display/preview.py`: cheap version markers currently include display date but no hour. Weather selection/staleness must change its version at the appropriate boundaries.
- `server/app/display/scenarios.py`: `Scenario(label, build, now)` table creates `DisplayInputs` in memory.
- `deploy/compose.yaml`: environment must explicitly pass the new API key into the container; adding a key only to `.env` is insufficient.

## U1. Provider boundary and deterministic forecast rules
**New files:** `server/app/weather/__init__.py`, `types.py`, `client.py`, `forecast.py`; tests `server/tests/test_weather.py`, fixtures under `server/tests/fixtures/weather/`.

### API contract
Use `httpx`, already installed. Fixed HTTPS origin `https://api.weatherapi.com`; never accept a caller-supplied API base URL or fetch provider icon URLs.
- Search: `GET /v1/search.json`, params `key`, `q` (user search text).
- Forecast: `GET /v1/forecast.json`, params `key`, `q="<lat>,<lon>"`, `days=3`, `aqi=no`, `alerts=no`.
- Search results provide `name`, `region`, `country`, `lat`, `lon`. **They do not provide `tz_id`**. Timezone is resolved from forecast response `location.tz_id` on first enabled fetch; Settings says “Timezone resolved on first forecast” until then.
- Forecast daily fields: `forecast.forecastday[].date`, `.day.maxtemp_c`, `.mintemp_c`, `.condition.code`, `.daily_chance_of_rain`, `.daily_chance_of_snow`.
- Hourly fields: `.hour[].time_epoch`, `.temp_c`, `.condition.code`, `.is_day`, `.chance_of_rain`, `.chance_of_snow`.
- Use hourly epoch seconds as the identity/order, converted using validated `ZoneInfo(location.tz_id)`. Do not attach a timezone to the provider's naive `time` string or reuse one UTC offset across DST.
- Store Celsius once; convert to Fahrenheit with `C * 9 / 5 + 32` at presentation time. Round the final temperature to nearest whole degree, half away from zero; suppress negative zero. Clamp neither invalid temperatures nor invalid probabilities into plausible output: reject malformed required envelopes and treat null/invalid individual values as missing.
- Accept probability values as numbers or numeric strings in [0,100], display nearest whole percent. Missing/null/unparseable/out-of-range → `None` → `—`; preserve genuine 0. Condition code missing/unrecognised → unknown icon, not sunny.
- Timeout 15 seconds; bound response body to 2 MiB and disable redirects (fixed origin). Validate response object shape before replacing cache; an empty forecast list is a failed fetch, not a successful fresh snapshot. Missing individual fields do not discard otherwise useful forecast rows.
- Error messages are safe literals: `Weather API key is missing`, `Weather API key is invalid`, `Weather API quota exceeded`, `Weather location was not found`, `Weather service is unavailable`, `Weather response was invalid`. Map known provider auth/quota/location error codes, HTTP errors and timeouts without showing/logging full request URLs, raw bodies, key or `httpx` exception text. Redact the `key` query parameter from any application request logging; prevent httpx/httpcore INFO request logs from exposing keyed URLs in this app.

### Normalised cache payload
One snapshot contains `location_key` (canonical selected lat/lon pair), `timezone` (IANA id), `fetched_at` (UTC ISO), `days` and `hours`.
- Day record: `date`, `high_c`, `low_c`, `condition_code`, `rain_pct`, `snow_pct`.
- Hour record: `epoch`, `temp_c`, `condition_code`, `is_day`, `rain_pct`, `snow_pct`.
- Sort days by date and hours by epoch; deduplicate equal identities. Do not persist the raw API response or its request URL/key.

### Weather conditions and percentage choice
Use one explicit condition-code mapping for local icon category and precipitation kind. Use bundled inline SVG shapes with black stroke, no emoji, no external images, and an unknown question-mark icon.
- `1000`: clear (sun by day, crescent at night); `1003`: partly cloudy; `1006,1009`: cloud.
- `1030,1135,1147`: fog; additional haze/dust/smoke codes `1012,1015,1018,1021,1024,1027,1033,1036,1039,1042,1045,1048`: fog-style icon, rain probability (not silently clear).
- Drizzle/rain/freezing rain: `1063,1072,1150,1153,1168,1171,1180,1183,1186,1189,1192,1195,1198,1201,1240,1243,1246`: rain icon and rain percentage.
- Snow/sleet/ice: `1066,1069,1114,1117,1204,1207,1210,1213,1216,1219,1222,1225,1237,1249,1252,1255,1258,1261,1264`: snow icon and snow percentage.
- Thunder: `1087,1273,1276`: thunder icon/rain; `1279,1282`: thunder-with-snow icon/snow.
- Unknown: unknown icon/rain. Never combine rain and snow probabilities. Daily icons are day-style.

### Selection behavior
Implement selection as a pure function of saved preferences, a snapshot, effective display time and actual fetch age.
- Convert effective display time to location timezone; don't change calendar/menu time.
- Daily: fixed three dates starting that local day; if a date is absent, leave a missing-data cell for that date rather than shifting another date into it. High/low describe the full provider day, not its remaining hours.
- Subdaily: independently enumerate the next **four boundary instants**, strictly after effective time, with local minute 00 and hour divisible by 2 or 3. Generate candidates by walking UTC instants, converting to location timezone and applying the local-boundary predicate; keep chronological epoch order. This skips nonexistent spring-forward times and retains genuinely distinct eligible fall-back instants. At exactly 10:00, 2h mode starts at 12:00. Match snapshot records onto these fixed cells: at 09:20 with missing 12:00 data, show 10:00 / 12:00 — / 14:00 / 16:00, not 10/14/16/18. Include timezone abbreviation where duplicate clock labels would otherwise be ambiguous.
- For cross-midnight cells show a short day/day-number with time. Truncate long location names in the header, not temperatures or percentages.
- Daily use three cells and subdaily four. Missing trailing subdaily slots remain `—`; if all requested slots are absent, render `Weather unavailable`. Never invent days beyond the provider horizon.
- Stale age uses actual UTC now minus `fetched_at`, not a preview's pretend timestamp: at 3600 seconds add `Stale`; at 21600 seconds suppress forecast values and render unavailable. Future/invalid `fetched_at` is invalid cache. Coverage selection still uses preview effective time, so out-of-horizon pretend dates are unavailable even with a fresh cache.

**Tests (TDD):** official-shaped search/forecast fixtures; all-day/hour fields; invalid envelope; numeric strings/null/0/out-of-range probability; snow vs rain; unknown code; positive/negative/Fahrenheit rounding; 09:20→10/12/14/16 (2h), 09:20→12/15/18/21 (3h); exact-boundary behavior; next day labels; spring-forward/fall-back epoch ordering; three-day horizon, missing middle date, empty future slots; freshness at 3599/3600/21599/21600 seconds.

## U2. Settings, cache and background lifecycle
**Depends on U1. Files:** new `server/app/weather/service.py`, `routes.py`; existing `config.py`, `main.py`, `calendars/routes.py`, `settings_store.py`, `templates/settings/index.html`, `deploy/compose.yaml`. New tests `server/tests/test_weather_settings.py`, `test_weather_service.py`.

### Persisted settings and secret
- Add `Config.weather_api_key: str | None`, environment `WEATHER_API_KEY` (empty → None). Compose passes `WEATHER_API_KEY=${WEATHER_API_KEY:-}`. No key in forms or query links. Key rotation uses container recreation as other secrets do.
- `weather.config`: JSON text `{enabled: bool, mode: "daily"|"2h"|"3h", unit: "celsius"|"fahrenheit", location: null|{name,region,country,lat,lon}, fetch_generation: int}`. Generation starts at 0 and increments on every actual enabled-state or coordinate-identity transition, not on unit/mode changes.
- Missing config defaults to disabled/daily/celsius/no location. Use one parser/validator; corrupt saved state disables the weather section with a safe Settings error, never breaks the display.
- `weather.snapshot`: normalised snapshot from U1, or absent.
- `weather.status`: JSON text `{location_key: str|null, fetch_generation: int, last_attempt_at: UTC ISO|null, last_error: str|null}`. Fetch age is based on snapshot success time, independently of status/failed attempts.
- Use `settings_store.get/put`; write one complete JSON value per key. No new DB table, migration or third-party package. Config/status/cache mutations use a short SQLite write transaction begun with `BEGIN IMMEDIATE` **before** reading current config (fresh session/connection); read/validate/mutate/commit together. Never hold that transaction across network I/O. This serialises Settings saves and guarded fetch completions without new infrastructure.

### Settings controls and routes
Append a `Weather` section using a new `templates/weather/settings.html` partial; provide its context from existing Settings `_page` via `weather.service.settings_context(session)`.
- Show key configured/not configured, enabled switch, selected location label/coords, resolved timezone if available, daily/2h/3h select, °C/°F select, last successful fetch, safe last error, provider attribution and explanation that subdaily percentages refer to the sampled hour.
- `GET /settings/weather/search?q=`: read-only search results partial. Explicit Search submit, not per-keystroke calls. Blank or >100-character query → 422; missing key → 422 with setup message; no matches → `No matching locations`; upstream failure → 502 with safe message. Results render escaped name/region/country plus selection buttons.
- `POST /settings/weather/location`: accepts selected result fields (name, region, country, lat, lon); validate finite lat [-90,90], lon [-180,180] and length-bounded labels. Save exactly the explicitly selected location; coordinate identity uses a stable canonical numeric representation. Keep enabled/mode/unit. Atomically clear `weather.snapshot` and reset status when coordinate identity changes, even if disabled. No reuse of an old place's cache. Search does not implicitly select or enable.
- `POST /settings/weather`: accepts enabled/mode/unit. Invalid enums →422; enabling without location/key→422 with the form values retained and no persisted mutation. Missing key must not prevent disabling. Valid save→303 `/settings`.
- Location/timezone display is escaped; don't introduce a map or location tracker.

### Background worker
`run_weather_loop()` in service, registered and cancelled with other lifespan loops. Use asynchronous `httpx` network calls, with a short-lived DB session before/after the network operation, never pass an ORM Session to a worker thread. No `heavy_lock`: forecast calls aren't Chromium/Whisper work.
- Check schedule every 5 seconds. Enabled with location/key and no matching attempt status → due now (initial enable, new location, re-enable). An “immediate” fetch is queued for the next loop pass, within 5 seconds, not a blocking Settings request.
- Thereafter due at `last_attempt_at + 30 minutes`, including on failure. Restart preserves that schedule. No aggressive retry loop or call on every render.
- Mark attempt before sending, storing current location and generation. On completion use a fresh short write transaction, re-read config, and install response/error **only if** still enabled and both captured generation and location identity match. This also rejects disable→re-enable and A→B→A races, not just a simple location change. Never overwrite preferences saved during the call. Successful response stores snapshot and clears safe error atomically; failure stores only safe error and preserves same-place last-good snapshot.
- Changing only unit/mode doesn't clear cache, change generation or reset schedule. Disabling suppresses display immediately, increments generation and clears scheduling status. Disabled→enabled likewise increments generation and clears attempt/error status atomically with config, so it schedules a fetch now even if an earlier attempt was recent; retain the same-location snapshot only within age/coverage limits. Coordinate changes increment generation and clear snapshot/status atomically.
- Catch provider failures so they cannot terminate the loop; cancellation propagates normally.

**Tests:** defaults; search ambiguity and explicit select; no matches/key/invalid query; enabling prerequisites; disable with absent key; enums/coordinates; preserve typed values on 422; key never rendered; stale data for old place never appears; first enable/new location due within loop pass; half-hour schedule survives restart; failed fetch keeps last good; changing units/mode is network-free; disabled/new-location race discards in-flight result; safe errors do not contain a test API key. Use injected clock and provider boundary; no real API in permanent tests.

## U3. E-ink weather strip
**Depends on U1/U2. Files:** existing `server/app/display/data.py`, `server/app/templates/display.html`; new `server/app/templates/weather/strip.html`, `icons.html`; test `server/tests/test_weather_display.py`.

### Data flow
Add optional weather snapshot/preferences inputs to `DisplayInputs` (default None) and an optional presentation model `weather` to `DisplayModel` (default None). `load_inputs` reads persisted config/cache only. `compose` calls pure forecast selection. Pass actual UTC fetch-age time separately when composing a pretend-time preview so pretending six months ahead doesn't incorrectly change cache age; existing calls can default it to actual UTC now. Scenario fixtures supply their own deterministic fetched-at/actual-time pair.

Disabled → `weather=None`, no strip and no weather-related header class: existing screen output/layout unchanged. Enabled with prerequisites but no usable forecast → compact `Weather unavailable` strip, never an exception. Missing key is explained in Settings, not printed on the panel.

### Fixed layout
Keep the left outer width 280 and inner width 250 px, and month-grid geometry unchanged. Weather-enabled layout alone reduces day-number line from 100 to 82 px (font 78 px) and weekday line from 34 to 30 px (font 25 px); recover 22 px to make a 120 px weather area fit below a six-week month. Existing month/year line stays 30 px plus 10 px margin. Right column unchanged.
- Forecast strip follows calendar with 4 px top margin, maximum height 116 px.
- Heading (16 px line): truncated selected location + °C/°F; timezone abbreviation when its IANA id differs from app timezone; `Stale` clearly marked if needed.
- Three equal daily columns or four equal subdaily columns. Each cell: 16 px label line, 24 px inline SVG icon, 18 px temperature line, 14 px probability line `Rain 40%` / `Snow 60%` (missing `Rain —`/`Snow —`). Subdaily cross-day/time labels can use two lines; reserve 30 px for labels in both modes so no overflow surprises.
- Daily labels `Sun 18`, not ambiguous weekday-only. High/low format `16°/8°`; 14 px temperature font only when the actual longest negative/three-digit unit case cannot fit the daily cell (do not truncate numbers).
- Source line 12 px `WeatherAPI.com`, hyperlink `https://www.weatherapi.com/` in HTML; visible plain-text source survives the PNG/physical screen. No provider-owned remote images. Settings/preview also have linked credit.
- Weather errors/staleness belong in the strip, not the already crowded global calendar/backup footer.

**Tests:** compose/cache behavior and all modes/units, no weather→existing calendar/menu assertions unchanged, six-week grid data, expected missing-value labels. **Visual proof is required:** actual HTML and 1-bit output for a six-week month (March 2026), daily Fahrenheit with negative/three-digit values, four cross-midnight slots and provider credit. If the proposed dimensions fail, adjust only the approved weather-enabled large header and internal strip spacing; no change to grid/right column, no clipping, no dropped source or precipitation label.

### Existing offline overlay precedence
Preserve `server/app/display/stamp.py` and the client's offline behavior unchanged. Its 22 px text plus padding is stamped over the bottom-left **after** rendering; it can obscure lower forecast cells in an offline-stamped image. Normal weather-layout legibility guarantees above apply before that existing emergency overlay, not through it. Do not silently relocate the banner or compress the large date further to reserve space for it. Put the source credit near the **top** of the weather strip (after the heading), not at the bottom, so it stays above the overlay; total strip height budget remains 116 px. Include a six-week/four-slot offline-stamped PNG in visual proof and disclose the obscured cells explicitly.


## U4. Preview fidelity and time progression
**Depends on U3. Files:** existing `server/app/display/preview.py`, `scenarios.py`, `preview_routes.py` and `templates/preview.html`; extend `server/tests/test_preview.py`, `test_preview_routes.py`.

- Add cheap weather version inputs: canonical `weather.config`, snapshot location identity/success time, and a pure selection marker (the four expected boundary epochs / three dates, freshness bucket, displayed heading timezone abbreviation and UTC offset), not a provider call. Selection must use the same logic as display composition. Four cells from a three-day snapshot are bounded in-memory work; do not reintroduce full-model composition into `/preview/version`. Daily-mode DST transitions must update the heading even while dates and freshness are unchanged.
- Extend preview layout invalidation to include modification times of `templates/weather/strip.html` and `templates/weather/icons.html` as well as existing display/CSS paths. Both HTML and PNG must refresh when either partial changes without a Python reload. Extend the existing template-mtime regression to each new partial.
- Include actual age bucket changes at 1/6 hours even with pretend time, actual slot advancement without fetches, and location midnight; keep existing marker behavior for calendar/menu.
- Real preview uses cached forecasts for its effective date/time only. Out-of-horizon weather becomes unavailable; existing calendar coverage note remains independent.
- Extend existing Scenario builders with deterministic three-day/hourly weather. Match fixtures to scenario effective time (including month-boundary time shift); scenario weather never queries WeatherAPI.
- Add a preview-only weather state control `weather=sample|snow|missing|stale|unavailable|off` (default sample in scenarios; real mode with no override uses saved weather). `sample` uses a deterministic mix of sunny/cloud/rain conditions; `snow` stresses snow percentage. Scenario/override weather respects saved mode/unit, defaulting to daily/Celsius if unconfigured, and is visible even when real weather is disabled. These controls affect preview inputs/query/version/cache keys only, never saved config or panel requests. Do not add location/timezone/unit/mode override controls; change real mode/units in Settings.
- Keep panel cache/ETag isolated as today. Updating cached weather naturally changes the next panel render; retain Pi client's five-minute polling and quiet hours.

**Tests:** version changes at a selected-slot boundary, location midnight and 1/6-hour age thresholds without fetch; key isn't included in any version URL/response; changes of mode/unit/location/snapshot invalidate; real pretend-time outside weather coverage; sample/snow/missing/stale/off scenarios; `/preview/version`, `/display` and PNG render make zero upstream requests.

Additional review regression cases: re-enable before the prior 30-minute deadline fetches now; disable→enable and A→B→A while a request is running discard old-generation completion; incomplete middle boundary preserves its labelled empty cell; daily-mode DST offset/abbreviation change invalidates the preview version.

## Verification and completion
Use TDD at U1 pure selection/normalisation, U2 settings/fetch lifecycle and U4 version boundaries. Reuse current pytest/session fixtures; no real network in permanent tests. Run targeted tests during implementation; run full server/client suites at the end.

Smoke with a disposable test DB and server:
1. No key/location: app and display work unchanged; Settings gives setup instructions.
2. Set a locally supplied `WEATHER_API_KEY`, search and explicitly select the user's location, enable. Within 5 seconds the first attempt begins; Settings records success and resolved timezone. Compare all three daily probabilities/highs/lows with actual provider fields.
3. Switch units and all three modes; confirm immediate cached presentation changes, no additional upstream call. A 09:20 local snapshot displays the expected fixed-boundary times from U1.
4. Visual-check `/preview` and the real 800×480 1-bit frame for U3 cases, with no clipping/overlap and visible source credit. Include local icons at night and unknown code.
5. Deny upstream access or inject a provider failure in a throwaway smoke harness: last-good survives, ages as specified, calendar/menu still render. Change location during an in-flight call and ensure old-place data isn't installed.
6. Prove weather preview version advances with time, both panes catch up, and existing preview/panel cache isolation remains intact.

The user must supply a WeatherAPI account/key and location privately for live provider smoke; if absent, finish all reachable implementation and fixture-based/browser proof, state precisely which real API checks remain unexercised, never fabricate live weather success. No release, PR or deployment is authorised by this planning confirmation alone. Subsequent implementation handoff should follow the established worker → review → fix → re-review workflow if requested.

## Sources and constraints
- WeatherAPI Free pricing/horizon: https://www.weatherapi.com/pricing.aspx
- Forecast fields and requests: https://www.weatherapi.com/docs/forecast-api/
- Search API: https://www.weatherapi.com/docs/search-api/
- Location fields, including absence of timezone from search: https://www.weatherapi.com/docs/location-object/
- Condition code list: https://www.weatherapi.com/docs/weather_conditions.json
- Attribution: https://www.weatherapi.com/docs/attribution/
- Terms: https://www.weatherapi.com/terms.aspx

WeatherAPI is a commercial hosted service with a free tier, not a self-hosted/open-source component. No alternative provider integration or automatic fallback is in this feature. API request examples researched during planning were not exercised with credentials.
