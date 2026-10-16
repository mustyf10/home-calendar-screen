#!/usr/bin/env bash
# Install the kitchen e-ink frame client on Raspberry Pi OS Lite 64-bit.
# Run from this directory: sudo ./install.sh   (safe to re-run)
set -euo pipefail

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
apt-get install -y python3-pil python3-spidev python3-gpiozero python3-lgpio fonts-dejavu-core curl
raspi-config nonint do_spi 0
timedatectl set-timezone Europe/London

install -d "$opt/waveshare_epd"
for f in __init__.py epdconfig.py epd7in5_V2.py; do
  curl -fsSL "$epd_base/$f" -o "$opt/waveshare_epd/$f"
done
install -m 755 "$here/frame.py" "$opt/frame.py"
install -m 644 "$here/stamp.py" "$opt/stamp.py"  # a symlink in the repo; install copies its target

install -d "$etc"
if [[ ! -f "$etc/frame.env" ]]; then
  install -m 644 "$here/frame.env.example" "$etc/frame.env"
fi

install -d -o "$SUDO_USER" "$state"
usermod -aG spi,gpio "$SUDO_USER"

sed "s/__FRAME_USER__/$SUDO_USER/" "$here/home-calendar-frame.service" > /etc/systemd/system/home-calendar-frame.service
install -m 644 "$here/home-calendar-frame.timer" /etc/systemd/system/home-calendar-frame.timer
systemctl daemon-reload
systemctl enable --now home-calendar-frame.timer

if [[ ! -f "$etc/caddy-root.crt" ]]; then
  echo "Next: copy Caddy's root CA to $etc/caddy-root.crt (see README)."
fi
