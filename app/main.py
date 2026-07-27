"""FastAPI app: REST API, SSE live stream, static dashboard.

Run:  python -m app.main   (or: uvicorn app.main:app)
For a home LAN. When a passcode is set (auth_enabled), remote devices must log
in to view or control; the host machine itself (localhost) is always exempt and
can reset the passcode. Don't expose it to the internet. See app/auth.py.
"""
import asyncio
import csv
import io
import json
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, collector as collector_mod, config, db, netinfo

cfg = config.load()
collector: collector_mod.Collector | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global collector
    db.init()
    collector = collector_mod.Collector()
    collector.start()
    yield
    collector.shutdown()


app = FastAPI(title="Go Web, Go!", lifespan=lifespan)


# ---------------------------------------------------------------- auth gate

def _authorized(request: Request) -> bool:
    """Loopback is always allowed; otherwise open unless a passcode is on,
    in which case a valid session is required (CSRF on writes)."""
    if auth.is_local(request):
        return True
    if not config.load().get("auth_enabled"):
        return True
    require_csrf = request.method not in ("GET", "HEAD", "OPTIONS")
    return auth.validate_session(request.cookies.get("wtb_session"),
                                 request.headers.get("X-CSRF"), require_csrf)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        if not _authorized(request):
            return JSONResponse({"detail": "login required"}, status_code=401)
    response = await call_next(request)
    # This is a fast-iterating local tool: never let the browser serve a
    # stale app shell / script from cache. no-cache = revalidate every load
    # (cheap on a LAN; returns 304 when unchanged), so edits always show up.
    if not path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ---------------------------------------------------------------- helpers

def _range(t_from: float | None, t_to: float | None) -> tuple[float, float]:
    now = time.time()
    t_to = t_to or now
    t_from = t_from or (t_to - 6 * 3600)
    if t_from >= t_to:
        raise HTTPException(status_code=400, detail="bad time range")
    return t_from, t_to


def _bucket_for(t_from: float, t_to: float, bucket: float | None) -> float:
    if bucket:
        return max(1.0, bucket)
    span = t_to - t_from
    return max(3.0, span / 400)  # ~400 points per chart


# ---------------------------------------------------------------- public API

def _full_status() -> dict:
    snap = collector.snapshot()
    c = config.load()
    snap["server_time"] = time.time()
    snap["machine_name"] = c["machine_name"]
    snap["last_speedtest"] = db.last_speedtest()
    snap["known_labels"] = db.known_labels()
    snap["peers"] = c["peers"]
    return snap


@app.get("/api/status")
def status():
    return _full_status()


@app.get("/api/metrics")
def metrics(t_from: float | None = Query(None, alias="from"),
            t_to: float | None = Query(None, alias="to"),
            bucket: float | None = None):
    t_from, t_to = _range(t_from, t_to)
    b = _bucket_for(t_from, t_to, bucket)
    return {
        "bucket": b,
        "ping": db.ping_series(t_from, t_to, b),
        "throughput": db.throughput_series(t_from, t_to, b),
        "dns": db.dns_series(t_from, t_to, b),
        "notes": db.notes_in_range(t_from, t_to),
        "interface_events": db.interface_events(t_from, t_to),
        "wan_ip_changes": db.wan_ip_changes(t_from, t_to),
    }


