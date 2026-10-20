# home-calendar-screen

A self-hosted household kitchen app:

- **Recipe library**: save dinners by hand or import them from links, screenshots and paper scans (Claude extracts the recipe).
- **Weekly dinner menu** (Saturday–Friday) with a "roll the dice" suggester and an automatically rebuilt shopping list.
- **E-ink display**: a Raspberry Pi with a Waveshare 7.5" V2 panel (800×480, black/white) shows the date, a month grid, upcoming Google Calendar events (secret ICS URLs), this week's dinners and, optionally, a weather forecast.

The server (`server/`, FastAPI + SQLite) renders the panel image. The display client (`display-client/`) pulls `/display/frame.png` every 5 minutes and redraws for changed content, connection status or periodic full-refresh maintenance, subject to the Settings-controlled night pause and refresh safeguards.

**There is no login.** Run it on a private network (home LAN, VPN or tailnet) and never expose it to the internet.

## Requirements

- A machine to run the server in Docker (any Linux host; the published image is `arm64`, see [Release](#release) for other architectures).
- An [Anthropic API key](https://console.anthropic.com/) for recipe import and shopping lists.
- Optional: a [WeatherAPI.com](https://www.weatherapi.com/) key for the forecast strip.
- Optional: a Raspberry Pi (3B or newer) with a Waveshare 7.5" V2 e-paper HAT for the physical display. Without it, `/preview` shows exactly what the panel would show.

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

Screen preview: `/preview` (linked from Settings) shows the panel's HTML and its 1-bit PNG side by side and refreshes when the data, `display.html`/`app.css` or the Python code (under `--reload`) changes. Its pretend time, scenario, forced-warning, offline-stamp and weather-state controls live in the URL and never affect the real panel.

Display client tests (need Pillow): `server/.venv/bin/python -m pytest -q display-client`. Dry-run the client against a local server (disable the night pause in **Settings → Display** if running during the pause; use a separate state directory so the dry-run does not affect the physical client's refresh history):

```sh
python3 display-client/frame.py --url http://localhost:8000/display/frame.png \
  --state-dir /tmp/fstate --dry-run --out /tmp/panel.png
```

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

## Display client (Raspberry Pi + Waveshare 7.5" V2)

1. Flash Raspberry Pi OS Lite 64-bit, enable SSH, attach the HAT.
2. Copy `display-client/` to the Pi with `rsync -rL` or `scp -r` (`stamp.py` is a symlink to `server/app/display/stamp.py` and must arrive as a real file), then run it from that directory:

   ```sh
   sudo ./install.sh                         # keeps the Pi's timezone
   sudo TIMEZONE=Europe/Paris ./install.sh   # or set it explicitly
   ```

   It is safe to re-run. It installs packages, enables SPI, fetches a pinned Waveshare driver into `/opt/home-calendar-frame/`, creates `/etc/home-calendar-frame/frame.env` and enables a 5-minute systemd timer.
3. Edit `/etc/home-calendar-frame/frame.env`: set `FRAME_URL` to your server's `/display/frame.png`. If you serve HTTPS with a private certificate authority, copy its root certificate to the Pi and set `CA_FILE` to its path; leave `CA_FILE` empty otherwise.
4. Reboot (picks up SPI and group changes), then `sudo systemctl start home-calendar-frame.service` and `journalctl -u home-calendar-frame -n 20`.

In **Settings → Display**, enable, disable or change the night pause (initially **23:00–06:00**). Times use the app's `TZ`, including daylight-saving changes, not the display Pi's timezone. The last image stays visible during the pause; the screen is not blanked and the Pi stays on. The client still checks the server every five minutes, so settings changes arrive even while redraws are paused.

All physical redraws are at least **180 seconds apart**, including changed images, offline warnings and recovery. A single normal full redraw is forced after **23 hours 45 minutes**, leaving margin before the manufacturer's 24-hour recommendation even when content is unchanged. It uses the cached image if the server is offline. Maintenance takes priority over the night pause and may happen just before it starts; no extra `Clear()` or blank-screen cycle is added. The driver sleeps after drawing.

After an hour without the server, the next permitted redraw adds an "Offline since HH:MM" warning. Offline maintenance keeps that warning and its original time; successful recovery removes it only when the clean frame is actually redrawn. The last valid schedule, refresh attempts/completions and clean image persist in `/var/lib/home-calendar-frame`. Concurrent invocations are serialized. These timing safeguards require a running Pi, a valid clock and a working panel; they cannot guarantee a refresh during power loss or hardware failure.

**Updating an existing display:** update the server, copy the updated `display-client/` files and re-run `install.sh`. Settings is now the only schedule authority: remove `QUIET_HOURS` from `/etc/home-calendar-frame/frame.env` (the installer preserves this file), and remove any custom `--quiet-hours` argument. The environment variable is no longer read and the CLI option is no longer accepted. Previous cached-image metadata is migrated automatically; a cache without refresh history waits three minutes before its first safety redraw. A new client must receive a valid policy from the updated server before drawing ordinary updates; after that, the cached policy works offline.

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
