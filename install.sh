#!/usr/bin/env bash
# TokenBoard installer - clone, run this, open a browser. No root, no pip.
#
# Usage:
#   ./install.sh                      # defaults: box on :8100, vLLM on :8000
#   ./install.sh --port 8100 \
#   ./install.sh --vllm-url http://127.0.0.1:8001 \
#   ./install.sh --box-name gb10-2 --hub-url http://10.0.1.5:8101
#   ./install.sh --hub                # also install a local hub on :8101
#   ./install.sh --no-systemd         # run via nohup instead of user services
#
set -euo pipefail
cd "$(dirname "$0")"

PORT=8100
HUB_PORT=8101
VLLM_URL=""
BOX_NAME=""
HUB_URL=""
WITH_HUB=0
NO_SYSTEMD=0

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --port)      PORT="$2"; shift 2;;
    --hub-port)  HUB_PORT="$2"; shift 2;;
    --vllm-url)  VLLM_URL="$2"; shift 2;;
    --box-name)  BOX_NAME="$2"; shift 2;;
    --hub-url)   HUB_URL="$2"; shift 2;;
    --hub)       WITH_HUB=1; shift;;
    --no-systemd) NO_SYSTEMD=1; shift;;
    -h|--help)   usage;;
    *) echo "unknown flag: $1 (try --help)"; exit 2;;
  esac
done

say()  { printf '\n\033[1;32m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m   ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m   x %s\033[0m\n' "$*"; exit 1; }

say "TokenBoard install"

