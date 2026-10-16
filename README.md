# home-calendar-screen

A household kitchen app:

- **Recipe library**: save dinners by hand or import them from links, screenshots and paper scans (Claude extracts the recipe).
- **Weekly dinner menu** (Saturday–Friday) with a "roll the dice" suggester and a shopping list.
- **E-ink display**: a Raspberry Pi 3B with a Waveshare 7.5" V2 (800×480, black/white) shows the date, a month grid, upcoming Google Calendar events (secret ICS URLs) and this week's dinners.

The server (`server/`, FastAPI + SQLite) runs as a Docker container on the homelab Pi 4 at `https://kitchen.home`. It renders the panel image; the Pi 3B (`display-client/`) only pulls `/display/frame.png` every 5 minutes and redraws when the ETag changes. No login: LAN and Tailscale only.

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

Open http://127.0.0.1:8000. Other env vars: `TZ` (default `Europe/London`), `LLM_MODEL` (default `claude-sonnet-5-5`).

Screen preview: http://127.0.0.1:8000/preview (linked from Settings) shows the panel's HTML and its 1-bit PNG side by side and refreshes when the data, `display.html`/`app.css` or the Python code (under `--reload`) changes. Its pretend time, scenario, forced-warning and offline-stamp controls live in the URL and never affect the real panel.

Display client tests (need Pillow): `server/.venv/bin/python -m pytest -q display-client`.
Dry-run the client against a local server:

```sh
python3 display-client/frame.py --url http://localhost:8000/display/frame.png \
  --state-dir /tmp/fstate --quiet-hours off --dry-run --out /tmp/panel.png
```

## Docker

```sh
docker build -t home-calendar server
docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-... \
  -v "$PWD/.data:/data" home-calendar
```

The container runs as uid 1000 / gid 100, applies migrations on start and serves on port 8000.

## Release

Pushing a tag `vX.Y.Z` runs `.github/workflows/release.yml`: server and display-client tests, then an arm64 image build pushed to `ghcr.io/mustyf10/home-calendar-screen:vX.Y.Z`.

```sh
git tag v0.1.0 && git push origin v0.1.0
```

After the first release, make the package public (GitHub → Packages → home-calendar-screen → Package settings → Change visibility) and check with `docker logout ghcr.io && docker pull ghcr.io/mustyf10/home-calendar-screen:v0.1.0`.

To upgrade, bump the `image:` tag in `/compose/home-calendar/compose.yaml` (or in Dockge) and run `sudo docker compose up -d`. diun announces new tags.

## Pi 4 (server) one-time setup

On `pi@192.168.1.4`:

1. Data and backup dirs:
   ```sh
   sudo install -d -o 1000 -g 100 /appdata/home-calendar
   sudo install -d -o 1000 -g 100 /srv/mergerfs/pool1/backups/home-calendar
   ```
2. `sudo mkdir /compose/home-calendar` and copy in `deploy/compose.yaml`.
3. Create `/compose/home-calendar/.env`:
   ```
   TZ=Europe/London
   ANTHROPIC_API_KEY=sk-...
   ```
4. Add `kitchen.home home-calendar:8000` under `# Utilities` in the `*.home` map of `/compose/caddy-cloudflare/conf/Caddyfile`, then `sudo docker exec -w /etc/caddy caddy caddy reload`.
5. Add DNS: read `sudo pihole-FTL --config dns.hosts`, then write it back with the new entry appended:
   `sudo pihole-FTL --config dns.hosts '[ …existing entries…, "192.168.1.4 kitchen.home" ]'`.
6. `cd /compose/home-calendar && sudo docker compose up -d`.

Check: `sudo docker ps | grep home-calendar` shows `healthy`, and `curl -sk --resolve kitchen.home:443:127.0.0.1 https://kitchen.home/healthz` returns `{"ok":true}`.

Backups run nightly at 03:30 into `/srv/mergerfs/pool1/backups/home-calendar/<date>/` (14 days kept).

If `kitchen.home` doesn't resolve over Tailscale, add `192.168.1.4` as a split-DNS nameserver for `home` in the Tailscale admin console.

## Pi 3B (display) install

1. Flash Raspberry Pi OS Lite 64-bit, enable SSH, attach the Waveshare HAT.
2. Copy `display-client/` to the Pi (`scp -r` or `rsync -rL`: `stamp.py` is a symlink to `server/app/display/stamp.py` and must arrive as a file) and run `sudo ./install.sh` in it (idempotent). It installs packages, enables SPI, fetches the pinned Waveshare driver into `/opt/home-calendar-frame/`, creates `/etc/home-calendar-frame/frame.env` and enables the 5-minute timer.
3. Copy Caddy's root CA (from your laptop):
   ```sh
   ssh pi@192.168.1.4 sudo cat /appdata/caddy/data/caddy/pki/authorities/local/root.crt > caddy-root.crt
   scp caddy-root.crt <pi3b>:/tmp/
   ssh <pi3b> sudo install -m 644 /tmp/caddy-root.crt /etc/home-calendar-frame/caddy-root.crt
   ```
4. Reboot (picks up SPI and group changes), then `sudo systemctl start home-calendar-frame.service` and `journalctl -u home-calendar-frame -n 20`.

Behaviour: no redraws during `QUIET_HOURS` (default `23:00-06:00`, `off` disables). If the server is unreachable for 60 minutes, the last frame is redrawn with an "Offline since HH:MM" box in the bottom-left corner; the next successful fetch redraws normally. State lives in `/var/lib/home-calendar-frame`.

## Trusting `https://kitchen.home` on phones

Caddy signs `*.home` with its own local CA. Get the root certificate once:

```sh
ssh pi@192.168.1.4 sudo cat /appdata/caddy/data/caddy/pki/authorities/local/root.crt > caddy-root.crt
```

Send `caddy-root.crt` to the phone (AirDrop, email or a file share).

**iPhone / iPad**
1. Open the file → "Profile Downloaded".
2. Settings → General → VPN & Device Management → the Caddy profile → Install.
3. Settings → General → About → Certificate Trust Settings → turn on full trust for "Caddy Local Authority".

**Android**
1. Settings → Security & privacy → More security settings → Encryption & credentials → Install a certificate → CA certificate (wording varies by vendor).
2. Pick `caddy-root.crt` and confirm. Chrome trusts it straight away; Firefox needs "Use third party CA certificates" enabled in its secret settings.
