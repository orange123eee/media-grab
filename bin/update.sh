#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

CFG_DIR="$HOME/.config/media-grab"
REPO_DIR="$HOME/media-grab"
if [ -f "$CFG_DIR/repo_dir" ]; then
  REPO_DIR="$(cat "$CFG_DIR/repo_dir")"
fi

if [ ! -d "$REPO_DIR/.git" ]; then
  echo "未找到 Git 仓库目录: $REPO_DIR"
  echo "请重新执行安装命令拉取仓库。"
  exit 1
fi

echo "[gup] 正在从 GitHub 拉取更新..."
git -C "$REPO_DIR" fetch origin main
git -C "$REPO_DIR" reset --hard origin/main
bash "$REPO_DIR/install.sh" --update
echo "[gup] 已更新到最新版本，Cookie 和设置保持不变。"
