"""Configuration for What the Bit.

Loads config.json from the project root (one level above this package).
Creates it with sane defaults on first run.
"""
import json
import os
import secrets
import socket
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
DB_PATH = os.path.join(PROJECT_ROOT, "whatthebit.db")
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")

_lock = threading.Lock()

DEFAULTS = {
    "machine_name": socket.gethostname(),
    "port": 8745,
    "bind": "0.0.0.0",
    # Probe cadence (seconds)
    "ping_interval": 3,
    "ping_timeout_ms": 1500,
    "dns_interval": 15,
    "dns_hostname": "www.google.com",
    "http_interval": 30,
    "http_url": "http://connectivitycheck.gstatic.com/generate_204",
    "wan_ip_interval": 300,
    "wan_ip_url": "https://api.ipify.org",
    "interface_interval": 5,
    "throughput_interval": 2,
    # Internet probe targets (gateway + ISP first hop are discovered at runtime)
    "internet_targets": ["1.1.1.1", "8.8.8.8"],
    # Extra targets, e.g. a non-routable IP for outage simulation during testing
    "extra_targets": [],
    "speedtest_interval_min": 30,
    "speedtest_max_seconds": 8,
    # Outage detection
    "fail_threshold": 3,        # consecutive failures to open an outage
    "recover_threshold": 3,     # consecutive successes to close it
    "degraded_loss_pct": 20,    # rolling loss % that counts as degraded
    "degraded_latency_ms": 250, # sustained internet latency that counts as degraded
    # Retention
    "retention_days": 30,
    # Auto-delete logged alerts after this many days (0 = keep forever)
    "alert_retention_days": 90,
    # Other machines running What the Bit, e.g. ["http://192.168.1.20:8745"]
    "peers": [],
    # Auth: numeric passcode (scrypt hash) required from remote devices when
    # auth_enabled. The host machine (localhost) is always exempt. session_secret
    # is auto-generated on first load. See app/auth.py.
    "password_hash": None,
    "auth_enabled": False,
    "session_secret": None,
}


def _write(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def load() -> dict:
    with _lock:
        cfg = dict(DEFAULTS)
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass  # fall back to defaults rather than crash the monitor
        changed = False
        if not cfg.get("session_secret"):
            cfg["session_secret"] = secrets.token_hex(32)
            changed = True
        if changed or not os.path.exists(CONFIG_PATH):
            _write(cfg)
        return cfg


def save(cfg: dict) -> None:
    with _lock:
        _write(cfg)


def update(**kwargs) -> dict:
    cfg = load()
    cfg.update(kwargs)
    save(cfg)
    return cfg
