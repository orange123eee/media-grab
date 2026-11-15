# media-grab（手机 Termux 本地网页版 + 命令行 · 抖音 / 小红书无水印提取）

跑在手机 **Termux** 本机上的无水印提取工具：**不用电脑开机、不用数据线拷文件、平时 0 后台、一行命令从 GitHub 更新**。

---

## 一、手机 Termux 一行安装（无需数据线）

> **前提**：从 **F-Droid** 或 **GitHub Releases** 安装 **Termux** 和 **Termux:API**（两个 App 必须同一个安装源，不要用 Google Play 版）。安装后给两个 App 开启系统通知权限。

打开手机里的 **Termux**，直接粘贴这一行运行（弹窗点允许存储权限）：

```bash
pkg install -y git && git clone https://github.com/orange123eee/media-grab.git ~/media-grab && bash ~/media-grab/install.sh
```

---

## 二、日常用法（全都在手机本机跑，用完即退）

装完以后 Termux 里直接多出 3 个超短命令：

### 1. `gw` —— 启动手机本机网页版（推荐，最像小程序）
1. 在抖音 / 小红书点 **「分享 → 复制链接」**。
2. 打开 Termux 敲 **`gw`** 回车：
   - 1 秒在本机拉起网页服务并自动用浏览器（建议设为 **X浏览器**）打开 `http://127.0.0.1:8765`；
   - 在 **X浏览器** 把这个地址「添加到桌面」，以后在页面里点 **「读取剪贴板」** 或粘贴口令，就能直接预览视频/无水印原图，点 **「一键存进相册」** 直接写进 `DCIM/MediaGrab` 并刷新相册；
   - **用完切走不用管**：10 分钟无操作会自动退出后台，也可以点右上角 **「关闭后台」** 立即关闭。

### 2. `g` —— 命令行极速直存（不打开网页）
复制链接后打开 Termux 敲 **`g`** 回车，直接读取剪贴板（若系统拦截剪贴板会提示长按粘贴）并把全部原图/视频存进相册后自动退出。

### 3. `gup` —— 一行从 GitHub 拉取更新
后续我修复解析或增加 X (Twitter) 支持推上 GitHub 后，你在 Termux 敲 **`gup`** 回车即可秒级更新，**已保存的 Cookie 和设置不会丢失**。

---

## 三、遇到滑块 / 风控时配置 Cookie

- **网页版**：点页面右上角 **「Cookie 设置」**，粘贴在 X浏览器登录后的 Cookie 点保存即可；
- **命令行**：复制 Cookie 后执行 `media-grab --save-cookie xhs` 或 `media-grab --save-cookie douyin`。

---

## 四、本地测试

```bash
python -B -m unittest discover -s tests -v
```
