#!/usr/bin/env bash
# What the Bit — Linux / Raspberry Pi installer.
# Creates a venv, installs deps, sets a dashboard passcode, and installs a
# systemd user service so the monitor starts on boot. Run from the project
# folder:
#   bash install.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PORT=8745

echo "Using $(python3 --version)"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --upgrade pip --quiet
./.venv/bin/pip install -r requirements.txt --quiet
echo "Dependencies installed."

# passcode: required from other devices; the host machine (localhost) is
# always exempt and can reset it later from Settings.
if ! grep -q '"password_hash": "' config.json 2>/dev/null; then
    echo ""
    echo "Set a dashboard passcode (numbers only, at least 4 digits)."
    echo "Other devices on your network will need it; this machine won't."
    while true; do
        read -rsp "Passcode: " PIN; echo
        if [[ "$PIN" =~ ^[0-9]{4,}$ ]]; then break; fi
        echo "  Must be at least 4 digits (numbers only). Try again."
    done
    ./.venv/bin/python -m app.auth set-passcode "$PIN"
fi

mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/whatthebit.service <<EOF
[Unit]
Description=What the Bit network monitor
After=network.target

[Service]
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python -m app.main
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now whatthebit.service
# keep the user service running without an open login session
loginctl enable-linger "$USER" 2>/dev/null || true

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "Done! Dashboard:  http://localhost:$PORT"
[ -n "$IP" ] && echo "From your phone: http://$IP:$PORT"
