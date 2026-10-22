#!/usr/bin/env bash
# Install the e-ink frame client on Linux (default: Raspberry Pi + Waveshare).
# Run from this directory: sudo ./install.sh [--generic]   (safe to re-run)
# Optionally set TIMEZONE=Europe/Paris; otherwise the host's timezone is kept.
set -euo pipefail

generic=false
case "$#" in
  0) ;;
  1) [[ "$1" == --generic ]] && generic=true || { echo "Usage: sudo ./install.sh [--generic]" >&2; exit 1; } ;;
  *) echo "Usage: sudo ./install.sh [--generic]" >&2; exit 1 ;;
esac

if [[ $EUID -ne 0 || -z "${SUDO_USER:-}" || "$SUDO_USER" == root ]]; then
  echo "Run as a normal user via sudo: sudo ./install.sh" >&2
  exit 1
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
opt=/opt/home-calendar-frame
etc=/etc/home-calendar-frame
state=/var/lib/home-calendar-frame
epd_base=https://raw.githubusercontent.com/waveshareteam/e-Paper/a794fbc39656b0f93938d1ffb3fdc77eaed9e9fc/RaspberryPi_JetsonNano/python/lib/waveshare_epd

apt-get update
if "$generic"; then
  apt-get install -y python3-pil fonts-dejavu-core
else
  apt-get install -y python3-pil python3-spidev python3-gpiozero python3-lgpio fonts-dejavu-core curl
  raspi-config nonint do_spi 0
fi
if [[ -n "${TIMEZONE:-}" ]]; then
  timedatectl set-timezone "$TIMEZONE"
fi

install -d "$opt/adapters"
if ! "$generic"; then
  install -d "$opt/waveshare_epd"
  for f in __init__.py epdconfig.py epd7in5_V2.py; do
    curl -fsSL "$epd_base/$f" -o "$opt/waveshare_epd/$f"
  done
fi
install -m 755 "$here/frame.py" "$opt/frame.py"
install -m 644 "$here/stamp.py" "$opt/stamp.py"  # a symlink in the repo; install copies its target
install -m 644 "$here"/adapters/*.py "$opt/adapters/"

install -d "$etc"
if [[ ! -f "$etc/frame.env" ]]; then
  install -m 644 "$here/frame.env.example" "$etc/frame.env"
fi

install -d -o "$SUDO_USER" "$state"
if ! "$generic"; then
  usermod -aG spi,gpio "$SUDO_USER"
fi

sed "s/__FRAME_USER__/$SUDO_USER/" "$here/home-calendar-frame.service" > /etc/systemd/system/home-calendar-frame.service
install -m 644 "$here/home-calendar-frame.timer" /etc/systemd/system/home-calendar-frame.timer
systemctl daemon-reload
if "$generic"; then
  systemctl enable home-calendar-frame.timer
  echo "Next: install your adapter and its dependencies, set FRAME_DRIVER and FRAME_URL in $etc/frame.env,"
  echo "then start polling with: sudo systemctl start home-calendar-frame.timer"
else
  systemctl enable --now home-calendar-frame.timer
  echo "Next: edit $etc/frame.env and set FRAME_URL (see README)."
fi
