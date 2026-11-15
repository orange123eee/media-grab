#!/data/data/com.termux/files/usr/bin/bash
set -u

PORT="${MEDIA_GRAB_PORT:-8765}"
APP_DIR="$HOME/.local/share/media-grab"
LOG_DIR="$HOME/.cache"
mkdir -p "$LOG_DIR"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"

DAEMON_ONLY=0
RESTART=0
for arg in "$@"; do
  case "$arg" in
    --daemon-only) DAEMON_ONLY=1 ;;
    --restart) RESTART=1 ;;
  esac
done

ensure_wake_lock() {
  command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock >/dev/null 2>&1 || true
}

ensure_wake_lock

has_legacy_gate() {
  if command -v pgrep >/dev/null 2>&1 && pgrep -f "media_grab\.gate" >/dev/null 2>&1; then
    return 0
  fi
  python3 - <<'PYEOF'
import os, pathlib, sys
me, pp = os.getpid(), os.getppid()
target = "media_grab." + "gate"
for p in pathlib.Path("/proc").iterdir():
    if not p.name.isdigit():
        continue
    pid = int(p.name)
    if pid in (me, pp):
        continue
    try:
        cmd = (p / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
    except OSError:
        continue
    if target in cmd:
        sys.exit(0)
sys.exit(1)
PYEOF
}

if has_legacy_gate; then
  RESTART=1
fi

is_listening() {
  python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.4); sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT))==0 else 1)"
}

stop_existing() {
  python3 - "$PORT" <<'PYEOF'
import os, pathlib, signal, socket, sys, time
port = int(sys.argv[1])
me, pp = os.getpid(), os.getppid()
targets = ("media_grab." + "gate", "media_grab." + "web")

def find_pids():
    found = []
    proc = pathlib.Path("/proc")
    if not proc.is_dir():
        return found
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        pid = int(p.name)
        if pid in (me, pp):
            continue
        try:
            cmd = (p / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except OSError:
            continue
        if any(t in cmd for t in targets):
            found.append(pid)
    return found

pids = find_pids()
for pid in pids:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except OSError:
            pass
    s = socket.socket()
    s.settimeout(0.2)
    listening = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    if not alive and not listening:
        sys.exit(0)
    time.sleep(0.08)

for pid in find_pids():
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
PYEOF
  if command -v pkill >/dev/null 2>&1; then
    pkill -f "media_grab\.(gate|web)" 2>/dev/null || true
  fi
  sleep 0.2
}

if [ "$RESTART" -eq 1 ]; then
  stop_existing
  ensure_wake_lock
fi

if ! is_listening; then
  nohup python3 -B -m media_grab.web --host 127.0.0.1 --port "$PORT" --idle-timeout 180 --hibernate-on-idle --allow-shutdown > "$LOG_DIR/media-grab-web.log" 2>&1 &
  for _ in $(seq 1 30); do
    if is_listening; then
      break
    fi
    sleep 0.1
  done
fi

if [ "$DAEMON_ONLY" -eq 1 ]; then
  exit 0
fi

URL="http://127.0.0.1:$PORT"
if command -v termux-open-url >/dev/null 2>&1; then
  termux-open-url "$URL"
fi

echo "本地高速服务已就绪 (常驻零功耗休眠，打开网页秒开): $URL"