@app.get("/api/timeline")
def timeline(t_from: float | None = Query(None, alias="from"),
             t_to: float | None = Query(None, alias="to"),
             bucket: float | None = None):
    """Uptime ribbon: per bucket -> healthy/degraded/outage/not monitoring."""
    t_from, t_to = _range(t_from, t_to)
    b = _bucket_for(t_from, t_to, bucket)
    c = config.load()
    monitored = db.monitored_intervals(t_from, t_to)
    outages = db.outages_in_range(t_from, t_to)
    loss = db.loss_by_bucket(t_from, t_to, b,
                             c["internet_targets"])

    def state_at(bs: float, be: float) -> str | None:
        if not any(s < be and e > bs for s, e in monitored):
            return None  # not monitoring
        for o in outages:
            oe = o["end_ts"] or time.time()
            if o["start_ts"] < be and oe > bs:
                return "outage:" + o["layer"]
        pct = loss.get(bs)
        if pct is not None and pct >= c["degraded_loss_pct"]:
            return "degraded"
        return "ok"

    cells = []
    bs = (t_from // b) * b
    while bs < t_to:
        cells.append({"t": bs, "state": state_at(bs, bs + b)})
        bs += b
    return {"bucket": b, "cells": cells,
            "labels": db.labels_in_range(t_from, t_to),
            "outages": outages}


@app.get("/api/outages")
def outages(t_from: float | None = Query(None, alias="from"),
            t_to: float | None = Query(None, alias="to")):
    t_from, t_to = _range(t_from, t_to)
    return db.outages_in_range(t_from, t_to)


@app.get("/api/speedtests")
def speedtests(t_from: float | None = Query(None, alias="from"),
               t_to: float | None = Query(None, alias="to")):
    t_from, t_to = _range(t_from, t_to)
    return db.speedtests_in_range(t_from, t_to)


@app.get("/api/labels")
def labels():
    cur = db.current_label()
    return {"current": dict(cur) if cur else None,
            "known": db.known_labels()}


@app.get("/api/notes")
def notes(t_from: float | None = Query(None, alias="from"),
          t_to: float | None = Query(None, alias="to")):
    t_from, t_to = _range(t_from, t_to)
    return db.notes_in_range(t_from, t_to)


@app.get("/api/diagnostics")
def diagnostics():
    return netinfo.adapter_diagnostics()


# ---------------------------------------------------------------- insights

def _fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


@app.get("/api/insights")
def insights(t_from: float | None = Query(None, alias="from"),
             t_to: float | None = Query(None, alias="to")):
    t_from, t_to = _range(t_from, t_to)
    now = time.time()
    monitored = db.monitored_intervals(t_from, t_to)
    monitored_secs = sum(e - s for s, e in monitored)
    outs = db.outages_in_range(t_from, t_to)

    def dur(o) -> float:
        return (o["end_ts"] or now) - o["start_ts"]

    outage_secs = sum(dur(o) for o in outs)
    uptime_pct = (100.0 * (1 - outage_secs / monitored_secs)
                  if monitored_secs > 0 else None)

    by_layer: dict[str, int] = {}
    for o in outs:
        by_layer[o["layer"]] = by_layer.get(o["layer"], 0) + 1

    # heatmap: outage minutes by hour-of-day x day-of-week (local time)
    heat = [[0.0] * 24 for _ in range(7)]
    for o in outs:
        t = o["start_ts"]
        end = min(o["end_ts"] or now, t_to)
        while t < end:
            lt = time.localtime(t)
            step = min(3600 - (lt.tm_min * 60 + lt.tm_sec), end - t)
            heat[lt.tm_wday][lt.tm_hour] += step / 60
            t += step

    # per-label comparison
    labels = db.labels_in_range(t_from, t_to)
    label_stats: dict[str, dict] = {}
    for lb in labels:
        s = max(lb["start_ts"], t_from)
        e = min(lb["end_ts"] or now, t_to)
        if e <= s:
            continue
        st = label_stats.setdefault(
            lb["label"], {"label": lb["label"], "seconds": 0.0,
                          "drops": 0, "outage_seconds": 0.0})
        st["seconds"] += e - s
        for o in outs:
            if s <= o["start_ts"] < e:
                st["drops"] += 1
                st["outage_seconds"] += min(dur(o), e - o["start_ts"])
    comparison = []
    for st in label_stats.values():
        days = st["seconds"] / 86400
        st["drops_per_day"] = round(st["drops"] / days, 2) if days > 0 else None
        st["uptime_pct"] = round(
            100.0 * (1 - st["outage_seconds"] / st["seconds"]), 2)
        st["hours_monitored"] = round(st["seconds"] / 3600, 1)
        comparison.append(st)
    comparison.sort(key=lambda s: -(s["drops_per_day"] or 0))

    verdict = _verdict(outs, by_layer, comparison, monitored_secs, now)
    longest = max((dur(o) for o in outs), default=0)

    return {
        "monitored_hours": round(monitored_secs / 3600, 1),
        "uptime_pct": round(uptime_pct, 3) if uptime_pct is not None else None,
        "outage_count": len(outs),
        "outage_minutes": round(outage_secs / 60, 1),
        "longest_outage": _fmt_dur(longest) if outs else None,
        "avg_outage": _fmt_dur(outage_secs / len(outs)) if outs else None,
        "by_layer": by_layer,
        "heatmap": heat,
        "label_comparison": comparison,
        "verdict": verdict,
        "self_saturated_count": sum(1 for o in outs if o["self_saturated"]),
    }


def _verdict(outs, by_layer, comparison, monitored_secs, now) -> str:
    if monitored_secs < 600:
        return ("Not enough monitoring time yet — leave the monitor running "
                "and check back.")
    if not outs:
        return ("No outages recorded in this period. If things still feel "
                "broken, the problem is likely this computer (or a specific "
                "app), not the network.")
    total = len(outs)
    layer, count = max(by_layer.items(), key=lambda kv: kv[1])
    layer_name = {"link": "the link layer (cable, dock, adapter or WiFi)",
                  "lan": "the router / local network",
                  "internet": "the internet connection (ISP)",
                  "degraded": "degraded quality (loss/latency, not full drops)",
                  "dns": "DNS resolution"}.get(layer, layer)
    parts = [f"{total} outage{'s' if total != 1 else ''} recorded; "
             f"{count} of them at {layer_name}."]
    sat = sum(1 for o in outs if o["self_saturated"])
    if sat:
        parts.append(f"{sat} happened while this computer was saturating "
                     "the connection — those may be self-inflicted.")
    if len(comparison) >= 2 and comparison[0]["drops_per_day"]:
        worst, best = comparison[0], comparison[-1]
        if (best["drops_per_day"] is not None
                and worst["drops_per_day"] > 3 * max(0.1,
                                                     best["drops_per_day"])):
            parts.append(
                f"Setup '{worst['label']}' drops "
                f"{worst['drops_per_day']}/day vs "
                f"{best['drops_per_day']}/day for '{best['label']}' — "
                f"the evidence points at '{worst['label']}'.")
    if layer == "link":
        parts.append("Link-layer drops mean the cable, dock, port or WiFi — "
                     "not the ISP and not this computer.")
    elif layer == "internet":
        parts.append("The router stayed reachable while the internet "
                     "dropped — that points at the ISP. Export the data "
                     "and the outage log as evidence.")
    return " ".join(parts)


# ---------------------------------------------------------------- export

@app.get("/api/export")
def export(t_from: float | None = Query(None, alias="from"),
           t_to: float | None = Query(None, alias="to"),
           fmt: str = "csv", table: str = "outages"):
    t_from, t_to = _range(t_from, t_to)
    allowed = {
        "outages": lambda: db.outages_in_range(t_from, t_to),
        "speedtests": lambda: db.speedtests_in_range(t_from, t_to),
        "notes": lambda: db.notes_in_range(t_from, t_to),
        "pings": lambda: [dict(r) for r in db.query(
            "SELECT ts, target, rtt_ms FROM ping_samples "
            "WHERE ts BETWEEN ? AND ? ORDER BY ts", (t_from, t_to))],
    }
    if table not in allowed:
        raise HTTPException(status_code=400, detail="unknown table")
    rows = allowed[table]()
    for r in rows:
        r.pop("traceroute", None)
    if fmt == "json":
        return rows
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=gowebgo_{table}.csv"})


# ---------------------------------------------------------------- SSE

@app.get("/api/stream")
async def stream(request: Request):
    async def gen():
        while True:
            if await request.is_disconnected():
                return
            # full status, not the bare snapshot — the dashboard's tiles
            # (speed test result, machine name, labels) live off this stream
            yield f"data: {json.dumps(_full_status())}\n\n"
            await asyncio.sleep(2.0)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- auth

@app.get("/api/auth/state")
def auth_state(request: Request):
    c = config.load()
    local = auth.is_local(request)
    enabled = bool(c.get("auth_enabled"))
    authed = (local or not enabled
              or auth.validate_session(request.cookies.get("wtb_session"),
                                       None, require_csrf=False))
    return {"enabled": enabled, "configured": bool(c.get("password_hash")),
            "superuser": local, "authed": authed}


@app.post("/api/auth/login")
async def auth_login(request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    if not auth.is_local(request) and auth.rate_limited(ip):
        raise HTTPException(status_code=429,
                            detail="too many attempts — wait 5 minutes")
    body = await request.json()
    passcode = str(body.get("passcode", ""))
    c = config.load()
    if not c.get("password_hash"):
        raise HTTPException(status_code=503, detail="no passcode set")
    if not auth.verify_passcode(passcode, c["password_hash"]):
        auth.record_failure(ip)
        raise HTTPException(status_code=401, detail="wrong passcode")
    auth.clear_failures(ip)
    token, csrf = auth.issue_session()
    response.set_cookie("wtb_session", token, httponly=True,
                        samesite="strict", max_age=auth.SESSION_HOURS * 3600)
    return {"ok": True, "csrf": csrf}


@app.post("/api/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie("wtb_session")
    return {"ok": True}


def _authed_remote(request: Request) -> bool:
    return auth.validate_session(request.cookies.get("wtb_session"),
                                 request.headers.get("X-CSRF"), True)


@app.post("/api/auth/change")
async def auth_change(request: Request):
    """Change the passcode. Superuser (localhost) may reset without the old
    one; a remote device must be logged in and supply the current passcode."""
    body = await request.json()
    new = str(body.get("new", ""))
    c = config.load()
    local = auth.is_local(request)
    if not local:
        if not _authed_remote(request):
            raise HTTPException(status_code=401, detail="login required")
        if not auth.verify_passcode(str(body.get("current", "")),
                                    c.get("password_hash")):
            raise HTTPException(status_code=403, detail="wrong current passcode")
    if not auth.valid_passcode(new):
        raise HTTPException(status_code=400,
                            detail="passcode must be at least 4 digits")
    config.update(password_hash=auth.hash_passcode(new))
    return {"ok": True}


@app.post("/api/auth/enable")
async def auth_enable(request: Request):
    """Turn protection on. Everyone (superuser included) must supply the
    passcode. The only exception is first-time setup on a machine with no
    passcode yet, which is allowed from the host and establishes it. Changing
    an existing passcode is done via /api/auth/change (the Reset button)."""
    body = await request.json()
    passcode = str(body.get("passcode", ""))
    c = config.load()
    if not c.get("password_hash"):
        # first-time setup — only from the host machine
        if not auth.is_local(request):
            raise HTTPException(
                status_code=403,
                detail="no passcode set — set one on the host machine first")
        if not auth.valid_passcode(passcode):
            raise HTTPException(status_code=400,
                                detail="passcode must be at least 4 digits")
        config.update(password_hash=auth.hash_passcode(passcode),
                      auth_enabled=True)
        return {"ok": True}
    if not auth.verify_passcode(passcode, c["password_hash"]):
        raise HTTPException(status_code=403, detail="wrong passcode")
    config.update(auth_enabled=True)
    return {"ok": True}


@app.post("/api/auth/disable")
async def auth_disable(request: Request):
    """Turn protection off. Everyone (superuser included) must confirm with the
    current passcode."""
    body = await request.json()
    c = config.load()
    if not auth.verify_passcode(str(body.get("passcode", "")),
                                c.get("password_hash")):
        raise HTTPException(status_code=403, detail="wrong passcode")
    config.update(auth_enabled=False)
    return {"ok": True}


# ---------------------------------------------------------------- control

@app.post("/api/monitor/start")
def monitor_start():
    collector.set_monitoring(True)
    return {"ok": True}


@app.post("/api/monitor/pause")
def monitor_pause():
    collector.set_monitoring(False)
    return {"ok": True}


@app.post("/api/label")
async def set_label(request: Request):
    body = await request.json()
    label = str(body.get("label", "")).strip()[:80]
    if not label:
        raise HTTPException(status_code=400, detail="empty label")
    collector.set_label(label)
    return {"ok": True, "label": label}


@app.post("/api/note")
async def add_note(request: Request):
    body = await request.json()
    text = str(body.get("text", "")).strip()[:500]
    if not text:
        raise HTTPException(status_code=400, detail="empty note")
    ts = float(body.get("ts") or time.time())
    return {"ok": True, "id": db.add_note(ts, text)}


@app.post("/api/outage/note")
async def set_outage_note(request: Request):
    body = await request.json()
    try:
        outage_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad outage id")
    text = str(body.get("text", "")).strip()[:500]
    if not db.set_outage_note(outage_id, text):
        raise HTTPException(status_code=404, detail="no such outage")
    return {"ok": True, "id": outage_id, "text": text}


@app.post("/api/speedtest/run")
def run_speedtest():
    import threading
    threading.Thread(target=collector.run_speedtest, daemon=True).start()
    return {"ok": True, "started": True}


# ---------------------------------------------------------------- alerts

@app.get("/api/alerts/rules")
def alert_rules():
    return db.list_alert_rules()


@app.post("/api/alerts/rules")
async def add_alert_rule(request: Request):
    body = await request.json()
    metric = str(body.get("metric", ""))
    if metric not in collector_mod.ALERT_METRICS:
        raise HTTPException(status_code=400, detail="unknown metric")
    try:
        threshold = float(body.get("threshold"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad threshold")
    duration = 0
    if metric in collector_mod.LIVE_METRICS:
        try:
            duration = max(0, int(body.get("duration_s") or 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="bad duration")
    rid = db.add_alert_rule(metric, threshold, duration)
    return {"ok": True, "id": rid}


@app.delete("/api/alerts/rules/{rule_id}")
def delete_alert_rule(rule_id: int):
    db.delete_alert_rule(rule_id)
    return {"ok": True}


@app.get("/api/alerts/events")
def alert_events(limit: int = 50):
    return db.alert_events_recent(max(1, min(500, limit)))


@app.delete("/api/alerts/events")
def clear_alert_events():
    return {"ok": True, "deleted": db.clear_alert_events()}


@app.delete("/api/alerts/events/{event_id}")
def delete_alert_event(event_id: int):
    db.delete_alert_event(event_id)
    return {"ok": True}


@app.get("/api/alerts/config")
def get_alert_config():
    return {"retention_days": config.load().get("alert_retention_days", 90)}


@app.post("/api/alerts/config")
async def set_alert_config(request: Request):
    body = await request.json()
    try:
        days = int(body.get("retention_days"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad retention_days")
    if days not in (0, 30, 60, 90):
        raise HTTPException(status_code=400,
                            detail="retention must be 30, 60, 90, or 0 (never)")
    config.update(alert_retention_days=days)
    return {"ok": True, "retention_days": days}


@app.delete("/api/data")
def clear_data(t_from: float = Query(..., alias="from"),
               t_to: float = Query(..., alias="to"),
               include_notes_labels: bool = False):
    if t_from >= t_to:
        raise HTTPException(status_code=400, detail="bad time range")
    return {"ok": True,
            "deleted": db.clear_range(t_from, t_to, include_notes_labels)}


# ---------------------------------------------------------------- static

app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True),
          name="static")


def main() -> None:
    c = config.load()
    port = c["port"]
    lan = netinfo.local_ip()
    print()
    print("  Go, Web, Go! is currently monitoring network traffic...")
    print()
    print("  Monitor your dashboard for mission-critical events:")
    print(f"    On this machine:   http://localhost:{port}")
    if lan:
        print(f"    From your network: http://{lan}:{port}")
    print()
    print(" Mission Control is now monitoring network activity...")
    print()
    print("  Keep this window open while monitoring. Press Ctrl+C to stop.")
    print(flush=True)
    # timeout_graceful_shutdown: force-close the never-ending SSE stream on
    # Ctrl+C instead of waiting forever for it to finish (that wait is why
    # Ctrl+C appeared to hang).
    uvicorn.run("app.main:app", host=c["bind"], port=c["port"],
                log_level="warning", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
