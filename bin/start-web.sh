#!/data/data/com.termux/files/usr/bin/bash
set -u

PORT="${MEDIA_GRAB_PORT:-8765}"
WORKER_PORT=$((PORT + 1))
APP_DIR="$HOME/.local/share/media-grab"
LOG_DIR="$HOME/.cache"
mkdir -p "$LOG_DIR"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"

DAEMON_ONLY=0
if [ "${1:-}" = "--daemon-only" ]; then
  DAEMON_ONLY=1
fi

is_listening() {
  python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.4); sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT))==0 else 1)"
}

if ! is_listening; then
  nohup python3 -B -m media_grab.gate --host 127.0.0.1 --port "$PORT" --worker-port "$WORKER_PORT" --idle-timeout 90 > "$LOG_DIR/media-grab-gate.log" 2>&1 &
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

echo "本地按需网关已就绪 (打开网页自动启服务，关页约 6 秒自动关闭): $URL"
