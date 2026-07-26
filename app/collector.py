"""Background collection threads and the outage detector.

One Collector owns all probe threads. Probes write raw samples to SQLite and
update `self.live` (the snapshot streamed to dashboards over SSE). The
detector thread turns consecutive probe failures into classified outages:

    link      adapter/link down            -> cable / dock / WiFi
    lan       gateway unreachable          -> router / LAN
    internet  gateway OK, internet dead    -> ISP
    dns       pings fine, resolution fails -> DNS
    degraded  high loss or latency         -> flaky, not fully down
"""
import collections
import socket
import threading
import time

import httpx
import psutil

from . import config, db, netinfo, speedtest

FAIL = object()  # sentinel for a lost probe in rolling windows

# Alert metrics. Live metrics are evaluated every detector tick and support a
# sustained duration; speed-test metrics are evaluated per completed test.
LIVE_METRICS = {"latency_ms", "loss_pct", "quality"}
SPEEDTEST_METRICS = {"speedtest_down", "speedtest_up"}
ALERT_METRICS = LIVE_METRICS | SPEEDTEST_METRICS
# metrics where the alert fires when the value drops BELOW the threshold
BELOW_METRICS = {"quality", "speedtest_down", "speedtest_up"}


def alert_message(metric: str, value: float, threshold: float) -> str:
    d = "below" if metric in BELOW_METRICS else "above"
    if metric == "latency_ms":
        return f"Latency {value:.0f} ms ({d} {threshold:.0f} ms)"
    if metric == "loss_pct":
        return f"Packet loss {value:.0f}% ({d} {threshold:.0f}%)"
    if metric == "quality":
        return f"Quality {value:.0f} ({d} {threshold:.0f})"
    if metric == "speedtest_down":
        return f"Download {value:.1f} Mbps ({d} {threshold:.1f} Mbps)"
    if metric == "speedtest_up":
        return f"Upload {value:.1f} Mbps ({d} {threshold:.1f} Mbps)"
    return metric