# ---------------------------------------------------------------- python
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 not found on PATH - Python 3.10+ is the only requirement"
PYVER=$("$PYTHON" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || die "python3 $PYVER is too old - need 3.10+"
echo "   python3 $PYVER OK ($PYTHON)  - stdlib only, nothing to pip install"

# ---------------------------------------------------------------- vLLM detection
detect_vllm() {
  for u in "$@"; do
    [ -n "$u" ] || continue
    if "$PYTHON" - "$u" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    t = urllib.request.urlopen(sys.argv[1].rstrip("/") + "/metrics", timeout=5).read().decode()
    sys.exit(0 if "vllm:prompt_tokens_total" in t else 1)
except Exception:
    sys.exit(1)
PY
    then echo "$u"; return 0; fi
  done
  return 1
}

if [ -z "$VLLM_URL" ]; then
  VLLM_URL=$(detect_vllm "http://127.0.0.1:8000" "http://127.0.0.1:8001") || VLLM_URL=""
fi
if [ -n "$VLLM_URL" ]; then
  echo "   vLLM found at $VLLM_URL (token counting enabled)"
else
  warn "no vLLM /metrics endpoint found on :8000/:8001 (or --vllm-url given)"
  warn "continuing anyway - TokenBoard samples GPU/CPU/memory now and starts"
  warn "counting tokens automatically the moment vLLM or llama-server comes up"
  warn "(llama-server is auto-discovered on any local port via /proc; pin one"
  warn " with llama_url in config.json if it runs inside Docker)."
  VLLM_URL="http://127.0.0.1:8000"
fi

# ---------------------------------------------------------------- box name
[ -n "$BOX_NAME" ] || BOX_NAME=$("$PYTHON" -c 'import socket; print(socket.gethostname())')
echo "   box name: $BOX_NAME"

# ---------------------------------------------------------------- config
if [ -f config.json ]; then
  warn "config.json already exists - leaving it untouched"
else
  "$PYTHON" - "$PORT" "$BOX_NAME" "$VLLM_URL" "$HUB_URL" <<'PY'
import json, sys
port, box_name, vllm_url, hub_url = sys.argv[1:5]
cfg = json.load(open("config.example.json"))
cfg.update({"box_name": box_name, "port": int(port),
            "vllm_url": vllm_url, "hub_url": hub_url})
json.dump(cfg, open("config.json", "w"), indent=2)
PY
  echo "   wrote config.json (edit later; restart service to apply)"
fi

if [ "$WITH_HUB" = 1 ]; then
  if [ -f config-hub.json ]; then
    warn "config-hub.json already exists - leaving it untouched"
  else
    "$PYTHON" - "$HUB_PORT" <<'PY'
import json, sys
cfg = json.load(open("config-hub.example.json"))
cfg["port"] = int(sys.argv[1])
json.dump(cfg, open("config-hub.json", "w"), indent=2)
PY
    echo "   wrote config-hub.json (hub on port $HUB_PORT)"
  fi
fi

# ---------------------------------------------------------------- prices
say "Frontier prices"
if timeout 25 "$PYTHON" scripts/refresh_prices.py; then
  echo "   live prices fetched and written to prices.json"
else
  warn "live price fetch failed (offline?) - keeping the bundled snapshot"
  warn "in prices.json; the dashboard shows its 'verified' date. Re-run later:"
  warn "   python3 scripts/refresh_prices.py"
fi

# ---------------------------------------------------------------- services
mkdir -p logs
if [ "$NO_SYSTEMD" = 1 ]; then
  say "Starting (nohup mode)"
  nohup "$PYTHON" tokenboard.py >> logs/tokenboard-box.log 2>&1 &
  echo "   box  started (pid $!)  -> http://localhost:$PORT"
  [ "$WITH_HUB" = 1 ] && { nohup env TOKENBOARD_CONFIG=config-hub.json "$PYTHON" tokenboard.py >> logs/tokenboard-hub.log 2>&1 & echo "   hub  started (pid $!)  -> http://localhost:$HUB_PORT"; }
elif systemctl --user daemon-reload 2>/dev/null; then
  say "Installing user systemd services (auto-start on boot, no root)"
  mkdir -p "$HOME/.config/systemd/user"
  sed "s|@ROOT@|$(pwd)|g; s|@PORT@|$PORT|g; s|@PYTHON@|$PYTHON|g" \
    deploy/tokenboard-box.service.in > "$HOME/.config/systemd/user/tokenboard-box.service"
  systemctl --user enable --now tokenboard-box.service
  echo "   tokenboard-box.service enabled + running"
  if [ "$WITH_HUB" = 1 ]; then
    sed "s|@ROOT@|$(pwd)|g; s|@HUB_PORT@|$HUB_PORT|g; s|@PYTHON@|$PYTHON|g" \
      deploy/tokenboard-hub.service.in > "$HOME/.config/systemd/user/tokenboard-hub.service"
    systemctl --user enable --now tokenboard-hub.service
    echo "   tokenboard-hub.service enabled + running"
  fi
  echo "   (reboot-proof; logs: journalctl --user -u tokenboard-box)"
else
  warn "user systemd unavailable - starting via nohup instead (no auto-start)"
  nohup "$PYTHON" tokenboard.py >> logs/tokenboard-box.log 2>&1 &
  echo "   box started (pid $!)"
fi

# ---------------------------------------------------------------- health check
say "Health check"
LAN_IP=$("$PYTHON" - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80))
    print(s.getsockname()[0])
except Exception:
    print(socket.gethostbyname(socket.gethostname()))
finally:
    s.close()
PY
)
ok=0
for _ in $(seq 1 30); do
  if "$PYTHON" - "$PORT" <<'PY' 2>/dev/null
import json, sys, urllib.request
d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/state", timeout=3))
sys.exit(0 if "today" in d or d.get("state") == "empty" else 1)
PY
  then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || die "dashboard did not come up - check: journalctl --user -u tokenboard-box (or logs/tokenboard-box.log)"

printf '\n\033[1;32m== TokenBoard is live\033[0m\n'
echo "   this machine :  http://localhost:$PORT"
echo "   on the LAN   :  http://$LAN_IP:$PORT"
if [ "$WITH_HUB" = 1 ]; then echo "   hub (all boxes):  http://$LAN_IP:$HUB_PORT"; fi
echo
echo "   stop later:  systemctl --user disable --now tokenboard-box   (or: kill \$(pgrep -f tokenboard.py))"
