#!/usr/bin/env python3
"""TokenBoard Windows probe - read-only environment report.

Run on the Windows workstation (Python 3.8+, no deps, no admin):

    python scripts\\win_probe.py

Writes win_probe_report.txt in the current directory and prints it.
Does not install or modify anything; HTTP checks are read-only GETs
against localhost only. Paste/upload win_probe_report.txt back.

Also runs on Linux/macOS (useful as a sanity check of the HTTP scan).
"""
import ctypes
import csv
import io
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import urllib.request

HTTP_TIMEOUT = 1.0
TCP_TIMEOUT = 0.3
# well-known inference-server ports, probed even when netstat misses them:
# LM Studio default, Ollama default, llama-server default, vLLM default
COMMON_PORTS = [1234, 11434, 8080, 8000, 8001, 8081]
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

L = []  # report lines


def section(title):
    L.append("")
    L.append("=" * 60)
    L.append(f"== {title}")
    L.append("=" * 60)


def ok(fn, *a, **kw):
    """Run fn, return (True, result) or (False, 'ErrType: msg')."""
    try:
        return True, fn(*a, **kw)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- system

def report_system():
    section("system")
    L.append(f"platform    : {platform.platform()}")
    L.append(f"os.name     : {os.name}")
    L.append(f"python      : {sys.version.splitlines()[0]}")
    L.append(f"executable  : {sys.executable}")
    L.append(f"cwd         : {os.getcwd()}")
    L.append(f"admin/elev. : " + (
        str(bool(ctypes.windll.shell32.IsUserAnAdmin()))
        if os.name == "nt" else "n/a"))


# ---------------------------------------------------------------- cpu (ctypes)

class _FILETIME(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]


def _ft(v):
    return (v.hi << 32) | v.lo


def win_cpu_sample():
    idle, kern, user = _FILETIME(), _FILETIME(), _FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
        raise ctypes.WinError()
    busy = _ft(kern) + _ft(user) - _ft(idle)
    total = _ft(kern) + _ft(user)
    return busy, total, _ft(idle)


def report_cpu():
    section("cpu - kernel32.GetSystemTimes (TokenBoard's planned source)")
    if os.name != "nt":
        L.append("skipped (not Windows)")
        return
    okA, a = ok(win_cpu_sample)
    if not okA:
        L.append(f"FAIL first sample: {a}")
        return
    time.sleep(1.0)
    okB, b = ok(win_cpu_sample)
    if not okB:
        L.append(f"FAIL second sample: {b}")
        return
    busy = b[0] - a[0]
    total = b[1] - a[1]
    pct = 100.0 * busy / total if total else 0.0
    L.append(f"GetSystemTimes: OK  (busy% over 1s = {pct:.1f})")


# ---------------------------------------------------------------- mem (ctypes)

class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def report_mem():
    section("memory - kernel32.GlobalMemoryStatusEx (planned source)")
    if os.name != "nt":
        L.append("skipped (not Windows)")
        return
    def probe():
        m = _MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            raise ctypes.WinError()
        gb = 1024 ** 3
        return (round(m.ullTotalPhys / gb, 2),
                round((m.ullTotalPhys - m.ullAvailPhys) / gb, 2))
    good, r = ok(probe)
    if good:
        L.append(f"GlobalMemoryStatusEx: OK  (used {r[1]} GB / total {r[0]} GB)")
    else:
        L.append(f"FAIL: {r}")


# ---------------------------------------------------------------- nvidia-smi

def report_gpu():
    section("nvidia-smi (TokenBoard's GPU util/power source)")
    def probe():
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,power.draw,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW)
        if p.returncode != 0:
            raise RuntimeError(f"exit {p.returncode}: {p.stderr.strip()[:200]}")
        return p.stdout.strip()
    good, r = ok(probe)
    L.append("nvidia-smi: OK" if good else f"nvidia-smi: NOT USABLE -> {r}")
    if good:
        for line in r.splitlines():
            L.append(f"  {line}")


# ---------------------------------------------------------------- listening ports