class Collector:
    def __init__(self) -> None:
        self.cfg = config.load()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.session_id: int | None = None
        self.monitoring = db.meta_get("monitoring", "1") == "1"

        self.gateway: str | None = None
        self.isp_hop: str | None = None
        self.targets: list[str] = []

        # rolling result windows per target (True=ok entries are rtt floats)
        self.window: dict[str, collections.deque] = {}
        self.consec_fail: dict[str, int] = {}
        self.consec_ok: dict[str, int] = {}

        self.adapter_state: dict = {}
        self.current_outage: dict | None = None  # {id, layer, start_ts}
        self.degraded_since: float | None = None
        # per-rule alert tracking: rule_id -> {since, event_id, peak}
        self.alert_state: dict[int, dict] = {}
        # some routers never answer ICMP ("General failure" / silent drop);
        # only blame the LAN layer if the gateway has proven pingable
        self.gateway_pingable = False

        self.live: dict = {
            "monitoring": self.monitoring, "adapter": None, "conn_type": None,
            "ssid": None, "link_mbps": None, "is_up": None,
            "wifi_signal_pct": None, "rx_bps": 0.0, "tx_bps": 0.0,
            "latency": {}, "loss_pct": None, "jitter_ms": None,
            "dns_ms": None, "http_ok": None, "gateway": None, "isp_hop": None,
            "outage": None, "quality": None, "label": None,
            "label_since": None,
            "speedtest_running": False, "wan_ip": None, "active_alerts": 0,
        }
        self.threads: list[threading.Thread] = []
        self.last_speedtest_ts = 0.0
        self._own_throughput_recent = collections.deque(maxlen=30)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        db.init()
        db.repair_dangling_sessions()
        self.gateway = netinfo.default_gateway()
        self._rebuild_targets()
        if self.monitoring:
            self.session_id = db.start_session(time.time())
        cur = db.current_label()
        with self.lock:
            self.live["gateway"] = self.gateway
            self.live["label"] = cur["label"] if cur else None
            self.live["label_since"] = cur["start_ts"] if cur else None
        for fn in (self._ping_loop, self._dns_loop, self._http_loop,
                   self._throughput_loop, self._interface_loop,
                   self._wan_ip_loop, self._detector_loop,
                   self._speedtest_loop, self._maintenance_loop,
                   self._isp_hop_discovery):
            t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
            t.start()
            self.threads.append(t)

    def shutdown(self, reason: str = "shutdown") -> None:
        self.stop_event.set()
        if self.session_id is not None:
            db.end_session(self.session_id, time.time(), reason)

    def set_monitoring(self, on: bool) -> None:
        with self.lock:
            if on == self.monitoring:
                return
            self.monitoring = on
            self.live["monitoring"] = on
        db.meta_set("monitoring", "1" if on else "0")
        if on:
            self.session_id = db.start_session(time.time())
        elif self.session_id is not None:
            db.end_session(self.session_id, time.time(), "paused")
            self.session_id = None

    def _rebuild_targets(self) -> None:
        t = list(self.cfg["internet_targets"]) + list(self.cfg["extra_targets"])
        if self.gateway:
            t.insert(0, self.gateway)
        if self.isp_hop and self.isp_hop not in t:
            t.append(self.isp_hop)
        self.targets = t
        for tgt in t:
            self.window.setdefault(tgt, collections.deque(maxlen=40))
            self.consec_fail.setdefault(tgt, 0)
            self.consec_ok.setdefault(tgt, 0)

    def _sleep(self, seconds: float) -> bool:
        """Wait; True means keep running, False means stop requested."""
        return not self.stop_event.wait(seconds)

    # ------------------------------------------------------------- probes

    def _ping_loop(self) -> None:
        interval = self.cfg["ping_interval"]
        timeout_ms = self.cfg["ping_timeout_ms"]
        while self._sleep(0.1):
            if not self.monitoring:
                if not self._sleep(interval):
                    return
                continue
            start = time.time()
            results: dict[str, float | None] = {}
            threads = []

            def probe(tgt: str) -> None:
                results[tgt] = netinfo.ping_once(tgt, timeout_ms)

            for tgt in list(self.targets):
                th = threading.Thread(target=probe, args=(tgt,), daemon=True)
                th.start()
                threads.append(th)
            for th in threads:
                th.join(timeout_ms / 1000 + 5)
            ts = time.time()
            lat: dict[str, float | None] = {}
            for tgt, rtt in results.items():
                db.add_ping(ts, tgt, rtt)
                win = self.window[tgt]
                win.append(rtt if rtt is not None else FAIL)
                if rtt is None:
                    self.consec_fail[tgt] += 1
                    self.consec_ok[tgt] = 0
                else:
                    self.consec_ok[tgt] += 1
                    self.consec_fail[tgt] = 0
                    if tgt == self.gateway:
                        self.gateway_pingable = True
                lat[tgt] = rtt
            with self.lock:
                self.live["latency"] = lat
                self.live["loss_pct"] = self._rolling_loss(
                    self.cfg["internet_targets"])
                self.live["jitter_ms"] = self._rolling_jitter(
                    self.cfg["internet_targets"])
                self.live["quality"] = self._quality_score()
            if not self._sleep(max(0.2, interval - (time.time() - start))):
                return

    def _dns_loop(self) -> None:
        host = self.cfg["dns_hostname"]
        while self._sleep(self.cfg["dns_interval"]):
            if not self.monitoring:
                continue
            t0 = time.time()
            try:
                socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
                ms = (time.time() - t0) * 1000
            except OSError:
                ms = None
            db.add_dns(time.time(), ms)
            with self.lock:
                self.live["dns_ms"] = ms

    def _http_loop(self) -> None:
        url = self.cfg["http_url"]
        while self._sleep(self.cfg["http_interval"]):
            if not self.monitoring:
                continue
            t0 = time.time()
            try:
                r = httpx.get(url, timeout=5.0, follow_redirects=False)
                ok = r.status_code in (204, 200)
                ms = (time.time() - t0) * 1000
            except httpx.HTTPError:
                ok, ms = False, None
            db.add_http(time.time(), ok, ms)
            with self.lock:
                self.live["http_ok"] = ok

    def _throughput_loop(self) -> None:
        interval = self.cfg["throughput_interval"]
        prev = psutil.net_io_counters(pernic=True)
        prev_t = time.time()
        while self._sleep(interval):
            now = psutil.net_io_counters(pernic=True)
            now_t = time.time()
            dt = max(0.001, now_t - prev_t)
            adapter = self.adapter_state.get("adapter")
            if self.monitoring and adapter and adapter in now and adapter in prev:
                rx = (now[adapter].bytes_recv - prev[adapter].bytes_recv) * 8 / dt
                tx = (now[adapter].bytes_sent - prev[adapter].bytes_sent) * 8 / dt
                rx, tx = max(0.0, rx), max(0.0, tx)
                db.add_throughput(now_t, adapter, rx, tx)
                self._own_throughput_recent.append((now_t, rx, tx))
                with self.lock:
                    self.live["rx_bps"] = rx
                    self.live["tx_bps"] = tx
            prev, prev_t = now, now_t

    def _interface_loop(self) -> None:
        last_heartbeat = 0.0
        while self._sleep(self.cfg["interface_interval"]):
            if not self.monitoring:
                continue
            st = netinfo.active_adapter()
            ts = time.time()
            event = None
            old = self.adapter_state
            if not old:
                event = "start"
            elif st["is_up"] != old.get("is_up"):
                event = "link_up" if st["is_up"] else "link_down"
            elif st["adapter"] != old.get("adapter"):
                event = "adapter_change"
            elif (st["link_mbps"] and old.get("link_mbps")
                  and st["link_mbps"] != old["link_mbps"]):
                event = (f"renegotiated_{int(old['link_mbps'])}"
                         f"_to_{int(st['link_mbps'])}")
            elif st["ssid"] != old.get("ssid"):
                event = "ssid_change"
            if event or ts - last_heartbeat >= 30:
                db.add_interface_state(
                    ts, st["adapter"], st["conn_type"], st["ssid"],
                    st["link_mbps"], st["is_up"], st["ip"],
                    st["wifi_signal_pct"], event or "heartbeat")
                last_heartbeat = ts
            if event in ("adapter_change", "link_up", "start"):
                gw = netinfo.default_gateway()
                if gw and gw != self.gateway:
                    self.gateway = gw
                    self.isp_hop = None
                    self._rebuild_targets()
                    with self.lock:
                        self.live["gateway"] = gw
            self.adapter_state = st
            with self.lock:
                for k in ("adapter", "conn_type", "ssid", "link_mbps",
                          "is_up", "wifi_signal_pct"):
                    self.live[k] = st[k]

    def _wan_ip_loop(self) -> None:
        while self._sleep(self.cfg["wan_ip_interval"]):
            if not self.monitoring or self.live.get("outage"):
                continue
            try:
                r = httpx.get(self.cfg["wan_ip_url"], timeout=8.0)
                ip = r.text.strip()
            except httpx.HTTPError:
                continue
            if ip and len(ip) < 64:
                last = db.last_wan_ip()
                if last is None or last["ip"] != ip:
                    db.add_wan_ip(time.time(), ip)
                with self.lock:
                    self.live["wan_ip"] = ip

    def _isp_hop_discovery(self) -> None:
        if not self._sleep(5):
            return
        hop = netinfo.discover_isp_hop(self.gateway)
        if hop:
            self.isp_hop = hop
            self._rebuild_targets()
            with self.lock:
                self.live["isp_hop"] = hop

    # ------------------------------------------------------------- detector

    def _rolling_loss(self, targets: list[str]) -> float | None:
        sent = lost = 0
        for t in targets:
            for v in self.window.get(t, ()):
                sent += 1
                lost += v is FAIL
        return (100.0 * lost / sent) if sent >= 5 else None

    def _rolling_jitter(self, targets: list[str]) -> float | None:
        diffs = []
        for t in targets:
            prev = None
            for v in self.window.get(t, ()):
                if v is not FAIL:
                    if prev is not None:
                        diffs.append(abs(v - prev))
                    prev = v
        return sum(diffs) / len(diffs) if diffs else None

    def _rolling_latency(self, targets: list[str]) -> float | None:
        vals = [v for t in targets for v in self.window.get(t, ())
                if v is not FAIL]
        return sum(vals) / len(vals) if vals else None

    def _quality_score(self) -> int | None:
        """MOS-style 0-100 from internet latency, jitter and loss."""
        lat = self._rolling_latency(self.cfg["internet_targets"])
        jit = self._rolling_jitter(self.cfg["internet_targets"])
        loss = self._rolling_loss(self.cfg["internet_targets"])
        if lat is None or loss is None:
            return None
        score = 100.0
        score -= max(0.0, lat - 30) * 0.25        # latency above 30 ms
        score -= (jit or 0.0) * 0.8               # jitter hurts calls most
        score -= loss * 4.0                       # loss is brutal
        return int(max(0, min(100, round(score))))

    def _layer_down(self, targets: list[str]) -> bool:
        th = self.cfg["fail_threshold"]
        return bool(targets) and all(
            self.consec_fail.get(t, 0) >= th for t in targets)

    def _layer_up(self, targets: list[str]) -> bool:
        th = self.cfg["recover_threshold"]
        return any(self.consec_ok.get(t, 0) >= th for t in targets)

    def _classify(self) -> str | None:
        """Current outage layer, most specific first; None = healthy."""
        if self.adapter_state and not self.adapter_state.get("is_up", True):
            return "link"
        if not self._layer_down(self.cfg["internet_targets"]):
            # internet reachable -> no outage, even if the gateway ignores
            # ICMP (traffic flows through it either way)
            return None
        if (self.gateway and self.gateway_pingable
                and self._layer_down([self.gateway])):
            return "lan"
        return "internet"

    def _self_saturated(self) -> bool:
        last = db.last_speedtest()
        if not last or not last["down_mbps"]:
            return False
        cap_bps = max(last["down_mbps"], last["up_mbps"] or 0) * 1e6
        recent = [r for r in self._own_throughput_recent
                  if r[0] > time.time() - 30]
        if not recent:
            return False
        peak = max(max(rx, tx) for _, rx, tx in recent)
        return peak > 0.85 * cap_bps

    def _detector_loop(self) -> None:
        while self._sleep(2.0):
            if not self.monitoring:
                continue
            ts = time.time()
            layer = self._classify()
            loss = self._rolling_loss(self.cfg["internet_targets"])
            lat = self._rolling_latency(self.cfg["internet_targets"])

            if self.current_outage is None:
                if layer:
                    oid = db.open_outage(ts, layer,
                                         self_saturated=self._self_saturated())
                    self.current_outage = {"id": oid, "layer": layer,
                                           "start_ts": ts, "traced": False}
                elif (loss is not None
                      and (loss >= self.cfg["degraded_loss_pct"]
                           or (lat or 0) >= self.cfg["degraded_latency_ms"])):
                    if self.degraded_since is None:
                        self.degraded_since = ts
                    elif ts - self.degraded_since >= 30:
                        oid = db.open_outage(
                            self.degraded_since, "degraded",
                            self_saturated=self._self_saturated())
                        self.current_outage = {"id": oid, "layer": "degraded",
                                               "start_ts": self.degraded_since,
                                               "traced": False}
                else:
                    self.degraded_since = None
            else:
                cur = self.current_outage
                if layer and layer != cur["layer"] and cur["layer"] != "link":
                    db.update_outage(cur["id"], layer=layer)
                    cur["layer"] = layer
                if loss is not None:
                    db.update_outage(cur["id"], worst_loss_pct=loss)
                # auto-traceroute once, for internet-layer problems
                if (not cur["traced"] and cur["layer"] in ("internet",
                                                           "degraded")
                        and ts - cur["start_ts"] >= 10):
                    cur["traced"] = True
                    threading.Thread(
                        target=self._trace_outage, args=(cur["id"],),
                        daemon=True).start()
                recovered = (
                    (cur["layer"] == "link"
                     and self.adapter_state.get("is_up"))
                    or (cur["layer"] == "lan"
                        and (self._layer_up([self.gateway] if self.gateway
                                            else [])
                             or self._layer_up(self.cfg["internet_targets"])))
                    or (cur["layer"] in ("internet", "degraded")
                        and self._layer_up(self.cfg["internet_targets"])
                        and (loss is None
                             or loss < self.cfg["degraded_loss_pct"] / 2)))
                if recovered:
                    db.close_outage(cur["id"], ts)
                    self.current_outage = None
                    self.degraded_since = None

            with self.lock:
                self.live["outage"] = (
                    {"layer": self.current_outage["layer"],
                     "start_ts": self.current_outage["start_ts"]}
                    if self.current_outage else None)

            try:
                self._eval_live_alerts(ts)
            except Exception:
                pass  # a bad rule must never kill the detector

    def _trace_outage(self, outage_id: int) -> None:
        target = self.cfg["internet_targets"][0]
        hops = netinfo.traceroute(target)
        if hops:
            db.add_traceroute(outage_id, time.time(), target, hops)

    # ------------------------------------------------------------- alerts

    def _metric_value(self, metric: str) -> float | None:
        if metric == "latency_ms":
            return self._rolling_latency(self.cfg["internet_targets"])
        if metric == "loss_pct":
            return self._rolling_loss(self.cfg["internet_targets"])
        if metric == "quality":
            return self.live.get("quality")
        return None

    @staticmethod
    def _cond_met(metric: str, value: float, threshold: float) -> bool:
        if value is None:
            return False
        return (value <= threshold if metric in BELOW_METRICS
                else value >= threshold)

    @staticmethod
    def _worse(metric: str, a: float | None, b: float | None) -> float:
        if a is None:
            return b
        if b is None:
            return a
        return min(a, b) if metric in BELOW_METRICS else max(a, b)

    def _eval_live_alerts(self, ts: float) -> None:
        """Open/close alert events for sustained live-metric rules."""
        rules = [r for r in db.list_alert_rules()
                 if r["enabled"] and r["metric"] in LIVE_METRICS]
        active = set()
        for r in rules:
            rid = r["id"]
            active.add(rid)
            st = self.alert_state.setdefault(
                rid, {"since": None, "event_id": None, "peak": None})
            val = self._metric_value(r["metric"])
            if val is None:
                continue  # no data this tick; hold state
            if self._cond_met(r["metric"], val, r["threshold"]):
                peak = (val if st["since"] is None
                        else self._worse(r["metric"], st["peak"], val))
                changed = peak != st["peak"]
                if st["since"] is None:
                    st["since"] = ts
                st["peak"] = peak
                msg = alert_message(r["metric"], peak, r["threshold"])
                if (st["event_id"] is None
                        and ts - st["since"] >= r["duration_s"]):
                    st["event_id"] = db.open_alert_event(
                        rid, r["metric"], r["threshold"], peak,
                        st["since"], msg)
                elif st["event_id"] is not None and changed:
                    db.update_alert_event(st["event_id"], peak, msg)
            else:
                if st["event_id"] is not None:
                    db.close_alert_event(st["event_id"], ts)
                st.update(since=None, event_id=None, peak=None)
        # rules deleted/disabled while an event was open -> close and forget
        for rid in list(self.alert_state):
            if rid not in active:
                st = self.alert_state.pop(rid)
                if st.get("event_id"):
                    db.close_alert_event(st["event_id"], ts)
        self.live["active_alerts"] = db.count_open_alert_events()

    def _eval_speedtest_alerts(self, result: dict) -> None:
        """Log a point alert for each speed-test rule the result trips."""
        if not result.get("ok"):
            return
        ts = time.time()
        for r in db.list_alert_rules():
            if not r["enabled"] or r["metric"] not in SPEEDTEST_METRICS:
                continue
            val = (result.get("down_mbps") if r["metric"] == "speedtest_down"
                   else result.get("up_mbps"))
            if val is not None and val <= r["threshold"]:
                db.open_alert_event(
                    r["id"], r["metric"], r["threshold"], val, ts,
                    alert_message(r["metric"], val, r["threshold"]), end_ts=ts)
        self.live["active_alerts"] = db.count_open_alert_events()

    # ------------------------------------------------------------- speedtest

    def _speedtest_loop(self) -> None:
        while self._sleep(20.0):
            interval = config.load()["speedtest_interval_min"] * 60
            if (self.monitoring and interval > 0
                    and time.time() - self.last_speedtest_ts >= interval
                    and not self.live.get("outage")):
                self.run_speedtest()

    def run_speedtest(self) -> dict:
        with self.lock:
            if self.live["speedtest_running"]:
                return {"ok": False, "error": "already running"}
            self.live["speedtest_running"] = True
        try:
            self.last_speedtest_ts = time.time()
            result = speedtest.run(
                max_seconds=self.cfg["speedtest_max_seconds"],
                ping_target=self.cfg["internet_targets"][0])
            db.add_speedtest(
                time.time(), result.get("down_mbps"), result.get("up_mbps"),
                result.get("latency_ms"), result.get("loaded_latency_ms"),
                result.get("grade"), result.get("ok", False),
                result.get("error"))
            try:
                self._eval_speedtest_alerts(result)
            except Exception:
                pass
            return result
        finally:
            with self.lock:
                self.live["speedtest_running"] = False

    # ------------------------------------------------------------- misc

    def _maintenance_loop(self) -> None:
        while self._sleep(3600.0):
            try:
                db.rollup_old_data()
                days = config.load().get("alert_retention_days", 90)
                if days and days > 0:
                    db.purge_old_alert_events(time.time() - days * 86400)
            except Exception:
                pass

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.live)

    def set_label(self, label: str) -> None:
        ts = time.time()
        db.set_label(label, ts)
        cur = db.current_label()  # unchanged start_ts if label was the same
        with self.lock:
            self.live["label"] = label
            self.live["label_since"] = cur["start_ts"] if cur else ts
