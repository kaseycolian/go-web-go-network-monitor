# `3  qC`

## Windows

1. Copy this folder onto the machine.
2. Open a terminal **as Administrator** in this folder.
3. Run:
   ```
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```
4. Choose a dashboard passcode (4+ digits) when asked — other devices need it.
5. Done — it's now running and starts automatically with Windows.

## Raspberry Pi / Linux

1. Copy this folder onto the machine.
2. In a terminal, from this folder, run:
   ```
   bash install.sh
   ```
3. Choose a dashboard passcode (4+ digits) when asked — other devices need it.
4. Done — it's now running and starts automatically on boot.

## Already installed, but not running?

(Dashboard won't load? Start it like this — from this folder.)

**Windows** (PowerShell):
```
Start-ScheduledTask -TaskName WhatTheBit
```
or run it directly in a terminal:
```
.venv\Scripts\python -m app.main      # cmd / PowerShell
./.venv/Scripts/python -m app.main    # Git Bash
```

**Raspberry Pi / Linux:**
```
systemctl --user start whatthebit
```

## Open the dashboard

- On the machine itself: **http://localhost:8745**
- From your phone or any device on your WiFi: **http://THE-MACHINE-IP:8745**
  (the installer prints this address; e.g. `http://192.168.50.23:8745`)

The first time you open it from another device, enter the passcode you set at
install. You stay logged in on that device. On the host machine itself
(`http://localhost:8745`) you never need the passcode, and from there you can
reset it or turn protection on/off under **Settings → Security**.

Tip: on your phone, use the browser's **Add to Home Screen** to install it
like an app.

Stuck? See [README.md](README.md) for details and troubleshooting.
