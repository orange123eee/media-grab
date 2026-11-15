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

command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock >/dev/null 2>&1 || true

if command -v pgrep >/dev/null 2>&1 && pgrep -f "media_grab\.gate" >/dev/null 2>&1; then
  RESTART=1
fi

is_listening() {
  python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.4); sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT))==0 else 1)"
}

if [ "$RESTART" -eq 1 ]; then
  if command -v pkill >/dev/null 2>&1; then
    pkill -f "media_grab\.(gate|web)" 2>/dev/null || true
    sleep 0.3
  fi
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
