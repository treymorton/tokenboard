#!/usr/bin/env python3
"""
TokenBoard - local token counter & frontier-cost-savings dashboard.

Stdlib-only (no pip). One process does:
  * sampling  : vLLM /metrics (authoritative token counters), /proc/stat,
                /proc/meminfo, nvidia-smi (GPU util / power)
  * storage   : SQLite (samples + cumulative counters, reset-aware)
  * serving   : dashboard + JSON API
  * hub       : role "box" can POST samples to a hub; role "hub"
                ingests + aggregates many boxes.

Roles
  box  : samples its own vLLM, optionally reports to hub_url
  hub  : ingests from boxes, shows aggregate + per-box view

Endpoints (server)
  GET  /                 dashboard
  GET  /api/state        cumulative counters, live rates, daily totals, prices
  GET  /api/timeseries   samples for charts (from, box)
  GET  /api/days         per-calendar-day token totals
  GET  /api/boxes        known boxes (hub view)
  POST /api/ingest       box -> hub sample push
  GET/POST /api/prices   frontier price table
"""
import json
import os
import re
import sqlite3
import socket
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("TOKENBOARD_CONFIG", str(ROOT / "config.json")))
PRICES_PATH = ROOT / "prices.json"

# ---------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default

CONFIG = load_json(CONFIG_PATH, {})
DB_PATH = ROOT / CONFIG.get("db_file", "tokenboard.db")
BOX_NAME = (CONFIG.get("box_name") or "").strip() or socket.gethostname()
ROLE = CONFIG.get("role", "box")
PORT = int(CONFIG.get("port", 8100))
VLLM_URL = (CONFIG.get("vllm_url") or "http://127.0.0.1:8000").rstrip("/")
OLLAMA_URL = (CONFIG.get("ollama_url") or "").rstrip("/")
_llama_cfg = CONFIG.get("llama_url") or ""
if isinstance(_llama_cfg, str):
    _llama_cfg = [_llama_cfg] if _llama_cfg.strip() else []
# explicit llama endpoints; when empty the sampler auto-discovers llama-server
LLAMA_PINS = [str(u).rstrip("/") for u in _llama_cfg if str(u).strip()]
HUB_URL = (CONFIG.get("hub_url") or "").rstrip("/")
SAMPLE_INTERVAL = int(CONFIG.get("sample_interval", 2))
# updated live by the sampler each cycle ("vllm" | "ollama" | "llama" | "none")
TOKEN_SOURCE = "none"

METRIC_RE = re.compile(
    r"^(?P<name>(?:vllm|llamacpp):[a-zA-Z0-9_:]+)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[\d.eE+-]+)$"
)
LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def now_utc():
    return datetime.now(timezone.utc)


