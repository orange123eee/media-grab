#!/data/data/com.termux/files/usr/bin/bash
set -u

PORT="${MEDIA_GRAB_PORT:-8765}"
APP_DIR="$HOME/.local/share/media-grab"
LOG_DIR="$HOME/.cache"
mkdir -p "$LOG_DIR"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"

is_listening() {
  python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.4); sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT))==0 else 1)"
}

if ! is_listening; then
  nohup python3 -m media_grab.web --host 127.0.0.1 --port "$PORT" --idle-timeout 600 > "$LOG_DIR/media-grab-web.log" 2>&1 &
  for _ in $(seq 1 30); do
    if is_listening; then
      break
    fi
    sleep 0.1
  done
fi

URL="http://127.0.0.1:$PORT"
if command -v termux-open-url >/dev/null 2>&1; then
  termux-open-url "$URL"
fi

echo "本地网页版已就绪 (10 分钟无操作自动退出): $URL"
