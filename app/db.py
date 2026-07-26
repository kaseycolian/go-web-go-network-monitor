"""SQLite persistence for What the Bit.

Single connection in WAL mode guarded by a lock — write volume is tiny
(a few rows/second). All timestamps are unix epoch seconds (UTC, float).
"""
import json
import sqlite3
import threading
import time

from . import config

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS ping_samples(
    ts REAL NOT NULL, target TEXT NOT NULL, rtt_ms REAL);
CREATE INDEX IF NOT EXISTS idx_ping_ts ON ping_samples(ts);

CREATE TABLE IF NOT EXISTS dns_samples(ts REAL NOT NULL, resolve_ms REAL);
CREATE INDEX IF NOT EXISTS idx_dns_ts ON dns_samples(ts);

CREATE TABLE IF NOT EXISTS http_samples(ts REAL NOT NULL, ok INTEGER, ms REAL);
CREATE INDEX IF NOT EXISTS idx_http_ts ON http_samples(ts);

CREATE TABLE IF NOT EXISTS throughput(
    ts REAL NOT NULL, adapter TEXT, rx_bps REAL, tx_bps REAL);
CREATE INDEX IF NOT EXISTS idx_tp_ts ON throughput(ts);

CREATE TABLE IF NOT EXISTS interface_state(
    ts REAL NOT NULL, adapter TEXT, conn_type TEXT, ssid TEXT,
    link_mbps REAL, is_up INTEGER, ip TEXT, wifi_signal_pct REAL,
    event TEXT);
CREATE INDEX IF NOT EXISTS idx_if_ts ON interface_state(ts);

CREATE TABLE IF NOT EXISTS notes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, text TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS speedtests(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    down_mbps REAL, up_mbps REAL, latency_ms REAL,
    loaded_latency_ms REAL, bufferbloat_grade TEXT,
    ok INTEGER NOT NULL, error TEXT);

CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts REAL NOT NULL, ended_ts REAL, end_reason TEXT);

CREATE TABLE IF NOT EXISTS location_labels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL, start_ts REAL NOT NULL, end_ts REAL);

CREATE TABLE IF NOT EXISTS outages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts REAL NOT NULL, end_ts REAL,
    layer TEXT NOT NULL, worst_loss_pct REAL,
    label_at_time TEXT, self_saturated INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_outage_start ON outages(start_ts);

CREATE TABLE IF NOT EXISTS traceroutes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outage_id INTEGER, ts REAL NOT NULL, target TEXT, hops_json TEXT);

CREATE TABLE IF NOT EXISTS wan_ip(ts REAL NOT NULL, ip TEXT);

CREATE TABLE IF NOT EXISTS ping_rollup(
    minute_ts REAL NOT NULL, target TEXT NOT NULL,
    avg_rtt REAL, min_rtt REAL, max_rtt REAL, jitter_ms REAL,
    sent INTEGER, lost INTEGER);
CREATE INDEX IF NOT EXISTS idx_pr_ts ON ping_rollup(minute_ts);

CREATE TABLE IF NOT EXISTS throughput_rollup(
    minute_ts REAL NOT NULL, adapter TEXT,
    avg_rx REAL, avg_tx REAL, max_rx REAL, max_tx REAL);
CREATE INDEX IF NOT EXISTS idx_tpr_ts ON throughput_rollup(minute_ts);

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS alert_rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric TEXT NOT NULL, threshold REAL NOT NULL,
    duration_s INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1, created_ts REAL NOT NULL);

CREATE TABLE IF NOT EXISTS alert_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER, metric TEXT NOT NULL, threshold REAL,
    value REAL, start_ts REAL NOT NULL, end_ts REAL, message TEXT);
