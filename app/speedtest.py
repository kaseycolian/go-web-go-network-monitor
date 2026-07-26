"""Speed test against Cloudflare's speed endpoints (no CLI dependency).

Download: GET  https://speed.cloudflare.com/__down?bytes=N
Upload:   POST https://speed.cloudflare.com/__up

While each direction runs, a side thread keeps pinging so we can measure
latency under load; the inflation vs idle latency yields a bufferbloat
grade A-F (waveform.com-style thresholds).
"""
import statistics
import threading
import time

import httpx

from . import netinfo

DOWN_URL = "https://speed.cloudflare.com/__down"
UP_URL = "https://speed.cloudflare.com/__up"
CHUNK = 64 * 1024


def _idle_latency(target: str, samples: int = 5) -> float | None:
    vals = [r for _ in range(samples)
            if (r := netinfo.ping_once(target, 1000)) is not None]
    return statistics.median(vals) if vals else None


class _LoadPinger:
    def __init__(self, target: str) -> None:
        self.target = target
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            r = netinfo.ping_once(self.target, 2000)
            if r is not None:
                self.samples.append(r)
            self._stop.wait(0.3)

    def __enter__(self) -> "_LoadPinger":
        self._t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._t.join(3)


def _measure_down(client: httpx.Client, max_seconds: float) -> float:
    """Download for up to max_seconds; returns Mbps.

    Cloudflare rejects ?bytes above ~75 MB (403), so loop 50 MB
    requests over the same keep-alive connection until time is up.
    """
    total = 0
    t0 = time.time()
    while time.time() - t0 < max_seconds:
        with client.stream("GET", DOWN_URL,
                           params={"bytes": 50_000_000}) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(CHUNK):
                total += len(chunk)
                if time.time() - t0 >= max_seconds:
                    break
    dt = max(0.1, time.time() - t0)
    return total * 8 / dt / 1e6


def _measure_up(client: httpx.Client, max_seconds: float) -> float:
    """Upload for up to max_seconds; returns Mbps."""
    total = 0
    t0 = time.time()
    payload = b"\x00" * (1 * 1024 * 1024)
    while time.time() - t0 < max_seconds:
        client.post(UP_URL, content=payload).raise_for_status()
        total += len(payload)
    dt = max(0.1, time.time() - t0)
    return total * 8 / dt / 1e6


def _grade(idle_ms: float | None, loaded_ms: float | None) -> str | None:
    if idle_ms is None or loaded_ms is None:
        return None
    bloat = max(0.0, loaded_ms - idle_ms)
    for grade, limit in (("A+", 5), ("A", 30), ("B", 60), ("C", 200),
                         ("D", 400)):
        if bloat < limit:
            return grade
    return "F"


def run(max_seconds: float = 8, ping_target: str = "1.1.1.1") -> dict:
    """Full test; returns dict matching the speedtests table columns."""
    result: dict = {"ok": False, "down_mbps": None, "up_mbps": None,
                    "latency_ms": None, "loaded_latency_ms": None,
                    "grade": None, "error": None}
    try:
        result["latency_ms"] = _idle_latency(ping_target)
        loaded: list[float] = []
        with httpx.Client(timeout=15.0) as client:
            with _LoadPinger(ping_target) as p:
                result["down_mbps"] = round(
                    _measure_down(client, max_seconds), 2)
                loaded += p.samples
            with _LoadPinger(ping_target) as p:
                result["up_mbps"] = round(_measure_up(client, max_seconds), 2)
                loaded += p.samples
        if loaded:
            # bufferbloat shows in the upper tail, not the average
            loaded.sort()
            idx = max(0, int(len(loaded) * 0.95) - 1)
            result["loaded_latency_ms"] = round(loaded[idx], 1)
        result["grade"] = _grade(result["latency_ms"],
                                 result["loaded_latency_ms"])
        result["ok"] = True
    except httpx.HTTPError as e:
        result["error"] = f"{type(e).__name__}: {e}"
    except Exception as e:  # never let a speed test kill the collector
        result["error"] = f"{type(e).__name__}: {e}"
    return result
