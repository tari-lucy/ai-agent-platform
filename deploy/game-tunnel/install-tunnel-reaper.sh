#!/usr/bin/env bash
# Install the tunnel session reaper (see aw_tunnel_reaper.py): a systemd timer that, every 20 s,
# ends the tunnel account's SSH sessions that have been silent for over 75 s.
# Run as root:  sudo bash install-tunnel-reaper.sh
# Remove:       sudo systemctl disable --now aw-tunnel-reaper.timer
#               sudo rm /usr/local/sbin/aw-tunnel-reaper /etc/systemd/system/aw-tunnel-reaper.*
#               sudo systemctl daemon-reload
# It touches only its own three files; no SSH, firewall, package or Docker change.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run this script as root.'; exit 1; }
source_file="$(dirname -- "$(readlink -f -- "$0")")/aw_tunnel_reaper.py"
[[ -f $source_file ]] || { echo 'aw_tunnel_reaper.py must be next to this script.'; exit 1; }
command -v python3 >/dev/null || { echo 'python3 is required.'; exit 1; }
install -o root -g root -m 755 "$source_file" /usr/local/sbin/aw-tunnel-reaper
cat > /etc/systemd/system/aw-tunnel-reaper.service <<'UNIT'
[Unit]
Description=End silent SSH sessions of the AI Agent Platform tunnel account

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I /usr/local/sbin/aw-tunnel-reaper
ProtectHome=true
ProtectSystem=strict
PrivateTmp=true
NoNewPrivileges=true
UNIT
cat > /etc/systemd/system/aw-tunnel-reaper.timer <<'UNIT'
[Unit]
Description=Look for silent tunnel sessions every 20 seconds

[Timer]
OnBootSec=30
OnUnitActiveSec=20
AccuracySec=1

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now aw-tunnel-reaper.timer
systemctl start aw-tunnel-reaper.service
echo 'TUNNEL_REAPER_READY'