def http_get(url, timeout=5):
    req = urllib.request.Request(url, headers={"User-Agent": "tokenboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse_prom(text):
    """Minimal prometheus text parser: returns {name: [values]} (multi-series)."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = METRIC_RE.match(line)
        if not m:
            continue
        try:
            val = float(m.group("value"))
        except ValueError:
            continue
        labels = dict(LABEL_RE.findall(m.group("labels") or ""))
        out.setdefault(m.group("name"), []).append((labels, val))
    return out


def first_value(metrics, name):
    series = metrics.get(name)
    if not series:
        return None
    return series[0][1]


def discover_llama_urls():
    """Base URLs of every LISTEN socket owned by a llama-server process.

    Hermes/llama.cpp launch llama-server on ephemeral ports, so we find it
    the way a sysadmin would: /proc comm -> socket fds -> /proc/net/tcp[6]
    LISTEN rows. Stdlib-only, Linux. Docker-external llama instances need
    an explicit llama_url (different net = invisible here).
    """
    sock_inodes = set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() != "llama-server":
                    continue
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    link = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if link.startswith("socket:["):
                    sock_inodes.add(link[8:-1])
        except OSError:
            continue
    if not sock_inodes:
        return []
    urls = set()
    for net in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(net) as f:
                next(f, None)  # header
                for line in f:
                    parts = line.split()
                    if len(parts) < 10 or parts[3] != "0A":  # 0A = LISTEN
                        continue
                    if parts[9] not in sock_inodes:
                        continue
                    urls.add(f"http://127.0.0.1:{int(parts[1].rsplit(':', 1)[1], 16)}")
        except OSError:
            continue
    return sorted(urls, key=lambda u: int(u.rsplit(":", 1)[1]))


# ---------------------------------------------------------------- db

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts          INTEGER NOT NULL,
  box         TEXT NOT NULL,
  cpu_pct     REAL,
  mem_used_gb REAL,
  mem_total_gb REAL,
  gpu_pct     REAL,
  gpu_power_w REAL,
  prompt_cum  INTEGER,
  gen_cum     INTEGER,
  requests_cum INTEGER,
  req_running INTEGER,
  kv_cache_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_samples_box_ts ON samples(box, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_samples_box_ts_uq ON samples(box, ts);

CREATE TABLE IF NOT EXISTS boxes (
  name      TEXT PRIMARY KEY,
  last_seen INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# ---------------------------------------------------------------- sampler

class Sampler(threading.Thread):
    """Samples the local box every SAMPLE_INTERVAL seconds."""

    def __init__(self):
        super().__init__(daemon=True)
        self.last_proc = None
        self.offset_prompt = 0
        self.offset_gen = 0
        self.offset_req = 0
        self.llama_urls = list(LLAMA_PINS) if LLAMA_PINS else None
        self.llama_next_scan = 0.0
        self.llama_reqs = {}   # url -> completed-request count; persisted in meta
        self.llama_busy = {}   # url -> per-slot busy flags; persisted in meta
        self._load_offsets()

    def _load_offsets(self):
        conn = db()
        try:
            self.offset_prompt = int(meta_get(conn, "offset_prompt", 0) or 0)
            self.offset_gen = int(meta_get(conn, "offset_gen", 0) or 0)
            self.offset_req = int(meta_get(conn, "offset_req", 0) or 0)
            try:
                self.llama_reqs = json.loads(meta_get(conn, "llama_reqs", "{}") or "{}")
                self.llama_busy = json.loads(meta_get(conn, "llama_busy", "{}") or "{}")
            except Exception:
                self.llama_reqs = {}
                self.llama_busy = {}
        finally:
            conn.close()

    def _persist_offsets(self, conn):
        meta_set(conn, "offset_prompt", self.offset_prompt)
        meta_set(conn, "offset_gen", self.offset_gen)
        meta_set(conn, "offset_req", self.offset_req)
        meta_set(conn, "llama_reqs", json.dumps(self.llama_reqs))
        meta_set(conn, "llama_busy", json.dumps(self.llama_busy))

    def _vllm_counters(self):
        """Return (prompt, gen, requests, req_running, kv_pct) or None."""
        try:
            m = parse_prom(http_get(f"{VLLM_URL}/metrics", timeout=5))
        except Exception:
            return None
        p = first_value(m, "vllm:prompt_tokens_total")
        g = first_value(m, "vllm:generation_tokens_total")
        if p is None or g is None:
            return None
        # sum across finished_reason for total request completions
        r = 0.0
        for _labels, v in m.get("vllm:request_success_total", []):
            r += v
        running = first_value(m, "vllm:num_requests_running")
        kv = first_value(m, "vllm:kv_cache_usage_perc")
        return (int(p), int(g), int(r),
                int(running) if running is not None else None,
                (kv * 100) if kv is not None else None)

    def _llama_slots(self, url):
        try:
            slots = json.loads(http_get(f"{url}/slots", timeout=5))
            return slots if isinstance(slots, list) else None
        except Exception:
            return None

    def _llama_req_advance(self, url, busy_flags):
        """Per-server completed-request count from /slots busy-state edges.

        A slot's busy->idle transition is exactly one completed request.
        Requests at least as long as the sample interval are counted exactly;
        shorter ones (sub-2 s probes) can start AND finish between samples
        and go unseen (vLLM's counter is exact, this is the best llama.cpp
        offers). The busy snapshot is persisted, so a request in flight
        across a TokenBoard restart is still counted when it completes;
        a changed slot count (llama restart / new parallelism) re-snapshots
        without counting a spurious completion.
        """
        prev = self.llama_busy.get(url)
        count = self.llama_reqs.get(url, 0)
        if prev is not None and len(prev) == len(busy_flags):
            count += sum(1 for was, now in zip(prev, busy_flags) if was and not now)
        self.llama_reqs[url] = count
        self.llama_busy[url] = busy_flags
        return count

    def _llama_counters(self):
        """Return (prompt, gen, requests, req_running, kv_pct) summed across
        every llama-server exposing counters, or None if none respond.

        Requests come from a metrics counter when the build has one, else
        from /slots id_task (see _llama_req_advance); if neither is readable
        requests is None. KV % = in-context tokens across slots / n_ctx
        (approximation: llama exposes no occupancy metric; prefix-cached
        prompt state counts as occupied, as in vLLM). Endpoints are
        auto-discovered via /proc unless pinned in config; ports are
        ephemeral so a failed fetch forces a rescan.
        """
        if self.llama_urls is None or time.time() >= self.llama_next_scan:
            self.llama_urls = discover_llama_urls() if not LLAMA_PINS else LLAMA_PINS
            self.llama_next_scan = time.time() + 30.0
        prompt = gen = 0
        reqs = 0
        have_reqs = False
        running = None
        kv_used = kv_cap = 0
        found = False
        for url in self.llama_urls:
            try:
                m = parse_prom(http_get(f"{url}/metrics", timeout=5))
            except Exception:
                self.llama_next_scan = 0.0  # endpoint vanished: rescan next cycle
                continue
            p = first_value(m, "llamacpp:prompt_tokens_total")
            g = first_value(m, "llamacpp:tokens_predicted_total")
            if p is None or g is None:
                continue  # up but not a readable metrics source (e.g. key-guarded)
            found = True
            # vLLM's prompt_tokens_total includes prefix-cache hits; llama.cpp
            # counts cached prompt tokens separately, so add them back for
            # equivalent "prompt tokens processed" semantics.
            prompt += int(p + (first_value(m, "llamacpp:prompt_tokens_cached_total") or 0))
            gen += int(g)
            rc = first_value(m, "llamacpp:requests_completed_total")
            run = first_value(m, "llamacpp:requests_processing")
            if run is None:
                run = first_value(m, "llamacpp:requests_active")
            if run is not None:
                running = (running or 0) + int(run)
            slots = self._llama_slots(url)
            if rc is not None:
                reqs += int(rc)
                have_reqs = True
            if slots:
                nctx = 0
                busy = []
                for s in slots:
                    try:
                        nctx = max(nctx, int(s.get("n_ctx") or 0))
                        busy.append(bool(s.get("is_processing")))
                        in_ctx = int(s.get("n_prompt_tokens") or 0)
                        nxt = s.get("next_token")
                        if isinstance(nxt, list) and nxt:
                            in_ctx += int(nxt[0].get("n_decoded") or 0)
                        kv_used += in_ctx
                    except (TypeError, ValueError):
                        busy.append(False)
                        continue
                if nctx > 0:
                    kv_cap += nctx
                if rc is None and busy:
                    reqs += self._llama_req_advance(url, busy)
                    have_reqs = True
        if not found:
            return None
        kv = round(min(100.0, 100.0 * kv_used / kv_cap), 1) if kv_cap else None
        return (prompt, gen, reqs if have_reqs else None, running, kv)

    def _ollama_status(self):
        try:
            data = json.loads(http_get(f"{OLLAMA_URL}/api/ps", timeout=5))
            models = [m.get("name") for m in data.get("models", [])]
            return models or None
        except Exception:
            return None

    def _cpu_pct(self):
        """Overall CPU busy % from /proc/stat delta."""
        try:
            with open("/proc/stat") as f:
                fields = [int(x) for x in f.readline().split()[1:]]
        except Exception:
            return None
        if self.last_proc is None:
            self.last_proc = fields
            return None
        prev = self.last_proc
        self.last_proc = fields
        total_delta = sum(fields) - sum(prev)
        idle_delta = fields[3] - prev[3]
        if total_delta <= 0:
            return None
        return round(100.0 * (total_delta - idle_delta) / total_delta, 1)

    def _mem_gb(self):
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0])  # kB
            total = info["MemTotal"] / 1024 / 1024
            avail = info.get("MemAvailable", info["MemFree"]) / 1024 / 1024
            return round(total - avail, 2), round(total, 2)
        except Exception:
            return None, None

    def _gpu(self):
        """GPU util % + power draw (works on GB10; memory fields are N/A there)."""
        try:
            import subprocess
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,power.draw",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip().splitlines()
            if out:
                util, power = out[0].split(",")
                return float(util.strip()), float(power.strip())
        except Exception:
            pass
        return None, None

    def _commit_raw(self, src, raw_p, raw_g, raw_r):
        """Apply reset-aware offsets to raw backend counters; returns
        (prompt, gen, requests). raw_r may be None for backends without a
        request counter.

        Re-baseline fires on a counter reset OR an engine switch (vLLM <->
        llama): the offset is adjusted so the stored cumulative continues
        seamlessly from its last value. Source-awareness matters: with a
        naive fold (offset += last_raw), a brief vLLM appearance while the
        llama counters keep running would double-count the llama era on the
        way back. raw falling below last_raw still fires it for same-engine
        restarts; a fresh llama server (small raw) is handled identically.
        """
        conn = db()
        try:
            last = meta_get(conn, "last_raw_prompt")
            if last is not None and (src != meta_get(conn, "source") or raw_p < int(last)):
                self.offset_prompt += int(last) - raw_p
                self.offset_gen += int(meta_get(conn, "last_raw_gen") or 0) - raw_g
                if raw_r is not None:
                    self.offset_req += int(meta_get(conn, "last_raw_req") or 0) - raw_r
            prompt = raw_p + self.offset_prompt
            gen = raw_g + self.offset_gen
            requests = raw_r + self.offset_req if raw_r is not None else None
            self._persist_offsets(conn)
            meta_set(conn, "source", src)
            meta_set(conn, "last_raw_prompt", raw_p)
            meta_set(conn, "last_raw_gen", raw_g)
            if raw_r is not None:
                meta_set(conn, "last_raw_req", raw_r)
            conn.commit()  # REQUIRED: uncommitted meta writes are rolled back
            # on close, which silently defeats reset detection after a restart
            return prompt, gen, requests
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def sample(self):
        global TOKEN_SOURCE
        ts = int(time.time())
        cpu = self._cpu_pct()
        mem_used, mem_total = self._mem_gb()
        gpu_pct, gpu_power = self._gpu()
        prompt = gen = requests = None
        running = kv = None
        c = None
        src = "none"

        if LLAMA_PINS:
            c = self._llama_counters()
            if c:
                src = "llama"
        else:
            c = self._vllm_counters()
            if c:
                src = "vllm"
        if c is None and not LLAMA_PINS:
            # vLLM not answering: Ollama status if configured, else llama.cpp
            if OLLAMA_URL:
                ollama_models = self._ollama_status()
                if ollama_models is not None:
                    src = "ollama"
                    conn = db()
                    try:
                        meta_set(conn, "ollama_models", ",".join(ollama_models))
                        conn.commit()
                    finally:
                        conn.close()
            if src == "none":
                c = self._llama_counters()
                if c:
                    src = "llama"
        if c:
            raw_p, raw_g, raw_r, running, kv = c
            prompt, gen, requests = self._commit_raw(src, raw_p, raw_g, raw_r)
        TOKEN_SOURCE = src

        conn = db()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, BOX_NAME, cpu, mem_used, mem_total, gpu_pct, gpu_power,
                 prompt, gen, requests, running, kv),
            )
            conn.execute(
                "INSERT INTO boxes(name,last_seen) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
                (BOX_NAME, ts),
            )
            conn.commit()
        finally:
            conn.close()

        return {
            "ts": ts, "cpu": cpu, "mem_used_gb": mem_used,
            "gpu_pct": gpu_pct, "gpu_power_w": gpu_power,
            "prompt_cum": prompt, "gen_cum": gen, "requests_cum": requests,
            "source": TOKEN_SOURCE,
        }

    def run(self):
        while True:
            try:
                self.sample()
            except Exception as e:
                print(f"[sampler] {e}", file=sys.stderr)
            time.sleep(SAMPLE_INTERVAL)


# ---------------------------------------------------------------- hub push

def push_to_hub(sample):
    if not HUB_URL:
        return
    payload = json.dumps({"box": BOX_NAME, "sample": sample}).encode()
    try:
        req = urllib.request.Request(
            f"{HUB_URL}/api/ingest", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
    except Exception as e:
        print(f"[hub] push failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------- queries

def daily_totals(conn, box=None, days=14):
    """Per-calendar-day (LOCAL time) token totals from cumulative counters.

    Per box: day total = box's last cum within the day - box's last cum
    strictly before the day. Then sum across boxes. Reset-aware because
    box counters are monotonic (offsets persisted on restart).

    Note: a box whose vLLM counters predate the window is attributed its
    entire counter to the first day in the window - fine for tradeshow
    boxes where vLLM is freshly started per show.
    """
    today = date.today()  # local calendar date
    first_day = today - timedelta(days=days - 1)
    labels = {}
    for i in range(days):
        d = first_day + timedelta(days=i)
        labels[d.isoformat()] = {
            "date": d.isoformat(), "prompt": 0, "gen": 0, "requests": 0,
        }
    if box:
        boxes = [box]
    else:
        boxes = [r[0] for r in conn.execute("SELECT name FROM boxes").fetchall()]
    # local-time boundaries: naive datetimes resolve to the server's local tz
    win_start = datetime(first_day.year, first_day.month, first_day.day).timestamp()
    win_end = datetime((today + timedelta(days=1)).year,
                       (today + timedelta(days=1)).month,
                       (today + timedelta(days=1)).day).timestamp()
    for b in boxes:
        rows = conn.execute(
            "SELECT ts, prompt_cum, gen_cum, requests_cum FROM samples "
            "WHERE box=? AND prompt_cum IS NOT NULL AND ts>=? AND ts<? ORDER BY ts ASC",
            (b, win_start - 86400, win_end),
        ).fetchall()
        by_day = {}
        base_p = base_g = base_r = 0
        for ts, p, g, r in rows:
            d = datetime.fromtimestamp(ts).date().isoformat()  # local date
            by_day.setdefault(d, []).append((p, g, r))
        for d in sorted(by_day):
            day_vals = by_day[d]
            end = day_vals[-1]
            if d not in labels:
                # pre-window row (fetched to baseline day one): it only
                # carries the running base forward, never gets its own bar
                base_p, base_g, base_r = end[0], end[1], end[2] or 0
                continue
            labels[d]["prompt"] = max(0, end[0] - base_p)
            labels[d]["gen"] = max(0, end[1] - base_g)
            labels[d]["requests"] = max(0, (end[2] or 0) - base_r)
            base_p, base_g, base_r = end[0], end[1], end[2] or 0
        # carry last values into any subsequent days with no samples
        # (a box offline the whole day keeps its previous cumulative level,
        # so days after it stay consistent)
    return [labels[k] for k in sorted(labels)]


def load_prices():
    return load_json(PRICES_PATH, {"models": []})


def compute_cost(prices, prompt, gen):
    """Cost dict per model for the given token mix."""
    out = {}
    for m in prices.get("models", []):
        out[m["name"]] = round(
            prompt * m["input_per_m"] / 1e6 + gen * m["output_per_m"] / 1e6, 2
        )
    return out


# ---------------------------------------------------------------- api

class Handler(BaseHTTPRequestHandler):
    server_version = "TokenBoard/1.0"

    def log_message(self, fmt, *args):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            html = (ROOT / "web" / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/chart.umd.min.js":
            self._send(200, (ROOT / "web" / "chart.umd.min.js").read_bytes(),
                       "application/javascript")
        elif path == "/api/state":
            self.api_state()
        elif path == "/api/range":
            self.api_range()
        elif path == "/api/timeseries":
            self.api_timeseries()
        elif path == "/api/days":
            self.api_days()
        elif path == "/api/boxes":
            self.api_boxes()
        elif path == "/api/prices":
            self._send(200, load_prices())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/ingest":
            self.api_ingest()
        elif path == "/api/prices":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                prices = json.loads(body)
            except Exception:
                self._send(400, {"error": "bad json"})
                return
            PRICES_PATH.write_text(json.dumps(prices, indent=2))
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    # ---- handlers

    def api_state(self):
        conn = db()
        try:
            prices = load_prices()
            if ROLE == "box":
                row = conn.execute(
                    "SELECT ts, cpu_pct, mem_used_gb, mem_total_gb, gpu_pct, "
                    "gpu_power_w, prompt_cum, gen_cum, requests_cum, req_running, kv_cache_pct "
                    "FROM samples WHERE box=? ORDER BY ts DESC LIMIT 1",
                    (BOX_NAME,),
                ).fetchone()
                if not row:
                    self._send(200, {"state": "empty", "box": BOX_NAME, "role": ROLE,
                                     "source": TOKEN_SOURCE, "prices": prices})
                    return
                ts, cpu, mem_u, mem_t, gpu, power, p, g, r, running, kv = row
                live = {"cpu_pct": cpu, "gpu_pct": gpu, "gpu_power_w": power,
                        "mem_used_gb": mem_u, "mem_total_gb": mem_t,
                        "requests_running": running, "kv_cache_pct": kv}
                prompt_today = self._today_delta(conn, BOX_NAME, "prompt_cum", p)
                gen_today = self._today_delta(conn, BOX_NAME, "gen_cum", g)
                req_today = self._today_delta(conn, BOX_NAME, "requests_cum", r)
                total_today = prompt_today + gen_today
                # rolling tokens/sec over last 60s of samples
                win = conn.execute(
                    "SELECT ts, prompt_cum, gen_cum FROM samples WHERE box=? "
                    "AND prompt_cum IS NOT NULL AND ts<=? AND ts>=? "
                    "ORDER BY ts ASC",
                    (BOX_NAME, ts, ts - 60),
                ).fetchall()
                if len(win) >= 2:
                    dp = win[-1][1] - win[0][1]
                    dg = win[-1][2] - win[0][2]
                    dt = max(1, win[-1][0] - win[0][0])
                    live["tokens_per_sec"] = round(max(0, dp + dg) / dt, 1)
                else:
                    live["tokens_per_sec"] = None
                self._send(200, {
                    "box": BOX_NAME, "role": ROLE, "source": TOKEN_SOURCE,
                    "ts": ts, "live": live,
                    "today": {"prompt": prompt_today, "gen": gen_today,
                              "requests": req_today, "total": total_today,
                              "cost_by_model": compute_cost(prices, prompt_today, gen_today)},
                    "cumulative": {"prompt": p, "gen": g, "requests": r,
                                   "total": (p or 0) + (g or 0)},
                    "prices": prices,
                })
            else:
                # hub: aggregate across boxes
                boxes = conn.execute("SELECT name FROM boxes").fetchall()
                if not boxes:
                    self._send(200, {"state": "empty", "box": BOX_NAME, "role": ROLE,
                                     "source": TOKEN_SOURCE, "prices": prices})
                    return
                today_prompt = today_gen = today_req = cum_p = cum_g = cum_r = 0
                mem_used = mem_total = 0.0
                cpus, gpus, powers = [], [], []
                last_ts = 0
                for (b,) in boxes:
                    row = conn.execute(
                        "SELECT ts, cpu_pct, mem_used_gb, mem_total_gb, gpu_pct, "
                        "gpu_power_w, prompt_cum, gen_cum, requests_cum FROM samples "
                        "WHERE box=? AND prompt_cum IS NOT NULL ORDER BY ts DESC LIMIT 1",
                        (b,),
                    ).fetchone()
                    if not row:
                        continue
                    (ts, cpu, mu, mt, gpu, power, p, g, r) = row
                    last_ts = max(last_ts, ts)
                    cum_p += p; cum_g += g; cum_r += (r or 0)
                    today_prompt += self._today_delta(conn, b, "prompt_cum", p)
                    today_gen += self._today_delta(conn, b, "gen_cum", g)
                    today_req += self._today_delta(conn, b, "requests_cum", r)
                    if mu: mem_used += mu
                    if mt: mem_total += mt
                    if cpu is not None: cpus.append(cpu)
                    if gpu is not None: gpus.append(gpu)
                    if power is not None: powers.append(power)
                avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
                live = {"cpu_pct": avg(cpus), "gpu_pct": avg(gpus),
                        "gpu_power_w": round(sum(powers), 1) if powers else None,
                        "mem_used_gb": round(mem_used, 1),
                        "mem_total_gb": round(mem_total, 1)}
                rate = 0.0
                for (b,) in boxes:
                    rows = conn.execute(
                        "SELECT ts, prompt_cum, gen_cum FROM samples WHERE box=? "
                        "AND prompt_cum IS NOT NULL AND ts<=? AND ts>=? ORDER BY ts ASC",
                        (b, last_ts, last_ts - 60),
                    ).fetchall()
                    if len(rows) >= 2:
                        dp = rows[-1][1] - rows[0][1]
                        dg = rows[-1][2] - rows[0][2]
                        dt = max(1, rows[-1][0] - rows[0][0])
                        rate += max(0, dp + dg) / dt
                live["tokens_per_sec"] = round(rate, 1) if rate else None
                self._send(200, {
                    "box": "ALL BOXES", "role": ROLE, "source": "vllm",
                    "ts": last_ts, "live": live, "boxes_online": len(boxes),
                    "today": {"prompt": today_prompt, "gen": today_gen,
                              "requests": today_req,
                              "total": today_prompt + today_gen,
                              "cost_by_model": compute_cost(prices, today_prompt, today_gen)},
                    "cumulative": {"prompt": cum_p, "gen": cum_g, "requests": cum_r,
                                   "total": cum_p + cum_g},
                    "prices": prices,
                })
        finally:
            conn.close()

    def api_range(self):
        """Token totals over a window: /api/range?from=YYYY-MM-DD&to=YYYY-MM-DD.

        Bounds are local midnights (ISO date) or epoch seconds. Omitted
        'from' defaults to the first sample ever recorded; omitted 'to'
        defaults to now. Per-box: (last cum at/before to) - (last cum
        strictly before from), then summed across boxes. This is the
        endpoint the time-frame selector will drive.
        """
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)

        def parse_bound(val, default):
            if not val:
                return default
            val = val.strip()
            if val.isdigit():
                return int(val)
            try:
                d = date.fromisoformat(val[:10])
                return int(datetime(d.year, d.month, d.day).timestamp())
            except ValueError:
                return default

        conn = db()
        try:
            span = conn.execute(
                "SELECT min(ts), max(ts) FROM samples WHERE prompt_cum IS NOT NULL"
            ).fetchone()
            if not span or span[0] is None:
                self._send(200, {"range": None, "prompt": 0, "gen": 0,
                                 "requests": 0, "total": 0,
                                 "cost_by_model": {}})
                return

            # Named presets (driven by the dashboard's time-frame selector),
            # resolved in the server's local timezone so boundaries match the
            # daily bars. Explicit from/to params always take precedence.
            preset = params.get("preset", "all")
            today = date.today()
            midnight = lambda d: int(datetime(d.year, d.month, d.day).timestamp())
            lo_default, hi_default = span[0], span[1]
            if preset == "today":
                lo_default = midnight(today)
            elif preset == "yesterday":
                y = today - timedelta(days=1)
                lo_default, hi_default = midnight(y), midnight(today)
            elif preset in ("7d", "30d"):
                n = 7 if preset == "7d" else 30
                lo_default = midnight(today - timedelta(days=n))
            elif preset == "24h":
                lo_default = int(time.time()) - 86400

            lo = parse_bound(params.get("from"), lo_default)
            hi = parse_bound(params.get("to"), hi_default)
            hi = max(hi, lo)

            if ROLE == "box":
                boxes = [BOX_NAME]
            else:
                fb = params.get("box")
                boxes = [fb] if fb else [r[0] for r in conn.execute("SELECT name FROM boxes").fetchall()]

            prompt = gen = requests = 0
            for b in boxes:
                end = conn.execute(
                    "SELECT prompt_cum, gen_cum, requests_cum FROM samples "
                    "WHERE box=? AND prompt_cum IS NOT NULL AND ts<=? "
                    "ORDER BY ts DESC LIMIT 1", (b, hi),
                ).fetchone()
                if not end:
                    continue
                base = conn.execute(
                    "SELECT prompt_cum, gen_cum, requests_cum FROM samples "
                    "WHERE box=? AND prompt_cum IS NOT NULL AND ts<? "
                    "ORDER BY ts DESC LIMIT 1", (b, lo),
                ).fetchone()
                bp = base[0] if base else 0
                bg = base[1] if base and base[1] is not None else 0
                br = base[2] if base and base[2] is not None else 0
                prompt += max(0, end[0] - bp)
                gen += max(0, end[1] - bg)
                requests += max(0, (end[2] or 0) - br)
            prices = load_prices()
            self._send(200, {
                "box": BOX_NAME if ROLE == "box" else "ALL BOXES",
                "range": {
                    "from": datetime.fromtimestamp(lo).strftime("%Y-%m-%d %H:%M"),
                    "to": datetime.fromtimestamp(hi).strftime("%Y-%m-%d %H:%M"),
                },
                "prompt": prompt, "gen": gen, "requests": requests,
                "total": prompt + gen,
                "cost_by_model": compute_cost(prices, prompt, gen),
            })
        finally:
            conn.close()

    def _cum_prev_day(self, conn, box, ts_col):
        """Last cumulative value for this box strictly before local midnight."""
        today = date.today()
        day_start = int(datetime(today.year, today.month, today.day).timestamp())
        row = conn.execute(
            f"SELECT {ts_col} FROM samples WHERE box=? AND {ts_col} IS NOT NULL "
            f"AND ts<? ORDER BY ts DESC LIMIT 1", (box, day_start),
        ).fetchone()
        return row[0] if row else 0

    def _today_delta(self, conn, box, ts_col, last_val):
        if last_val is None:
            return 0
        base = self._cum_prev_day(conn, box, ts_col)
        return max(0, last_val - base)

    def api_timeseries(self):
        qbox = self.path.split("?", 1)[1] if "?" in self.path else ""
        box = None
        if qbox.startswith("box="):
            box = qbox[4:]
        window = 3600 * 6  # last 6h by default
        conn = db()
        try:
            where_box = "AND box=?" if box else ""
            args_box = (box,) if box else ()
            rows = conn.execute(
                f"SELECT ts, cpu_pct, mem_used_gb, gpu_pct, gpu_power_w FROM samples "
                f"WHERE ts>=? {where_box} ORDER BY ts ASC",
                (int(time.time()) - window, *args_box),
            ).fetchall()
            self._send(200, {
                "ts": [r[0] for r in rows],
                "cpu": [r[1] for r in rows],
                "mem_gb": [r[2] for r in rows],
                "gpu_pct": [r[3] for r in rows],
                "gpu_power_w": [r[4] for r in rows],
            })
        finally:
            conn.close()

    def api_days(self):
        box = None
        if "?" in self.path and self.path.split("?")[1].startswith("box="):
            box = self.path.split("?")[1][4:]
        conn = db()
        try:
            days = daily_totals(conn, box=box, days=14)
            prices = load_prices()
            for d in days:
                d["cost_by_model"] = compute_cost(prices, d["prompt"], d["gen"])
                d["total"] = d["prompt"] + d["gen"]
            self._send(200, {"days": days, "prices": prices})
        finally:
            conn.close()

    def api_boxes(self):
        conn = db()
        try:
            rows = conn.execute(
                "SELECT name, last_seen FROM boxes ORDER BY last_seen DESC"
            ).fetchall()
            out = []
            for name, seen in rows:
                r = conn.execute(
                    "SELECT prompt_cum, gen_cum FROM samples WHERE box=? AND prompt_cum IS NOT NULL "
                    "ORDER BY ts DESC LIMIT 1", (name,),
                ).fetchone()
                out.append({
                    "name": name,
                    "last_seen": seen,
                    "prompt": r[0] if r else 0,
                    "gen": r[1] if r else 0,
                })
            self._send(200, {"boxes": out})
        finally:
            conn.close()

    def api_ingest(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self._send(400, {"error": "bad json"})
            return
        box = data.get("box", "unknown")
        s = data.get("sample", {})
        conn = db()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.get("ts", int(time.time())), box,
                 s.get("cpu"), s.get("mem_used_gb"), s.get("mem_total_gb"),
                 s.get("gpu_pct"), s.get("gpu_power_w"),
                 s.get("prompt_cum"), s.get("gen_cum"), s.get("requests_cum"),
                 s.get("requests_running"), s.get("kv_cache_pct")),
            )
            conn.execute(
                "INSERT INTO boxes(name,last_seen) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
                (box, s.get("ts", int(time.time()))),
            )
            conn.commit()
        finally:
            conn.close()
        self._send(200, {"ok": True})


# ---------------------------------------------------------------- main

def main():
    conn = db()
    meta_set(conn, "box_name", BOX_NAME)
    conn.commit()
    conn.close()

    if ROLE == "box":
        sampler = Sampler()
        sampler.start()
        # periodic hub push (5s cadence): batch-push every sample newer than
        # the last one we sent (catches up after blips; keeps hub timeseries full).
        def push_loop():
            last_sent_ts = 0
            while True:
                time.sleep(5)
                try:
                    conn = db()
                    rows = conn.execute(
                        "SELECT ts, cpu_pct, mem_used_gb, mem_total_gb, gpu_pct, "
                        "gpu_power_w, prompt_cum, gen_cum, requests_cum "
                        "FROM samples WHERE box=? AND ts>? ORDER BY ts ASC",
                        (BOX_NAME, last_sent_ts),
                    ).fetchall()
                    conn.close()
                    for r in rows:
                        last_sent_ts = max(last_sent_ts, r[0])
                        push_to_hub({
                            "ts": r[0], "cpu": r[1], "mem_used_gb": r[2],
                            "mem_total_gb": r[3], "gpu_pct": r[4],
                            "gpu_power_w": r[5], "prompt_cum": r[6],
                            "gen_cum": r[7], "requests_cum": r[8],
                            "source": TOKEN_SOURCE,
                        })
                except Exception as e:
                    print(f"[hub] {e}", file=sys.stderr)
        if HUB_URL:
            threading.Thread(target=push_loop, daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"TokenBoard [{ROLE}] http://0.0.0.0:{PORT}  box={BOX_NAME} "
          f"source=auto vllm={VLLM_URL or '-'} "
          f"llama={'pinned:' + ','.join(LLAMA_PINS) if LLAMA_PINS else 'auto-detect'} "
          f"hub={HUB_URL or '-'}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
