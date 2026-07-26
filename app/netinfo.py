"""Platform layer: active network adapter, link state, gateway discovery.

Windows uses PowerShell (Get-NetAdapter, Get-NetRoute) and netsh; Linux/Pi
reads /sys/class/net and uses `ip route` / iwgetid. Everything here is
read-only and unprivileged.
"""
import re
import socket
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"

# Hide console windows spawned by subprocess on Windows
_STARTUPINFO = None
if IS_WINDOWS:
    _STARTUPINFO = subprocess.STARTUPINFO()
    _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW


def _run(args: list[str], timeout: float = 10.0) -> str:
    try:
        out = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            startupinfo=_STARTUPINFO)
        return out.stdout or ""
    except (subprocess.SubprocessError, OSError):
        return ""


def _ps(script: str, timeout: float = 10.0) -> str:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 script], timeout)


# ---------------------------------------------------------------- gateway

def default_gateway() -> str | None:
    if IS_WINDOWS:
        out = _ps("(Get-NetRoute -DestinationPrefix 0.0.0.0/0 | "
                  "Sort-Object RouteMetric | Select-Object -First 1)."
                  "NextHop")
        gw = out.strip().splitlines()[0].strip() if out.strip() else ""
        return gw or None
    out = _run(["ip", "route", "show", "default"])
    m = re.search(r"default via (\S+)", out)
    return m.group(1) if m else None


def local_ip() -> str | None:
    """IP of the interface that would route to the internet (no packets sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("1.1.1.1", 53))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


# ---------------------------------------------------------------- adapter

def _windows_adapter() -> dict:
    out = _ps(
        "Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | "
        "Sort-Object -Property @{Expression={$_.MediaType -eq '802.3'};"
        "Descending=$true} | Select-Object -First 1 | "
        "ForEach-Object { \"$($_.Name)`t$($_.MediaType)`t$($_.LinkSpeed)\" }")
    line = out.strip()
    if not line:
        return {"adapter": None, "conn_type": None, "ssid": None,
                "link_mbps": None, "is_up": False, "ip": None,
                "wifi_signal_pct": None}
    name, media, speed = (line.split("\t") + [None, None])[:3]
    conn_type = "ethernet" if media and "802.3" in media else "wifi"
    link_mbps = _parse_link_speed(speed or "")
    ssid = signal = None
    if conn_type == "wifi":
        wl = _run(["netsh", "wlan", "show", "interfaces"])
        m = re.search(r"^\s*SSID\s*:\s*(.+)$", wl, re.MULTILINE)
        ssid = m.group(1).strip() if m else None
        m = re.search(r"^\s*Signal\s*:\s*(\d+)\s*%", wl, re.MULTILINE)
        signal = float(m.group(1)) if m else None
    return {"adapter": name, "conn_type": conn_type, "ssid": ssid,
            "link_mbps": link_mbps, "is_up": True, "ip": local_ip(),
            "wifi_signal_pct": signal}


def _parse_link_speed(text: str) -> float | None:
    m = re.search(r"([\d.]+)\s*(G|M|K)?bps", text, re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "M").upper()
    return val * {"G": 1000.0, "M": 1.0, "K": 0.001}[unit]


def _linux_adapter() -> dict:
    out = _run(["ip", "route", "show", "default"])
    m = re.search(r"dev (\S+)", out)
    if not m:
        return {"adapter": None, "conn_type": None, "ssid": None,
                "link_mbps": None, "is_up": False, "ip": None,
                "wifi_signal_pct": None}
    dev = m.group(1)
    is_wifi = bool(_run(["test", "-d", f"/sys/class/net/{dev}/wireless"]) == ""
                   and __import__("os").path.isdir(
                       f"/sys/class/net/{dev}/wireless"))
    link_mbps = None
    try:
        with open(f"/sys/class/net/{dev}/speed") as f:
            v = float(f.read().strip())
            link_mbps = v if v > 0 else None
    except (OSError, ValueError):
        pass
    ssid = None
    signal = None
    if is_wifi:
        ssid = _run(["iwgetid", "-r"]).strip() or None
        iw = _run(["iwconfig", dev])
        m2 = re.search(r"Link Quality=(\d+)/(\d+)", iw)
        if m2:
            signal = 100.0 * int(m2.group(1)) / int(m2.group(2))
    up = False
    try:
        with open(f"/sys/class/net/{dev}/operstate") as f:
            up = f.read().strip() == "up"
    except OSError:
        pass
    return {"adapter": dev, "conn_type": "wifi" if is_wifi else "ethernet",
            "ssid": ssid, "link_mbps": link_mbps, "is_up": up,
            "ip": local_ip(), "wifi_signal_pct": signal}


def active_adapter() -> dict:
    """State of the adapter currently carrying the default route."""
    return _windows_adapter() if IS_WINDOWS else _linux_adapter()


# ---------------------------------------------------------------- traceroute

def traceroute(target: str, max_hops: int = 12,
               timeout: float = 60.0) -> list[dict]:
    """Run system traceroute; returns [{hop, ip, ms}] (ms/ip None = timeout)."""
    if IS_WINDOWS:
        args = ["tracert", "-d", "-h", str(max_hops), "-w", "1000", target]
    else:
        args = ["traceroute", "-n", "-m", str(max_hops), "-w", "1", target]
    out = _run(args, timeout=timeout)
    hops = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(.*)", line)
        if not m:
            continue
        hop_no, rest = int(m.group(1)), m.group(2)
        ip_m = re.search(r"(\d+\.\d+\.\d+\.\d+)", rest)
        ms_m = re.search(r"([\d.]+)\s*ms", rest)
        hops.append({"hop": hop_no,
                     "ip": ip_m.group(1) if ip_m else None,
                     "ms": float(ms_m.group(1)) if ms_m else None})
    return hops


def discover_isp_hop(gateway: str | None) -> str | None:
    """First responding hop past the local gateway — the ISP's edge."""
    hops = traceroute("1.1.1.1", max_hops=5)
    seen_gateway = False
    for h in hops:
        if h["ip"] is None:
            continue
        if h["ip"] == gateway or _is_private(h["ip"]):
            seen_gateway = True
            continue
        if seen_gateway or h["hop"] > 1:
            return h["ip"]
    return None


