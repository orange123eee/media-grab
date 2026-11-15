# media-grab（手机 Termux 按需自启网页版 + 命令行 · 抖音 / 小红书无水印提取）

跑在手机 **Termux** 本机上的无水印提取工具：**在 X浏览器打开网页自动启动服务，关页或切后台自动关闭，平时不占内存；一行命令从 GitHub 安装与更新**。

---

## 一、手机 Termux 一行安装（无需数据线）

> **前提**：从 **F-Droid** 或 **GitHub Releases** 安装 **Termux** 和 **Termux:API**（两个 App 必须同一个安装源，不要用 Google Play 版）。安装后给两个 App 开启系统通知权限。

打开手机里的 **Termux**，直接粘贴下面一行运行（兼容之前手动拷过旧文件夹的情况，弹窗点允许存储权限）：

```bash
rm -rf ~/media-grab && pkg update -y && pkg install -y git && git clone https://github.com/orange123eee/media-grab.git ~/media-grab && bash ~/media-grab/install.sh
```

也可以直接用 `curl` 引导安装：

```bash
curl -fsSL https://raw.githubusercontent.com/orange123eee/media-grab/main/install.sh | bash
```

---

## 二、日常用法（打开 X浏览器即自动开服务，关掉就自动停）

### 1. X浏览器「添加到桌面」一键像小程序一样用（最推荐）
安装完成时会自动在浏览器打开 **`http://127.0.0.1:8765`**，你在 **X浏览器** 里把它 **「添加到桌面」**：
- **打开即启动**：Termux 每次启动后会在本机挂一个 0 轮询轻量网关（仅阻塞等待连接，平时解析服务本身未运行）；以后你**只要从桌面或 X浏览器打开 `http://127.0.0.1:8765`，解析服务就会立刻自动拉起**，不用再手动切去 Termux 敲命令。
- **不用就自动关**：关闭网页约 **6 秒**、或切走/锁屏超过 **30 秒**（且没有正在进行的视频播放/相册保存/打包下载）后，解析工作进程会自动退出释放内存。
- **离线外壳兜底**：页面已注册本地 Service Worker (`/sw.js`)。万一手机刚重启、Termux 一次都还没开过，点桌面图标也会看到「⚡ 一键唤醒 Termux 服务」提示按钮，点一下唤起后再切回网页即可自动连上。

### 2. Termux 里的 3 个超短命令
- **`gw`**：手动唤起本机按需网关并在浏览器打开 `http://127.0.0.1:8765`。
- **`g`**：命令行极速直存（不打开网页），读取剪贴板或提示粘贴口令，直接存进 `DCIM/MediaGrab` 后退出。
- **`gup`**：一行从 GitHub 拉取最新版（保留 `config.json` 里的 Cookie 和设置）。

---

## 三、遇到滑块 / 风控时配置 Cookie

- **网页版**：点页面右上角 **「Cookie 设置」**，粘贴在 X浏览器登录后的 Cookie 点保存即可；
- **命令行**：复制 Cookie 后执行 `media-grab --save-cookie xhs` 或 `media-grab --save-cookie douyin`。

---

## 四、本地测试

```bash
python -B -m unittest discover -s tests -v
```