CREATE INDEX IF NOT EXISTS idx_alert_ev_start ON alert_events(start_ts);
"""


def init() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            return
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(SCHEMA)
        # migrations for DBs created before these columns existed
        cols = [r["name"] for r in
                _conn.execute("PRAGMA table_info(outages)")]
        if "note" not in cols:
            _conn.execute("ALTER TABLE outages ADD COLUMN note TEXT")
        _conn.commit()


def _c() -> sqlite3.Connection:
    if _conn is None:
        init()
    return _conn  # type: ignore[return-value]


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _c().execute(sql, params)
        _c().commit()
        return cur


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return _c().execute(sql, params).fetchall()


# ---------------------------------------------------------------- writes

def add_ping(ts: float, target: str, rtt_ms: float | None) -> None:
    execute("INSERT INTO ping_samples VALUES(?,?,?)", (ts, target, rtt_ms))

def add_dns(ts: float, resolve_ms: float | None) -> None:
    execute("INSERT INTO dns_samples VALUES(?,?)", (ts, resolve_ms))

def add_http(ts: float, ok: bool, ms: float | None) -> None:
    execute("INSERT INTO http_samples VALUES(?,?,?)", (ts, int(ok), ms))

def add_throughput(ts: float, adapter: str, rx_bps: float, tx_bps: float) -> None:
    execute("INSERT INTO throughput VALUES(?,?,?,?)", (ts, adapter, rx_bps, tx_bps))

def add_interface_state(ts, adapter, conn_type, ssid, link_mbps, is_up, ip,
                        wifi_signal_pct, event) -> None:
    execute("INSERT INTO interface_state VALUES(?,?,?,?,?,?,?,?,?)",
            (ts, adapter, conn_type, ssid, link_mbps, int(bool(is_up)), ip,
             wifi_signal_pct, event))

def add_note(ts: float, text: str) -> int:
    return execute("INSERT INTO notes(ts, text) VALUES(?,?)", (ts, text)).lastrowid

def delete_note(note_id: int) -> None:
    execute("DELETE FROM notes WHERE id=?", (note_id,))

def add_speedtest(ts, down_mbps, up_mbps, latency_ms, loaded_latency_ms,
                  grade, ok, error) -> int:
    return execute(
        "INSERT INTO speedtests(ts,down_mbps,up_mbps,latency_ms,"
        "loaded_latency_ms,bufferbloat_grade,ok,error) VALUES(?,?,?,?,?,?,?,?)",
        (ts, down_mbps, up_mbps, latency_ms, loaded_latency_ms, grade,
         int(ok), error)).lastrowid

def add_wan_ip(ts: float, ip: str) -> None:
    execute("INSERT INTO wan_ip VALUES(?,?)", (ts, ip))

def last_wan_ip() -> sqlite3.Row | None:
    rows = query("SELECT ts, ip FROM wan_ip ORDER BY ts DESC LIMIT 1")
    return rows[0] if rows else None

def add_traceroute(outage_id: int | None, ts: float, target: str,
                   hops: list) -> None:
    execute("INSERT INTO traceroutes(outage_id, ts, target, hops_json) "
            "VALUES(?,?,?,?)", (outage_id, ts, target, json.dumps(hops)))


# ---------------------------------------------------------------- sessions

def start_session(ts: float) -> int:
    return execute("INSERT INTO sessions(started_ts) VALUES(?)", (ts,)).lastrowid

def end_session(session_id: int, ts: float, reason: str) -> None:
    execute("UPDATE sessions SET ended_ts=?, end_reason=? WHERE id=? "
            "AND ended_ts IS NULL", (ts, reason, session_id))

def repair_dangling_sessions() -> None:
    """Close sessions left open by a crash, at the last recorded sample."""
    for row in query("SELECT id, started_ts FROM sessions WHERE ended_ts IS NULL"):
        last = query("SELECT MAX(ts) AS m FROM ping_samples WHERE ts >= ?",
                     (row["started_ts"],))
        end = last[0]["m"] or row["started_ts"]
        execute("UPDATE sessions SET ended_ts=?, end_reason='crash' WHERE id=?",
                (end, row["id"]))

def monitored_intervals(t_from: float, t_to: float) -> list[tuple[float, float]]:
    rows = query(
        "SELECT started_ts, ended_ts FROM sessions "
        "WHERE started_ts <= ? AND (ended_ts IS NULL OR ended_ts >= ?) "
        "ORDER BY started_ts", (t_to, t_from))
    out = []
    for r in rows:
        s = max(r["started_ts"], t_from)
        e = min(r["ended_ts"] if r["ended_ts"] is not None else time.time(), t_to)
        if e > s:
            out.append((s, e))
    return out


# ---------------------------------------------------------------- labels

def current_label() -> sqlite3.Row | None:
    rows = query("SELECT * FROM location_labels WHERE end_ts IS NULL "
                 "ORDER BY start_ts DESC LIMIT 1")
    return rows[0] if rows else None

def set_label(label: str, ts: float) -> None:
    cur = current_label()
    if cur and cur["label"] == label:
        return
    if cur:
        execute("UPDATE location_labels SET end_ts=? WHERE id=?", (ts, cur["id"]))
    execute("INSERT INTO location_labels(label, start_ts) VALUES(?,?)",
            (label, ts))

def labels_in_range(t_from: float, t_to: float) -> list[dict]:
    rows = query(
        "SELECT id, label, start_ts, end_ts FROM location_labels "
        "WHERE start_ts <= ? AND (end_ts IS NULL OR end_ts >= ?) "
        "ORDER BY start_ts", (t_to, t_from))
    return [dict(r) for r in rows]

def known_labels() -> list[str]:
    return [r["label"] for r in query(
        "SELECT label, MAX(start_ts) AS m FROM location_labels "
        "GROUP BY label ORDER BY m DESC LIMIT 20")]

def label_at(ts: float) -> str | None:
    rows = query("SELECT label FROM location_labels WHERE start_ts <= ? "
                 "AND (end_ts IS NULL OR end_ts > ?) "
                 "ORDER BY start_ts DESC LIMIT 1", (ts, ts))
    return rows[0]["label"] if rows else None


# ---------------------------------------------------------------- outages

def open_outage(ts: float, layer: str, self_saturated: bool = False) -> int:
    return execute(
        "INSERT INTO outages(start_ts, layer, worst_loss_pct, label_at_time, "
        "self_saturated) VALUES(?,?,?,?,?)",
        (ts, layer, 0.0, label_at(ts), int(self_saturated))).lastrowid

def update_outage(outage_id: int, worst_loss_pct: float | None = None,
                  layer: str | None = None) -> None:
    if worst_loss_pct is not None:
        execute("UPDATE outages SET worst_loss_pct=MAX(IFNULL(worst_loss_pct,0),?) "
                "WHERE id=?", (worst_loss_pct, outage_id))
    if layer is not None:
        execute("UPDATE outages SET layer=? WHERE id=?", (layer, outage_id))

def set_outage_note(outage_id: int, text: str | None) -> bool:
    cur = execute("UPDATE outages SET note=? WHERE id=?",
                  (text or None, outage_id))
    return cur.rowcount > 0

def close_outage(outage_id: int, ts: float) -> None:
    execute("UPDATE outages SET end_ts=? WHERE id=? AND end_ts IS NULL",
            (ts, outage_id))

def outages_in_range(t_from: float, t_to: float) -> list[dict]:
    rows = query(
        "SELECT * FROM outages WHERE start_ts <= ? "
        "AND (end_ts IS NULL OR end_ts >= ?) ORDER BY start_ts DESC",
        (t_to, t_from))
    out = []
    for r in rows:
        d = dict(r)
        tr = query("SELECT ts, target, hops_json FROM traceroutes "
                   "WHERE outage_id=? ORDER BY ts LIMIT 1", (r["id"],))
        d["traceroute"] = (json.loads(tr[0]["hops_json"]) if tr else None)
        out.append(d)
    return out


# ---------------------------------------------------------------- alerts

def add_alert_rule(metric: str, threshold: float, duration_s: int) -> int:
    return execute(
        "INSERT INTO alert_rules(metric, threshold, duration_s, created_ts) "
        "VALUES(?,?,?,?)", (metric, threshold, duration_s, time.time())
    ).lastrowid

def list_alert_rules() -> list[dict]:
    return [dict(r) for r in query(
        "SELECT * FROM alert_rules ORDER BY created_ts")]

def delete_alert_rule(rule_id: int) -> None:
    execute("DELETE FROM alert_rules WHERE id=?", (rule_id,))

def open_alert_event(rule_id: int, metric: str, threshold: float,
                     value: float, start_ts: float, message: str,
                     end_ts: float | None = None) -> int:
    return execute(
        "INSERT INTO alert_events(rule_id, metric, threshold, value, "
        "start_ts, end_ts, message) VALUES(?,?,?,?,?,?,?)",
        (rule_id, metric, threshold, value, start_ts, end_ts, message)
    ).lastrowid

def update_alert_event(event_id: int, value: float, message: str) -> None:
    execute("UPDATE alert_events SET value=?, message=? WHERE id=?",
            (value, message, event_id))

def close_alert_event(event_id: int, ts: float) -> None:
    execute("UPDATE alert_events SET end_ts=? WHERE id=? AND end_ts IS NULL",
            (ts, event_id))

def alert_events_recent(limit: int = 50) -> list[dict]:
    return [dict(r) for r in query(
        "SELECT * FROM alert_events ORDER BY start_ts DESC LIMIT ?", (limit,))]

def count_open_alert_events() -> int:
    rows = query("SELECT COUNT(*) AS n FROM alert_events WHERE end_ts IS NULL")
    return rows[0]["n"] if rows else 0

def delete_alert_event(event_id: int) -> None:
    execute("DELETE FROM alert_events WHERE id=?", (event_id,))

def clear_alert_events() -> int:
    return execute("DELETE FROM alert_events").rowcount

def purge_old_alert_events(cutoff_ts: float) -> int:
    return execute("DELETE FROM alert_events WHERE start_ts < ? "
                   "AND end_ts IS NOT NULL", (cutoff_ts,)).rowcount


# ---------------------------------------------------------------- series

def _bucketize(t_from: float, t_to: float, bucket: float) -> float:
    return max(bucket, 1.0)

def ping_series(t_from: float, t_to: float, bucket: float) -> dict:
    """Per-target bucketed latency/loss/jitter, merging raw + rollup."""
    bucket = _bucketize(t_from, t_to, bucket)
    result: dict[str, dict[float, dict]] = {}

    for r in query(
        "SELECT CAST(ts/? AS INTEGER)*? AS b, target, "
        "AVG(rtt_ms) AS avg_rtt, MAX(rtt_ms) AS max_rtt, "
        "COUNT(*) AS sent, SUM(rtt_ms IS NULL) AS lost "
        "FROM ping_samples WHERE ts BETWEEN ? AND ? "
        "GROUP BY b, target", (bucket, bucket, t_from, t_to)):
        result.setdefault(r["target"], {})[r["b"]] = {
            "avg": r["avg_rtt"], "max": r["max_rtt"],
            "sent": r["sent"], "lost": r["lost"] or 0, "jitter": None}

    for r in query(
        "SELECT b, target, AVG(d) AS j FROM ("
        "  SELECT CAST(ts/? AS INTEGER)*? AS b, target, "
        "  ABS(rtt_ms - LAG(rtt_ms) OVER (PARTITION BY target ORDER BY ts)) AS d"
        "  FROM ping_samples WHERE ts BETWEEN ? AND ? AND rtt_ms IS NOT NULL"
        ") WHERE d IS NOT NULL GROUP BY b, target",
            (bucket, bucket, t_from, t_to)):
        if r["target"] in result and r["b"] in result[r["target"]]:
            result[r["target"]][r["b"]]["jitter"] = r["j"]

    for r in query(
        "SELECT CAST(minute_ts/? AS INTEGER)*? AS b, target, "
        "AVG(avg_rtt) AS avg_rtt, MAX(max_rtt) AS max_rtt, "
        "AVG(jitter_ms) AS j, SUM(sent) AS sent, SUM(lost) AS lost "
        "FROM ping_rollup WHERE minute_ts BETWEEN ? AND ? "
        "GROUP BY b, target", (bucket, bucket, t_from, t_to)):
        tgt = result.setdefault(r["target"], {})
        if r["b"] not in tgt:
            tgt[r["b"]] = {"avg": r["avg_rtt"], "max": r["max_rtt"],
                           "sent": r["sent"], "lost": r["lost"] or 0,
                           "jitter": r["j"]}

    out = {}
    for target, buckets in result.items():
        pts = []
        for b in sorted(buckets):
            v = buckets[b]
            loss = (100.0 * v["lost"] / v["sent"]) if v["sent"] else None
            pts.append({"t": b, "avg": v["avg"], "max": v["max"],
                        "loss": loss, "jitter": v["jitter"]})
        out[target] = pts
    return out


def throughput_series(t_from: float, t_to: float, bucket: float) -> list[dict]:
    bucket = _bucketize(t_from, t_to, bucket)
    merged: dict[float, dict] = {}
    for r in query(
        "SELECT CAST(ts/? AS INTEGER)*? AS b, AVG(rx_bps) AS rx, "
        "AVG(tx_bps) AS tx, MAX(rx_bps) AS mrx, MAX(tx_bps) AS mtx "
        "FROM throughput WHERE ts BETWEEN ? AND ? GROUP BY b",
            (bucket, bucket, t_from, t_to)):
        merged[r["b"]] = {"t": r["b"], "rx": r["rx"], "tx": r["tx"],
                          "max_rx": r["mrx"], "max_tx": r["mtx"]}
    for r in query(
        "SELECT CAST(minute_ts/? AS INTEGER)*? AS b, AVG(avg_rx) AS rx, "
        "AVG(avg_tx) AS tx, MAX(max_rx) AS mrx, MAX(max_tx) AS mtx "
        "FROM throughput_rollup WHERE minute_ts BETWEEN ? AND ? GROUP BY b",
            (bucket, bucket, t_from, t_to)):
        merged.setdefault(r["b"], {"t": r["b"], "rx": r["rx"], "tx": r["tx"],
                                   "max_rx": r["mrx"], "max_tx": r["mtx"]})
    return [merged[b] for b in sorted(merged)]


def dns_series(t_from: float, t_to: float, bucket: float) -> list[dict]:
    bucket = _bucketize(t_from, t_to, bucket)
    return [{"t": r["b"], "avg": r["avg_ms"],
             "fail_pct": 100.0 * (r["failed"] or 0) / r["n"] if r["n"] else None}
            for r in query(
                "SELECT CAST(ts/? AS INTEGER)*? AS b, AVG(resolve_ms) AS avg_ms, "
                "COUNT(*) AS n, SUM(resolve_ms IS NULL) AS failed "
                "FROM dns_samples WHERE ts BETWEEN ? AND ? GROUP BY b",
                (bucket, bucket, t_from, t_to))]


def loss_by_bucket(t_from: float, t_to: float, bucket: float,
                   targets: list[str]) -> dict[float, float]:
    """Worst-case loss %% per bucket across the given targets (raw + rollup)."""
    bucket = _bucketize(t_from, t_to, bucket)
    acc: dict[float, list[int]] = {}
    ph = ",".join("?" * len(targets))
    for table, tscol in (("ping_samples", "ts"), ("ping_rollup", "minute_ts")):
        if table == "ping_samples":
            sql = (f"SELECT CAST(ts/? AS INTEGER)*? AS b, COUNT(*) AS sent, "
                   f"SUM(rtt_ms IS NULL) AS lost FROM ping_samples "
                   f"WHERE ts BETWEEN ? AND ? AND target IN ({ph}) GROUP BY b")
        else:
            sql = (f"SELECT CAST(minute_ts/? AS INTEGER)*? AS b, "
                   f"SUM(sent) AS sent, SUM(lost) AS lost FROM ping_rollup "
                   f"WHERE minute_ts BETWEEN ? AND ? AND target IN ({ph}) "
                   f"GROUP BY b")
        for r in query(sql, (bucket, bucket, t_from, t_to, *targets)):
            a = acc.setdefault(r["b"], [0, 0])
            a[0] += r["sent"] or 0
            a[1] += r["lost"] or 0
    return {b: (100.0 * lost / sent if sent else 0.0)
            for b, (sent, lost) in acc.items()}


def speedtests_in_range(t_from: float, t_to: float) -> list[dict]:
    return [dict(r) for r in query(
        "SELECT * FROM speedtests WHERE ts BETWEEN ? AND ? ORDER BY ts",
        (t_from, t_to))]

def last_speedtest(only_ok: bool = True) -> dict | None:
    where = "WHERE ok=1" if only_ok else ""
    rows = query(f"SELECT * FROM speedtests {where} ORDER BY ts DESC LIMIT 1")
    return dict(rows[0]) if rows else None

def notes_in_range(t_from: float, t_to: float) -> list[dict]:
    return [dict(r) for r in query(
        "SELECT * FROM notes WHERE ts BETWEEN ? AND ? ORDER BY ts",
        (t_from, t_to))]

def interface_events(t_from: float, t_to: float) -> list[dict]:
    return [dict(r) for r in query(
        "SELECT * FROM interface_state WHERE ts BETWEEN ? AND ? "
        "AND event IS NOT NULL AND event != 'heartbeat' ORDER BY ts",
        (t_from, t_to))]

def wan_ip_changes(t_from: float, t_to: float) -> list[dict]:
    rows = query("SELECT ts, ip FROM wan_ip WHERE ts BETWEEN ? AND ? ORDER BY ts",
                 (t_from, t_to))
    out, prev = [], None
    for r in rows:
        if prev is not None and r["ip"] != prev:
            out.append({"ts": r["ts"], "ip": r["ip"], "prev": prev})
        prev = r["ip"]
    return out


# ---------------------------------------------------------------- maintenance

MEASUREMENT_TABLES = {
    "ping_samples": "ts", "dns_samples": "ts", "http_samples": "ts",
    "throughput": "ts", "interface_state": "ts", "wan_ip": "ts",
    "ping_rollup": "minute_ts", "throughput_rollup": "minute_ts",
    "speedtests": "ts", "traceroutes": "ts",
}

def clear_range(t_from: float, t_to: float, include_notes_labels: bool) -> dict:
    counts = {}
    with _lock:
        for table, col in MEASUREMENT_TABLES.items():
            cur = _c().execute(
                f"DELETE FROM {table} WHERE {col} BETWEEN ? AND ?",
                (t_from, t_to))
            counts[table] = cur.rowcount
        cur = _c().execute(
            "DELETE FROM outages WHERE start_ts >= ? AND "
            "IFNULL(end_ts, start_ts) <= ?", (t_from, t_to))
        counts["outages"] = cur.rowcount
        if include_notes_labels:
            cur = _c().execute("DELETE FROM notes WHERE ts BETWEEN ? AND ?",
                               (t_from, t_to))
            counts["notes"] = cur.rowcount
            cur = _c().execute(
                "DELETE FROM location_labels WHERE start_ts >= ? "
                "AND end_ts IS NOT NULL AND end_ts <= ?", (t_from, t_to))
            counts["location_labels"] = cur.rowcount
        _c().commit()
    return counts


def rollup_old_data() -> int:
    """Downsample raw samples older than retention_days to 1-minute rollups."""
    cfg = config.load()
    cutoff = time.time() - cfg["retention_days"] * 86400
    moved = 0
    with _lock:
        c = _c()
        c.execute(
            "INSERT INTO ping_rollup(minute_ts,target,avg_rtt,min_rtt,max_rtt,"
            "jitter_ms,sent,lost) "
            "SELECT b, target, AVG(rtt_ms), MIN(rtt_ms), MAX(rtt_ms), "
            "AVG(d), COUNT(*), SUM(rtt_ms IS NULL) FROM ("
            "  SELECT CAST(ts/60 AS INTEGER)*60 AS b, target, ts, rtt_ms, "
            "  ABS(rtt_ms - LAG(rtt_ms) OVER (PARTITION BY target ORDER BY ts)) AS d"
            "  FROM ping_samples WHERE ts < ?"
            ") GROUP BY b, target", (cutoff,))
        cur = c.execute("DELETE FROM ping_samples WHERE ts < ?", (cutoff,))
        moved += cur.rowcount
        c.execute(
            "INSERT INTO throughput_rollup(minute_ts,adapter,avg_rx,avg_tx,"
            "max_rx,max_tx) "
            "SELECT CAST(ts/60 AS INTEGER)*60, adapter, AVG(rx_bps), "
            "AVG(tx_bps), MAX(rx_bps), MAX(tx_bps) FROM throughput "
            "WHERE ts < ? GROUP BY 1, adapter", (cutoff,))
        cur = c.execute("DELETE FROM throughput WHERE ts < ?", (cutoff,))
        moved += cur.rowcount
        for table in ("dns_samples", "http_samples"):
            c.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        c.commit()
    return moved


def meta_get(key: str, default: str | None = None) -> str | None:
    rows = query("SELECT value FROM meta WHERE key=?", (key,))
    return rows[0]["value"] if rows else default

def meta_set(key: str, value: str) -> None:
    execute("INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