def _is_private(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    a, b = int(parts[0]), int(parts[1])
    return (a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
            or a == 127 or (a == 169 and b == 254) or (a == 100 and 64 <= b <= 127))


# ---------------------------------------------------------------- ping

def ping_once(target: str, timeout_ms: int = 1500) -> float | None:
    """One system ping; returns RTT in ms, or None if lost/failed."""
    if IS_WINDOWS:
        args = ["ping", "-n", "1", "-w", str(timeout_ms), target]
    else:
        args = ["ping", "-c", "1", "-W",
                str(max(1, round(timeout_ms / 1000))), target]
    out = _run(args, timeout=timeout_ms / 1000 + 3)
    # Windows: "time=12ms" or "time<1ms"; Linux: "time=12.3 ms"
    m = re.search(r"[Tt]ime[=<]\s*([\d.]+)\s*ms", out)
    if m:
        return float(m.group(1))
    # Windows localized fallback: any "<n>ms TTL=" pattern
    m = re.search(r"([\d.]+)\s*ms.*TTL=", out)
    if m and ("TTL=" in out or "ttl=" in out):
        return float(m.group(1))
    return None


# ---------------------------------------------------------------- diagnostics

def adapter_diagnostics() -> dict:
    """Read-only driver/power info for the diagnostics panel."""
    info = {"power_saving": None, "driver_date": None, "driver_version": None,
            "hint": None}
    if not IS_WINDOWS:
        return info
    adp = active_adapter()
    if not adp["adapter"]:
        return info
    name = adp["adapter"].replace("'", "''")
    out = _ps(
        f"$a = Get-NetAdapter -Name '{name}'; "
        f"$pm = Get-NetAdapterPowerManagement -Name '{name}' "
        f"-ErrorAction SilentlyContinue; "
        "\"$($pm.AllowComputerToTurnOffDevice)`t"
        "$($a.DriverDate)`t$($a.DriverVersion)\"")
    parts = (out.strip().split("\t") + ["", "", ""])[:3]
    allow_off = parts[0].strip()
    info["power_saving"] = (allow_off.lower() in ("enabled", "true")
                            if allow_off else None)
    info["driver_date"] = parts[1].strip() or None
    info["driver_version"] = parts[2].strip() or None
    if info["power_saving"]:
        info["hint"] = (
            "Windows is allowed to turn off this adapter to save power — a "
            "very common cause of intermittent drops. In Device Manager → "
            "adapter → Power Management, untick 'Allow the computer to turn "
            "off this device to save power'.")
    return info