def win_listening():
    """[(port, pid, image)] LISTENing TCP sockets."""
    pids = {}
    out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                         capture_output=True, text=True, errors="replace",
                         timeout=20, creationflags=CREATE_NO_WINDOW).stdout
    rows = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 5 and f[3].upper() == "LISTENING":
            try:
                port = int(f[1].rsplit(":", 1)[1])
                rows.append((port, int(f[4])))
            except ValueError:
                continue
    names = {}
    try:
        tl = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                            capture_output=True, text=True, errors="replace",
                            timeout=20, creationflags=CREATE_NO_WINDOW).stdout
        for row in csv.reader(io.StringIO(tl)):
            if len(row) >= 2:
                try:
                    names[int(row[1])] = row[0]
                except ValueError:
                    pass
    except Exception:
        pass
    seen = set()
    out_rows = []
    for port, pid in sorted(rows):
        key = (port, pid)
        if key in seen:
            continue
        seen.add(key)
        out_rows.append((port, pid, names.get(pid, "?")))
    return out_rows


def linux_listening():
    rows = set()
    for net in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(net) as f:
                next(f, None)
                for line in f:
                    p = line.split()
                    if len(p) < 10 or p[3] != "0A":
                        continue
                    port = int(p[1].rsplit(":", 1)[1], 16)
                    pid = int(p[7] if len(p) > 7 else 0) or -1
                    rows.add((port, pid))
        except OSError:
            continue
    out = []
    for port, pid in sorted(rows):
        name = "?"
        try:
            with open(f"/proc/{pid}/comm") as f:
                name = f.read().strip()
        except Exception:
            pass
        out.append((port, pid, name))
    return out


INTERESTING = re.compile(
    r"llama|ollama|lm[-_ ]?studio|lmstudio|vllm|python|node", re.I)


def report_ports():
    section("listening TCP ports (pid -> image)")
    fn = win_listening if os.name == "nt" else linux_listening
    good, rows = ok(fn)
    if not good:
        L.append(f"FAIL: {rows}")
        return [], set()
    for port, pid, name in rows:
        mark = "  <-- interesting" if INTERESTING.search(name) else ""
        L.append(f"  :{port:<6} pid {pid:<8} {name}{mark}")
    return rows, {p for p, _, _ in rows}


# ---------------------------------------------------------------- http scan

def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tokenboard-probe/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.status, r.read(400_000).decode("utf-8", "replace")


def http_error_line(path, e):
    """Classify a failed /metrics request with as much detail as we can get."""
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        try:
            snippet = e.read(160).decode("utf-8", "replace").strip()
        except Exception:
            snippet = ""
        return f"  {path} -> HTTP {e.code} {e.reason}; body: {snippet[:140]!r}"
    return f"  {path} -> {type(e).__name__}: {str(e)[:120]}"


def tcp_open(port):
    """Fast gate: does anything actually accept connections on this port?"""
    try:
        s = socket.create_connection(("127.0.0.1", port), TCP_TIMEOUT)
        s.close()
        return True
    except OSError:
        return False


def classify(port):
    """Probe one port: what backend does it look like? Returns text lines."""
    lines = []
    # /metrics
    st, body = None, None
    err = "  /metrics -> no-response"
    try:
        st, body = http_get(f"http://127.0.0.1:{port}/metrics")
    except Exception as e:
        err = http_error_line("/metrics", e)
    if body is not None:
        prefixes = sorted(set(re.findall(r"^([A-Za-z_][\w]*):", body, re.M)))
        lines.append(f"  /metrics -> HTTP {st}, prefixes: {prefixes or '(none)'}")
        tok = sorted(set(re.findall(r"^(?:llamacpp|vllm|ollama|llm)[:\w]*token[\w]*",
                                    body, re.M)))[:12]
        lines.append(f"  token counters: {tok or 'NONE FOUND'}")
        if "llamacpp:" in body:
            lines.append("  CLASS: llama.cpp-compatible /metrics (TokenBoard-ready)")
        elif "vllm:" in body:
            lines.append("  CLASS: vLLM")
    else:
        lines.append(err)
    # /slots
    try:
        st, body = http_get(f"http://127.0.0.1:{port}/slots")
        data = json.loads(body)
        if isinstance(data, list):
            keys = sorted(data[0].keys())[:12] if data and isinstance(data[0], dict) else []
            lines.append(f"  /slots   -> HTTP {st}, {len(data)} slot(s), keys: {keys}")
            lines.append("  CLASS: llama.cpp-compatible API (TokenBoard /slots works)")
    except Exception:
        pass
    # /api/ps (Ollama)
    try:
        st, body = http_get(f"http://127.0.0.1:{port}/api/ps")
        data = json.loads(body)
        models = [m.get("name") for m in data.get("models", [])]
        lines.append(f"  /api/ps  -> HTTP {st}, loaded models: {models}")
        lines.append("  CLASS: Ollama")
    except Exception:
        pass
    return lines


