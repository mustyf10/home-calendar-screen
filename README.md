# home-calendar-screen

A self-hosted household kitchen app:

- **Recipe library**: save dinners by hand or import them from links, screenshots and paper scans (Claude extracts the recipe).
- **Weekly dinner menu** (Saturday–Friday) with a "roll the dice" suggester and an automatically rebuilt shopping list.
- **E-ink display**: monochrome calendar images at your display's resolution, showing the date, a month grid, upcoming Google Calendar events (secret ICS URLs), this week's dinners and, optionally, a weather forecast. The bundled hardware adapter supports a Raspberry Pi with a Waveshare 7.5" V2 panel (800×480).

The server (`server/`, FastAPI + SQLite) renders a hardware-independent 1-bit PNG. The display client (`display-client/`) pulls `/display/frame.png` every 5 minutes and redraws for changed content, connection status or adapter-specific full-refresh maintenance, subject to the Settings-controlled night pause and refresh safeguards. Other panels require their own locally installed adapter and dependencies; this is not universal plug-and-play hardware support.

**There is no login.** Run it on a private network (home LAN, VPN or tailnet) and never expose it to the internet.

## Requirements

- A machine to run the server in Docker (any Linux host; the published image is `arm64`, see [Release](#release) for other architectures).
- An [Anthropic API key](https://console.anthropic.com/) for recipe import and shopping lists.
- Optional: a [WeatherAPI.com](https://www.weatherapi.com/) key for the forecast strip.
- Optional: a Raspberry Pi (3B or newer) with a Waveshare 7.5" V2 e-paper HAT for the bundled hardware adapter, or a POSIX Python host and a custom panel adapter. Without hardware, `/preview` shows the HTML composition and output PNG.

## Quick start (Docker Compose)

1. Copy `deploy/compose.yaml` to a directory of your choice and create `.env` next to it:

   ```sh
   TZ=Europe/London
   ANTHROPIC_API_KEY=sk-...
   # WEATHER_API_KEY=...        # optional, enables Settings → Weather
   # LLM_MODEL=claude-sonnet-5-5  # any Claude model with structured outputs
   ```

2. Create the data and backup directories. The container runs as uid 1000 / gid 100:

   ```sh
   mkdir -p data backups && sudo chown 1000:100 data backups
   ```

3. Start it:

   ```sh
   docker compose up -d
   ```

4. Open `http://<server>:8000`. Check health with `curl http://<server>:8000/healthz` (returns `{"ok":true}`).

Migrations run automatically on start. To upgrade, change the `image:` tag and run `docker compose up -d`.

To serve it over HTTPS under a hostname of your choice, put any reverse proxy in front of port 8000 and remove the `ports:` mapping. Keep it private (see above).

### Backups

Backups run nightly at 03:30 into `/backups/<date>/` inside the container (mounted from `./backups`), keeping 14 days.

## Local development

```sh
cd server
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install chromium        # for the display frame render
.venv/bin/pytest -q

mkdir -p /tmp/hcs-data
export DATA_DIR=/tmp/hcs-data BACKUP_DIR=/tmp/hcs-backups ANTHROPIC_API_KEY=sk-...
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. Other env vars: `TZ` (default `Europe/London`), `LLM_MODEL` (default `claude-sonnet-5-5`; an override must support [structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)), `WEATHER_API_KEY`.

Screen preview: `/preview` (linked from Settings) shows the fitted HTML and its 1-bit PNG side by side and refreshes when the data, `display.html`/`app.css` or the Python code (under `--reload`) changes. Its pretend time, scenario, forced-warning, offline-stamp, weather-state and output-dimension controls live in the URL and never affect another display. The optional offline stamp appears in the PNG pane only. Select 1× or 2× zoom for pixel sizing. Physical-size zoom needs the screen's physical width in millimetres; it defaults to 163.2 mm only for 800×480, and is disabled at other resolutions until you enter a positive width. Calibration is saved in this browser per pixel resolution, not sent to the server.

Display client tests (need Pillow): `server/.venv/bin/python -m pytest -q display-client`. Dry-run the client against a local server (disable the night pause in **Settings → Display** if running during the pause; use a separate state directory so the dry-run does not affect the physical client's refresh history):

```sh
python3 display-client/frame.py --url http://localhost:8000/display/frame.png \
  --state-dir /tmp/fstate --dry-run --out /tmp/panel.png
```

For a portrait dry-run, add `--width 600 --height 800` and use another state directory. Dry-run writes a PNG without loading a hardware adapter or its libraries. It still respects the server's night pause and persistent redraw history.

## Display output and geometry

`/display/frame.png?width=600&height=800` returns a 600×800 monochrome PNG. `/display`, `/preview`, `/preview/version` and `/preview/frame.png` accept the same dimensions. Each axis defaults independently to 800×480: `?width=400` means 400×480, not 400×240. Values must be integers from 1 to 8192, with at most 16,777,216 pixels in total. Empty, malformed, repeated or out-of-range dimension parameters return HTTP 422.

The existing composition remains an 800×480 design canvas. Output uniformly fits it without cropping or stretching, with white letterboxing: 400×240 scales to half size; 600×800 contains 600×360 content starting at y=220; 1200×825 contains 1200×720 content starting at y=52. Integer rounding can differ by a subpixel between axes. Scaling uses nearest-neighbour sampling to keep the output 1-bit. Very small sizes are valid images, not a promise of readable text. Portrait means height exceeds width; physical mounting rotation belongs to the adapter.

Dimensions are request-local, not a database or night-policy setting, so multiple screens can share a server. The server renders one canonical composition and caches dimension-specific derived PNGs; ETags and conditional requests identify the selected representation, not another screen's geometry. Offline stamps use the same fitted design coordinates and stay clear of the letterbox bars.

## Weather (optional)

Set `WEATHER_API_KEY` in the server environment and recreate the container. Weather is disabled by default and needs no database migration. The key stays on the server and is never shown in the browser.

In **Settings → Weather**, search for a location, select the exact result (region and country are shown), then enable weather. Selecting a location alone does not enable it. Choose three daily forecasts or four sampled hours at fixed 2- or 3-hour boundaries, in Celsius or Fahrenheit. The forecast uses the location's timezone; the calendar and dinners keep the server's `TZ`. Subdaily rain/snow percentages describe the sampled hour, not the whole interval.

The server fetches within a few seconds of enabling and every 30 minutes after, including after failures. Changing mode or units, opening the display and using the preview all read the cache without another provider call. Last-good data is marked stale after one hour and hidden after six hours or beyond forecast coverage.

The offline banner (below) takes precedence over the lowest-left forecast cells; the provider credit sits above it. The display client needs no weather key.

## Bin collections (optional)

In **Settings → Bins**, enter your postcode and house number or name. The app checks the postcode with [postcodes.io](https://postcodes.io), detects the council and asks for a UPRN only if that council needs one, with a link to find it. If detection fails, choose the council from the list. Councils whose scrapers require a web browser are not supported on the kitchen Pi.
Changing the address re-detects the council and asks for a new UPRN where needed. A failed lookup keeps the chosen council for a retry.

Dates come from [UKBinCollectionData](https://github.com/robbrad/UKBinCollectionData), with no API key or database migration. Every returned bin type appears as an all-day `Bins: …` agenda event and a dot in the month grid; bins collected on the same day share one line. Only council-provided dates are shown—no collection patterns are projected.

Collections refresh twice a day, or with **Update now**. Failed updates keep the last-good dates and retry hourly; after two days of failures the screen shows a warning. **Remove** clears the address, dates and status. Enter the household's address only on the deployed app, never in repository files.

## Display client

### Raspberry Pi + Waveshare 7.5" V2 (default)

1. Flash Raspberry Pi OS Lite 64-bit, enable SSH, attach the HAT.
2. Copy all of `display-client/`, including `adapters/`, to the Pi with `rsync -rL` (`stamp.py` is a symlink to `server/app/display/stamp.py` and must arrive as a real file; do not copy only `frame.py`), then run it from that directory:

   ```sh
   sudo ./install.sh                         # keeps the Pi's timezone
   sudo TIMEZONE=Europe/Paris ./install.sh   # or set it explicitly
   ```

   It is safe to re-run and preserves an existing `frame.env`. It installs packages, enables SPI, fetches a pinned Waveshare driver into `/opt/home-calendar-frame/`, copies the client/shared stamp/adapters together, creates `/etc/home-calendar-frame/frame.env` if absent, and enables **and starts** a 5-minute systemd timer.
3. Edit `/etc/home-calendar-frame/frame.env`: set `FRAME_URL` to your server's `/display/frame.png`. If you serve HTTPS with a private certificate authority, copy its root certificate to the Pi and set `CA_FILE` to its path; leave `CA_FILE` empty otherwise.
4. Reboot (picks up SPI and group changes), then `sudo systemctl start home-calendar-frame.service` and `journalctl -u home-calendar-frame -n 20`.

### Other adapters and generic Linux installation

On a Linux system with systemd, copy the same complete client directory (dereferencing the stamp symlink), then run:

```sh
sudo ./install.sh --generic
```

Generic mode installs only `python3-pil` and `fonts-dejavu-core` plus the client, shared stamp, adapters and service/timer. It does **not** configure SPI, add GPIO/SPI groups, install GPIO libraries or download vendor code. It preserves an existing `frame.env` and enables the timer without starting it. Before starting, install your adapter and its hardware dependencies, configure its physical pins/transport, and set `FRAME_DRIVER` and `FRAME_URL` in `/etc/home-calendar-frame/frame.env`. Then run `sudo systemctl start home-calendar-frame.timer`. Other installer arguments are rejected. The default installation still performs the Pi/Waveshare setup.

The driver selector is `--driver module:factory` or `FRAME_DRIVER`; the default is `adapters.waveshare7in5_v2:create`. The factory is imported from trusted, locally installed Python code and called with no arguments. It must return an object with a `PanelSpec` from `adapters` as `.spec` and a callable `.show(image)`. Spec loading must not initialize hardware. `show` receives a mode-`1` Pillow image at exactly `spec.size`, clean or offline-stamped; the adapter owns transport, pixel packing, mounting rotation, hardware dependencies and the device's sleep/cleanup lifecycle. Only the bundled Waveshare adapter is supplied; other manufacturers need real drivers, not merely a changed resolution.

`PanelSpec(size, min_interval, maintenance_after, max_interval)` is frozen. Size is a tuple of two integers (not booleans), with the same axis/pixel bounds as the server. Intervals are `datetime.timedelta` values: minimum spacing must be nonnegative. Set both maintenance values to `None` to disable periodic and pre-pause maintenance, or require `min_interval <= maintenance_after < max_interval`, leaving at least five minutes between maintenance and maximum intervals. The client's five-minute poll cadence is fixed; hardware needing more frequent maintenance is unsupported. Invalid driver/spec configuration fails before networking or drawing.

Real mode uses the adapter's dimensions. Optional `--width`/`FRAME_WIDTH` and `--height`/`FRAME_HEIGHT` must match its spec; these options select arbitrary sizes only in dry-run, where each axis defaults independently to 800×480 and bundled timing safeguards apply without loading a driver. The client adds the resolved dimensions to `FRAME_URL`, preserving unrelated query parameters. Existing matching dimensions are accepted once; mismatches and repeated width/height parameters are errors.

Direct `python3 display-client/frame.py …` use does not require systemd, but requires Pillow and a POSIX host with `fcntl.flock`, plus the selected adapter's dependencies in real mode. The installer targets Linux; this is not a Windows client. Devices such as microcontrollers can consume the PNG endpoint with their own client; no firmware or additional native client is supplied.

### Refresh policy and state

In **Settings → Display**, enable, disable or change the night pause (initially **23:00–06:00**). Times use the app's `TZ`, including daylight-saving changes, not the display host's timezone. The last image stays visible during the pause; the screen is not blanked and the host stays on. The client still checks the server every five minutes, so settings changes arrive even while redraws are paused.

With the bundled Waveshare adapter, all physical redraws are at least **180 seconds apart**, including changed images, offline warnings and recovery. A single normal full redraw is forced after **23 hours 45 minutes**, leaving margin before the manufacturer's 24-hour recommendation even when content is unchanged. It uses a compatible cached image if the server is offline. Maintenance takes priority over the night pause and may happen just before it starts; no extra `Clear()` or blank-screen cycle is added. The driver sleeps after drawing, including after a draw failure. Other adapters use their own spec's minimum spacing and optional maintenance values.

After an hour without the server, the next permitted redraw adds an "Offline since HH:MM" warning. Offline maintenance keeps that warning and its original time; successful recovery removes it only when the clean frame is actually redrawn. The last valid schedule, refresh attempts/completions and clean image persist in `/var/lib/home-calendar-frame` by default. Concurrent invocations are serialized. Use a **separate `--state-dir` for each physical screen**, including when switching adapters to drive a different screen: refresh history belongs to the physical device. Unreadable or wrong-size cached images cannot be redrawn or stamped; their ETag is omitted to fetch a replacement unconditionally, while attempt/completion history and spacing guards remain intact. A bad fetched image retains the last good cache and exits with code 2. These timing safeguards require a running host, a valid clock and working hardware; they cannot guarantee refresh during power loss or hardware failure.

**Updating an existing display:** update the server, copy the updated `display-client/` files **including `adapters/` and the shared stamp**, and re-run the appropriate installer mode. Existing Waveshare deployments need no driver/dimension environment changes. Settings remains the only schedule authority: remove obsolete `QUIET_HOURS` from `/etc/home-calendar-frame/frame.env` (the installer preserves this file), and remove any custom `--quiet-hours` argument. The environment variable is no longer read and the CLI option is no longer accepted. Previous cached-image metadata is migrated automatically; a cache without refresh history waits three minutes before its first safety redraw. A new client must receive a valid policy from the updated server before drawing ordinary updates; after that, the cached policy works offline.

## Release

Pushing a tag `vX.Y.Z` runs `.github/workflows/release.yml`: server and display-client tests, then an `arm64` image build pushed to `ghcr.io/<owner>/home-calendar-screen:vX.Y.Z`.

```sh
git tag v1.2.3 && git push origin v1.2.3
```

If you fork the project, change the image name and source label in the workflow and the `image:` in `deploy/compose.yaml`. After the first release, make the package public (GitHub → Packages → Package settings → Change visibility) so servers can pull it without logging in. For another architecture, change `platforms:` in the workflow or build locally:

```sh
docker build -t home-calendar server
docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-... -v "$PWD/.data:/data" home-calendar
```

## Trusting a private HTTPS certificate on phones

Only needed if you front the app with a private certificate authority. Send the CA's root certificate to the phone, then:

**iPhone / iPad**: open the file → "Profile Downloaded"; Settings → General → VPN & Device Management → install it; Settings → General → About → Certificate Trust Settings → enable full trust.

**Android**: Settings → Security & privacy → More security settings → Encryption & credentials → Install a certificate → CA certificate (wording varies by vendor). Chrome trusts it straight away; Firefox needs "Use third party CA certificates" enabled in its secret settings.

## License

[MIT](LICENSE).
