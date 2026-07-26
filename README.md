# Go, Web, Go! ⌁

A self-hosted home network monitor that answers one question: **when the
internet "goes out", whose fault is it** — the cable/dock, the router, the
ISP, DNS, or this computer?

It pings your router, two internet anchors (1.1.1.1, 8.8.8.8) and your ISP's
first hop every few seconds, watches the adapter link state, samples
throughput, runs scheduled speed tests (with a bufferbloat grade), and serves
a live mobile-friendly dashboard on your LAN. Every outage is detected,
**classified by layer**, traced (auto-traceroute), and logged with the
location label active at the time — so "ethernet via dock in bedroom" vs
"ethernet from wall" becomes a numbers-backed verdict instead of a hunch.

> **In a hurry?** See [QUICKSTART.md](QUICKSTART.md) — install and open the
> dashboard in 5 steps.

## Quick start

1. **Get the files onto the machine** you want to monitor — copy this whole
   folder anywhere (e.g. `D:\sources\what-the-bit` or `~/what-the-bit`).
2. **Install Python 3** if it isn't already there
   ([python.org](https://www.python.org/downloads/), or
   `winget install Python.Python.3.14` on Windows,
   `sudo apt install python3-venv` on a Pi).
3. **Run the installer** (below) — it sets everything up and starts the
   monitor.
4. **Open the dashboard** — `http://localhost:8745` on the machine itself,
   or the `http://<ip>:8745` address the installer prints from any phone,
   tablet, or PC on your home network.

### Install on Windows

Open PowerShell **as Administrator** (right-click Start → "Terminal
(Admin)"), then:

```powershell
cd path\to\what-the-bit
powershell -ExecutionPolicy Bypass -File install.ps1
```

You can type this from **cmd or Git Bash too** — the command launches
PowerShell itself, so it works from any Windows shell (the installer uses
PowerShell-only features like firewall rules and Scheduled Tasks under the
hood).

Once installed, the monitor is already running (the installer starts it).
To run it manually instead — e.g. to watch its output — first stop the
background task (`Stop-ScheduledTask -TaskName WhatTheBit` in PowerShell),
then from the project folder:

```
.venv\Scripts\python -m app.main     # cmd / PowerShell
./.venv/Scripts/python -m app.main   # Git Bash / IDE bash terminals
```

If you see a port-in-use error on 8745, the background task is still
running — the dashboard is already up at http://localhost:8745.

The installer:
- creates a private Python environment (`.venv`) and installs dependencies
- asks you to set a **dashboard passcode** (4+ digits) that other devices need
- opens firewall port 8745 for **private networks only** — this is what lets
  your phone reach the dashboard
- registers a Scheduled Task so monitoring **starts automatically at logon**
  and restarts itself if it ever crashes
- prints the two dashboard addresses when it finishes

Admin rights are only needed for the firewall rule. Without admin,
everything still works *on that machine* — other devices just can't connect
until you re-run the script once as admin.

To run it by hand instead (no auto-start): `.venv\Scripts\python -m app.main`

### Install on Linux / Raspberry Pi

```bash
cd path/to/what-the-bit
bash install.sh
```

Same deal: venv + dependencies and a systemd user service so it starts
**on boot** (not just login — fine for a headless Pi). Manage it with
`systemctl --user status|restart|stop whatthebit`, logs with
`journalctl --user -u whatthebit`.

Pi notes:
- Works headless: copy the folder over with
  `scp -r what-the-bit pi@<pi-ip>:~/` and run the installer over SSH.
- If venv creation fails: `sudo apt install python3-venv`, then re-run.
- No firewall step needed — Raspberry Pi OS has none enabled by default.
  If you use `ufw`, allow the port: `sudo ufw allow 8745/tcp`.
- Find the Pi's dashboard address with `hostname -I` →
  `http://<that-ip>:8745`.

## Accessing the dashboard from your phone / other devices

Any device on the **same WiFi/LAN** can open the dashboard — nothing to
install on the phone.

1. Find the monitoring machine's IP address (the installer prints it):
   - Windows: `ipconfig` → "IPv4 Address" (e.g. `192.168.50.23`)
   - Linux/Pi: `hostname -I`
2. On your phone/tablet/other PC, open a browser and go to
   `http://<that-ip>:8745` — e.g. `http://192.168.50.23:8745`
3. Optional: use the browser's **"Add to Home Screen"** — the dashboard
   installs like an app with its own icon and full-screen view.

When passcode protection is on, each device enters the passcode once and then
stays logged in until the session expires (about a week). The machine running
the monitor is exempt: open `http://localhost:8745` **on that machine** and you
have full access with no passcode — and only from there can you reset a
forgotten passcode or turn protection on/off (**Settings → Security**). Other
devices can never reset it. Don't expose the dashboard to the internet.

**Tip:** give the monitoring machine a fixed IP (a "DHCP reservation" in
your router's admin page) so the address never changes; then bookmark it.

### If another device can't connect

- Phone and monitor machine must be on the **same network** (not guest WiFi,
  not mobile data).
- Windows: the firewall rule only allows **Private** networks. Check the
  network is marked Private: Settings → Network & internet → your network →
  "Private network". Then re-run `install.ps1` as admin if needed.
- Confirm the monitor is running: does `http://localhost:8745` work on the
  machine itself? If not, re-run the installer and watch for errors.
- The dashboard is (deliberately) **not reachable from the internet** — LAN
  only. Don't port-forward it; if you need remote access, use a VPN into
  your home network (e.g. WireGuard/Tailscale).

## Using it
- **Set a location label** (✎ in the header) whenever you change how the
  machine is connected — "ethernet via dock", "ethernet from wall", "wifi
  living room". The insights section then compares reliability per label:
  that comparison is how you convict the dock.
- **Add notes** ("swapped cable", "rebooted router", "ISP tech visited") from
  the settings drawer; they become chart markers.
- The timeline ribbon colors outages by layer:
  **link** = cable/dock/adapter/WiFi · **router** = your LAN ·
  **internet** = the ISP · **degraded** = high loss/latency. Tap an outage
  for details including the traceroute taken during it.
- Passcode: when protection is on, other devices must enter the passcode to
  view or change anything, then stay logged in. The host machine (localhost) is
  always trusted and is where you reset the passcode or toggle protection.
  Manage all of this under **Settings → Security**.
- Export CSV from the settings drawer — outage logs make good evidence for
  ISP support calls.

## Reading the verdict

| Symptom in the data | Culprit |
|---|---|
| Outages at the **link** layer, adapter down | cable, dock, port, or WiFi |
| Router unreachable but link up | router / LAN |
| Router pings fine, internet targets dead | ISP |
| Pings fine, DNS failing | DNS (set 1.1.1.1/8.8.8.8 in the router) |
| Ethernet renegotiates 1 Gbps → 100 Mbps | failing cable/port |
| Outages only while this PC saturates the link | this computer / bufferbloat |
| Everything green but apps struggle | this computer, not the network |

Also check the diagnostics panel (settings drawer): Windows' NIC
power-saving option is a classic cause of intermittent ethernet drops.

## More machines

Repeat the install on each machine (any mix of Windows/Linux/Pi — the
monitor is per-machine). Add the others' URLs to `config.json` →
`"peers": ["http://192.168.1.20:8745"]` and a machine switcher appears in
the header.

## Config

`config.json` (created on first run): probe targets and intervals, speed
test cadence (`speedtest_interval_min`, 0 = disabled), retention
(`retention_days` before raw samples downsample to 1-minute aggregates —
outages and speed tests are kept forever), `peers`, port. To simulate an
outage while testing, add a non-routable IP to `extra_targets`
(e.g. `"192.0.2.1"`).

## Security notes

- **Passcode**: a numeric PIN (4+ digits), stored as a scrypt hash. When
  enabled, remote devices must log in to view or control; a signed HttpOnly
  SameSite=Strict session cookie keeps them logged in, and login attempts are
  rate-limited. The passcode is required for **everything** from other devices,
  not just changes.
- **Localhost is superuser**: requests over loopback (browsing
  `http://localhost:8745` on the host) never need the passcode and are the only
  way to reset it or enable/disable protection — the forgot-passcode escape
  hatch. Reach the host by SSH and use `localhost` if it's headless. Hitting the
  machine's LAN IP (even from the host) counts as a remote device.
- Change the passcode, or turn protection on/off, from **Settings → Security**.
  From the CLI on the host: `python -m app.auth set-passcode` (sets + enables)
  or `python -m app.auth disable`.
- The server binds to your LAN; the firewall rule is restricted to the
  Private profile so nothing is exposed on public networks. Don't
  port-forward it or put it behind a public reverse proxy; if you need
  remote access, use a VPN into your home network.
- The app talks to the internet only for probes (ping/DNS/HTTP checks,
  api.ipify.org) and Cloudflare's speed-test endpoint. All data stays in
  `whatthebit.db` on this machine.

## Future ideas (not built)

Windows toast notifications on recovery · Event Log correlation (driver
resets) · a central aggregation view across machines.
