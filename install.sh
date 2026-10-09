#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"
ROOT="$(pwd)"
if [[ "$EUID" -eq 0 ]]; then echo 'Run as your normal Hermes user, NOT with sudo'; exit 1; fi
if [[ ! -f .env ]]; then cp .env.example .env; chmod 600 .env; echo 'Edit .env before continuing: nano .env'; exit 1; fi
if grep -qE 'replace-me|replace-with|your-tapo-account' .env; then echo 'Complete all credentials in .env before installing'; exit 1; fi
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cat > /tmp/desk-power-service-"$$" <<EOF
[Unit]
Description=Desk Power Multi-P115 Monitor
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python $ROOT/server.py
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$ROOT/data
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
mkdir -p data
sudo install -m 644 /tmp/desk-power-service-"$$" /etc/systemd/system/desk-power.service
rm /tmp/desk-power-service-"$$"
sudo systemctl daemon-reload
sudo systemctl enable --now desk-power.service
echo
echo 'Service started. Check: sudo systemctl status desk-power --no-pager'
echo 'Live log: sudo journalctl -u desk-power -f'
echo 'Dashboard: http://hermes.karmtin:8766 (unless changed in .env)'