NOISE = ("URLError", "TimeoutError", "ConnectionRefusedError", "no-response")


def classify_guarded(port):
    if not tcp_open(port):
        return port, []
    return port, classify(port)


def report_scan(ports_seen):
    section("localhost HTTP scan (metrics / slots / ollama)")
    ports = sorted(ports_seen | set(COMMON_PORTS))
    if not ports:
        L.append("no ports to scan")
        return
    from concurrent.futures import ThreadPoolExecutor
    results = {}
    quiet = []
    L.append(f"scanning {len(ports)} port(s) in parallel "
             f"(closed ports are skipped after a {TCP_TIMEOUT}s TCP check)...")
    with ThreadPoolExecutor(max_workers=16) as ex:
        for port, lines in ex.map(classify_guarded, ports):
            real = [x for x in lines
                    if "->" in x and not any(n in x for n in NOISE)]
            results[port] = real
            print(f"  scanned :{port}"
                  + (f"  <- HTTP server, {len(real)} finding(s)" if real else ""),
                  flush=True)
    verdict = False
    for port in ports:
        if results[port]:
            verdict = True
            L.append(f"port {port}:")
            L.extend(results[port])
        else:
            quiet.append(str(port))
    if quiet:
        L.append("closed / no HTTP of interest: " + ", ".join(quiet))
    if not verdict:
        L.append("")
        L.append("No inference server detected. Start llama-server / LM Studio's")
        L.append("local server / Ollama, then re-run this probe.")


# ---------------------------------------------------------------- main

def main():
    L.append("TokenBoard Windows Probe")
    L.append(f"run at: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(os.getcwd(), f"win_probe_report_{stamp}.txt")
    try:
        for name, step in [
            ("system info", report_system),
            ("cpu (1 s sample)", report_cpu),
            ("memory", report_mem),
            ("nvidia-smi", report_gpu),
        ]:
            print(f"[{name}]...", flush=True)
            step()
        print("[listening ports]...", flush=True)
        rows, ports_seen = report_ports()
        print("[http scan]... (Ctrl+C is safe - saves the partial report)",
              flush=True)
        report_scan(ports_seen)
        section("what this tells us")
        L.append("- GetSystemTimes / GlobalMemoryStatusEx OK  -> CPU/mem tiles work")
        L.append("- nvidia-smi OK                             -> GPU util/power work")
        L.append("- llama.cpp CLASS port                      -> token tracking works")
        L.append("  (auto-discovery is designed from the netstat rows above)")
        L.append("- Ollama CLASS                              -> needs /metrics check above")
        L.append("- 'NOT USABLE' lines are the gaps TokenBoard's Windows support must fill")
    except KeyboardInterrupt:
        L.append("")
        L.append("!! interrupted by Ctrl+C - above is the partial report")
    except BaseException:
        import traceback
        L.append("")
        L.append("!! probe crashed - traceback below, partial report above")
        L.append(traceback.format_exc())
    text = "\n".join(L) + "\n"
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        written = out
    except OSError:
        # cwd unwritable (or whatever) - fall back to next to the script
        out2 = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"win_probe_report_{stamp}.txt")
        with open(out2, "w", encoding="utf-8") as f:
            f.write(text)
        written = out2
    print(text)
    print(f"[report written to: {written}]", flush=True)
    if any("!!" in x for x in L[-3:]):
        sys.exit(1)


if __name__ == "__main__":
    main()
