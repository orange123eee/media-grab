#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

REPO_URL="${MEDIA_GRAB_REPO:-https://github.com/orange123eee/media-grab.git}"
DEFAULT_REPO_DIR="$HOME/media-grab"
UPDATE_ONLY=0
POST_CLONE=0
case "${1:-}" in
  --update)
    UPDATE_ONLY=1
    ;;
  --post-clone)
    POST_CLONE=1
    ;;
esac

# 支持 curl | bash 远程安装，或从无 .git 的旧手动目录原地重拉为 Git 仓库
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -z "$SCRIPT_DIR" ] || [ ! -d "$SCRIPT_DIR/media_grab" ] || [ ! -d "$SCRIPT_DIR/.git" ]; then
  echo "[1/5] 正在从 GitHub 拉取 media-grab 仓库..."
  pkg update -y
  pkg install -y git python termux-api
  cd "$HOME"
  if [ -d "$DEFAULT_REPO_DIR/.git" ]; then
    git -C "$DEFAULT_REPO_DIR" fetch origin main
    git -C "$DEFAULT_REPO_DIR" reset --hard origin/main
  else
    rm -rf "$DEFAULT_REPO_DIR"
    git clone "$REPO_URL" "$DEFAULT_REPO_DIR"
  fi
  exec bash "$DEFAULT_REPO_DIR/install.sh" --post-clone
fi

if [ "$UPDATE_ONLY" -eq 0 ] && [ "$POST_CLONE" -eq 0 ]; then
  echo "[1/5] 安装 Termux 基础包 (git / python / termux-api)..."
  pkg update -y
  pkg install -y git python termux-api
fi

if [ ! -d "$HOME/storage/shared" ]; then
  echo "[2/5] 申请手机存储权限（请在弹窗中点允许）..."
  termux-setup-storage
  sleep 2
else
  echo "[2/5] 存储权限已配置。"
fi

APP_DIR="$HOME/.local/share/media-grab"
BIN_DIR="$HOME/bin"
SHORTCUT_DIR="$HOME/.shortcuts"
CFG_DIR="$HOME/.config/media-grab"

echo "[3/5] 安装核心代码到 $APP_DIR ..."
mkdir -p "$APP_DIR" "$BIN_DIR" "$SHORTCUT_DIR" "$CFG_DIR"
rm -rf "$APP_DIR/media_grab"
cp -r "$SCRIPT_DIR/media_grab" "$APP_DIR/"
printf '%s' "$SCRIPT_DIR" > "$CFG_DIR/repo_dir"

if [ ! -f "$CFG_DIR/config.json" ] && [ -f "$SCRIPT_DIR/config.example.json" ]; then
  cp "$SCRIPT_DIR/config.example.json" "$CFG_DIR/config.json"
fi

echo "[4/5] 配置快捷命令与按需自启 (gw / g / gup / media-grab)..."
cp "$SCRIPT_DIR/bin/termux-url-opener" "$BIN_DIR/termux-url-opener"
cp "$SCRIPT_DIR/bin/grab-clipboard.sh" "$SHORTCUT_DIR/提图提视频.sh"
cp "$SCRIPT_DIR/bin/start-web.sh" "$SHORTCUT_DIR/无水印网页版.sh"

TARGET_BIN="${PREFIX:-$HOME/.local}/bin"
mkdir -p "$TARGET_BIN"

cat > "$TARGET_BIN/media-grab" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
export PYTHONPATH="$HOME/.local/share/media-grab${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -B -m media_grab.cli "$@"
EOF

cat > "$TARGET_BIN/g" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
export PYTHONPATH="$HOME/.local/share/media-grab${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -B -m media_grab.cli --clipboard "$@"
EOF

cp "$SCRIPT_DIR/bin/start-web.sh" "$TARGET_BIN/gw"
cp "$SCRIPT_DIR/bin/update.sh" "$TARGET_BIN/gup"
sed -i 's/\r$//' "$BIN_DIR/termux-url-opener" "$SHORTCUT_DIR/提图提视频.sh" "$SHORTCUT_DIR/无水印网页版.sh" \
  "$TARGET_BIN/gw" "$TARGET_BIN/gup"
chmod +x "$BIN_DIR/termux-url-opener" "$SHORTCUT_DIR/提图提视频.sh" "$SHORTCUT_DIR/无水印网页版.sh" \
  "$TARGET_BIN/media-grab" "$TARGET_BIN/g" "$TARGET_BIN/gw" "$TARGET_BIN/gup"

BASHRC="$HOME/.bashrc"
touch "$BASHRC"
python3 - "$BASHRC" "$TARGET_BIN/gw" <<'PYEOF'
import re
import sys
from pathlib import Path

bashrc = Path(sys.argv[1])
gw_bin = sys.argv[2]
text = bashrc.read_text(encoding="utf-8", errors="ignore") if bashrc.exists() else ""
text = re.sub(r"\n?# >>> media-grab >>>[\s\S]*?# <<< media-grab <<<\n?", "\n", text).rstrip()
block = (
    "# >>> media-grab >>>\n"
    f'if [ -x "{gw_bin}" ]; then\n'
    f'  "{gw_bin}" --daemon-only >/dev/null 2>&1 || true\n'
    "fi\n"
    "# <<< media-grab <<<\n"
)
bashrc.write_text((text + "\n\n" if text else "") + block, encoding="utf-8")
PYEOF

if [ "$UPDATE_ONLY" -eq 0 ]; then
  "$TARGET_BIN/gw" --restart || true
else
  "$TARGET_BIN/gw" --restart --daemon-only >/dev/null 2>&1 || true
fi

echo "[5/5] 安装完成！"
echo "  - 已配置 Termux 打开时自动挂起轻量网关，在 X浏览器打开 http://127.0.0.1:8765 即自动启动解析服务，关页后自动退出"
echo "  - 建议现在就在 X浏览器里把 http://127.0.0.1:8765 点菜单「添加到桌面」"
echo "  - gw  : 拉起按需网关并用浏览器打开 http://127.0.0.1:8765"
echo "  - g   : 命令行读剪贴板或粘贴链接，直接存进相册"
echo "  - gup : 一行从 GitHub 拉取最新版"
echo "  - 默认相册目录: $HOME/storage/shared/DCIM/MediaGrab"
