"""Mobile-first web UI & tokenized media proxy server (pure Python stdlib)."""

import argparse
import io
import json
import os
import secrets
import socket
import threading
import time
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

from . import android
from .config import DEFAULT_CONFIG_DIR, Config, load_config
from .extractors import ExtractResult, ExtractionError, MediaAsset, extract
from .http_client import HttpClient, HttpError
from .utils import extract_url, sanitize_filename

APP_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="28" fill="#171a20"/>
  <rect x="8" y="8" width="112" height="112" rx="22" fill="#1f293d" stroke="#2f6fd0" stroke-width="4"/>
  <path d="M64 28v48m0 0l-18-18m18 18l18-18" fill="none" stroke="#7fbcf5" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M34 94h60" fill="none" stroke="#e6e9ef" stroke-width="8" stroke-linecap="round"/>
</svg>
"""

MANIFEST_JSON = json.dumps(
    {
        "name": "无水印提取",
        "short_name": "无水印提取",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f1115",
        "theme_color": "#0f1115",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    },
    ensure_ascii=False,
)

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f1115">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<title>无水印提取 · 抖音 / 小红书</title>
<style>
  :root { color-scheme: dark; --bg:#0f1115; --card:#171a20; --line:#262c37; --text:#e8ecf3; --muted:#8f98a8; --accent:#2f6fd0; --accent2:#1d3a5f; --ok:#3fb950; --err:#f85149; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; padding:18px 14px 72px; background:var(--bg); color:var(--text); font:15px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:680px; margin:0 auto; }
  .hd { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:16px; }
  h1 { font-size:20px; margin:0; letter-spacing:.2px; }
  .sub { font-size:12.5px; color:var(--muted); margin-top:2px; }
  .pill { border:1px solid var(--line); background:var(--card); color:var(--text); border-radius:999px; padding:7px 13px; font-size:12.5px; cursor:pointer; white-space:nowrap; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px; margin-bottom:14px; }
  textarea { width:100%; min-height:92px; border:1px solid var(--line); background:#0b0d11; color:var(--text); border-radius:12px; padding:12px; font:14px/1.5 inherit; resize:vertical; outline:none; }
  textarea:focus { border-color:var(--accent); }
  .row { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }
  button, a.btn { display:inline-flex; align-items:center; justify-content:center; gap:6px; min-height:44px; border-radius:11px; font:600 14.5px/1 inherit; padding:0 15px; border:1px solid var(--line); background:#222732; color:var(--text); text-decoration:none; cursor:pointer; }
  button.pri, a.btn.pri { background:var(--accent); border-color:var(--accent); color:#fff; flex:1; }
  button.sec { flex:1; }
  button:disabled { opacity:.55; cursor:not-allowed; }
  .hint { margin-top:9px; font-size:12.5px; color:var(--muted); min-height:18px; }
  .err { display:none; margin-top:10px; padding:11px 12px; border-radius:11px; background:rgba(248,81,73,.12); border:1px solid rgba(248,81,73,.35); color:#ffb3ae; font-size:13px; }
  .err.show { display:block; }
  .err button { min-height:32px; padding:0 10px; font-size:12px; margin-top:8px; }
  .meta { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; margin-bottom:10px; }
  .badge { display:inline-block; font-size:11.5px; padding:2px 8px; border-radius:999px; background:var(--accent2); color:#8ac4ff; margin-right:6px; vertical-align:1px; }
  .title { font-weight:600; font-size:15.5px; word-break:break-word; }
  .author { font-size:12.5px; color:var(--muted); margin-top:2px; }
  .acts { display:flex; gap:8px; margin:10px 0 14px; flex-wrap:wrap; }
  .vbox { background:#000; border-radius:12px; overflow:hidden; margin-bottom:10px; }
  .vbox video { width:100%; max-height:62vh; display:block; background:#000; }
  .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
  @media (min-width:560px) { .grid { grid-template-columns:repeat(3,minmax(0,1fr)); } }
  .item { background:#0e1015; border:1px solid var(--line); border-radius:12px; overflow:hidden; display:flex; flex-direction:column; }
  .thumb { position:relative; aspect-ratio:3/4; background:#0b0d11; overflow:hidden; cursor:zoom-in; }
  .thumb img { width:100%; height:100%; object-fit:cover; display:block; }
  .tag { position:absolute; left:7px; top:7px; font-size:11px; padding:2px 7px; border-radius:6px; background:rgba(0,0,0,.65); color:#fff; }
  .item-ft { padding:8px; display:flex; flex-direction:column; gap:6px; }
  .item-ft a.btn { min-height:36px; font-size:13px; width:100%; }
  .modal { position:fixed; inset:0; background:rgba(0,0,0,.72); display:none; align-items:flex-end; justify-content:center; z-index:99; padding:12px; }
  .modal.open { display:flex; }
  @media (min-width:560px) { .modal { align-items:center; } }
  .sheet { width:100%; max-width:520px; background:var(--card); border:1px solid var(--line); border-radius:18px; padding:16px; max-height:86vh; overflow:auto; }
  .sheet h2 { margin:0 0 10px; font-size:17px; }
  .field { margin-bottom:12px; }
  .field label { display:flex; justify-content:space-between; font-size:13px; color:var(--muted); margin-bottom:5px; }
  .field label b { color:var(--ok); font-weight:500; }
  .field label b.off { color:var(--muted); }
  .field textarea { min-height:66px; font-size:12.5px; }
  .chk { display:flex; align-items:center; gap:8px; font-size:13.5px; margin:8px 0 14px; }
  .lightbox { position:fixed; inset:0; background:rgba(0,0,0,.92); display:none; align-items:center; justify-content:center; z-index:120; padding:14px; }
  .lightbox.open { display:flex; }
  .lightbox img { max-width:100%; max-height:100%; object-fit:contain; border-radius:8px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="hd">
    <div>
      <h1>无水印提取</h1>
      <div class="sub">抖音 · 小红书 ｜ 最高画质 · 零广告</div>
    </div>
    <div style="display:flex;gap:6px;align-items:center">
      <button class="pill" id="stopBtn" type="button" style="display:none">关闭后台</button>
      <button class="pill" id="cfgBtn" type="button">Cookie 设置</button>
    </div>
  </div>

  <div class="card">
    <textarea id="inp" placeholder="粘贴抖音或小红书分享文案 / 链接（支持整段带文字口令）"></textarea>
    <div class="row">
      <button type="button" id="clrBtn">清空</button>
      <button type="button" class="sec" id="pasteBtn">读取剪贴板</button>
      <button type="button" class="pri" id="goBtn">提取</button>
    </div>
    <div class="hint" id="hint">提示：可直接粘贴整段分享口令，支持视频、多图图集和实况照片。</div>
    <div class="err" id="err"></div>
  </div>

  <div class="card" id="resCard" style="display:none"></div>
</div>

<div class="modal" id="cfgModal">
  <div class="sheet">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <h2 style="margin:0">Cookie 与偏好设置</h2>
      <button type="button" id="cfgClose" style="min-height:34px;padding:0 11px;font-size:12.5px">关闭</button>
    </div>
    <div class="sub" style="margin-bottom:12px">
      仅在遇到平台滑块或登录风控时填写。在 X浏览器登录对应网页后，复制请求 Cookie 粘贴到下方保存即可（留空表示不修改当前已存值）。
    </div>
    <div class="field">
      <label><span>小红书 Cookie</span><b id="xhsSt">未配置</b></label>
      <textarea id="xhsCk" placeholder="粘贴小红书 Cookie 字符串（需要清空请点下方清空按钮）"></textarea>
    </div>
    <div class="field">
      <label><span>抖音 Cookie</span><b id="dySt">未配置</b></label>
      <textarea id="dyCk" placeholder="粘贴抖音 Cookie 字符串（需要清空请点下方清空按钮）"></textarea>
    </div>
    <label class="chk">
      <input type="checkbox" id="liveChk" checked>
      <span>同时提取图集里的实况照片短视频</span>
    </label>
    <div class="row">
      <button type="button" id="clrCkBtn">清空Cookie</button>
      <button type="button" class="pri" id="saveCfgBtn">保存设置</button>
    </div>
  </div>
</div>

<div class="lightbox" id="lb"><img id="lbImg" alt=""></div>

<script>
const $ = id => document.getElementById(id);
let busy = false;
let currentJob = null;

function esc(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function showErr(msg) {
  const el = $('err');
  if (!msg) { el.classList.remove('show'); el.innerHTML = ''; return; }
  const needCk = /cookie|风控|验证|登录/i.test(msg);
  el.innerHTML = esc(msg) + (needCk ? '<div><button type="button" onclick="openCfg()">去配置 Cookie</button></div>' : '');
  el.classList.add('show');
}

function setHint(msg) { $('hint').textContent = msg || ''; }

async function runExtract(text) {
  const val = (text != null ? text : $('inp').value).trim();
  if (!val) { showErr('请先粘贴抖音或小红书分享链接'); return; }
  if (busy) return;
  busy = true;
  showErr('');
  $('goBtn').disabled = true;
  $('goBtn').textContent = '解析中…';
  setHint('正在请求目标页面并解析最高画质直链…');

  try {
    const r = await fetch('/api/extract', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: val })
    });
    const d = await r.json();
    if (!r.ok || !d.ok) {
      showErr(d.error || ('请求失败: ' + r.status));
      setHint('');
      return;
    }
    currentJob = d;
    renderResult(d);
    setHint('解析完成，可直接预览或保存到手机。');
  } catch (e) {
    showErr('连接本地服务失败: ' + e.message);
    setHint('');
  } finally {
    busy = false;
    $('goBtn').disabled = false;
    $('goBtn').textContent = '提取';
  }
}

function renderResult(d) {
  const card = $('resCard');
  const plat = d.platform === 'douyin' ? '抖音' : (d.platform === 'xiaohongshu' ? '小红书' : d.platform);
  const videos = d.assets.filter(a => a.kind === 'video');
  const images = d.assets.filter(a => a.kind === 'image');
  const lives = d.assets.filter(a => a.kind === 'live_video');

  const counts = [];
  if (videos.length) counts.push(videos.length + ' 个高清视频');
  if (images.length) counts.push(images.length + ' 张无水印原图');
  if (lives.length) counts.push(lives.length + ' 个实况视频');

  let html = '<div class="meta"><div>' +
    '<div class="title"><span class="badge">' + esc(plat) + '</span>' + esc(d.title) + '</div>' +
    '<div class="author">' + (d.author ? '作者：' + esc(d.author) + ' · ' : '') + esc(counts.join(' · ')) + '</div>' +
    '</div></div>';

  if (d.assets.length > 0) {
    html += '<div class="acts">' +
      '<button type="button" class="pri" id="saveLocalBtn">一键存进相册</button>' +
      (d.assets.length > 1 ? '<a class="btn" href="' + esc(d.zip_url) + '">打包 (.zip)</a>' : '') +
      '</div>';
  }

  for (const v of videos) {
    html += '<div class="vbox"><video controls playsinline preload="metadata" src="' + esc(v.preview_url) + '"></video></div>' +
      '<div class="acts"><a class="btn pri" href="' + esc(v.download_url) + '">保存最高画质无水印视频</a></div>';
  }

  if (images.length || lives.length) {
    const liveMap = new Map();
    lives.forEach(l => liveMap.set(l.pair_index, l));
    html += '<div class="grid">';
    images.forEach((im, idx) => {
      const lv = liveMap.get(im.index);
      html += '<div class="item">' +
        '<div class="thumb" data-src="' + esc(im.preview_url) + '">' +
        '<img loading="lazy" src="' + esc(im.preview_url) + '" alt="">' +
        '<span class="tag">#' + (idx + 1) + '</span></div>' +
        '<div class="item-ft">' +
        '<a class="btn pri" href="' + esc(im.download_url) + '">保存原图</a>' +
        (lv ? '<a class="btn" href="' + esc(lv.download_url) + '">实况视频</a>' : '') +
        '</div></div>';
    });
    if (!images.length && lives.length) {
      lives.forEach((lv, idx) => {
        html += '<div class="item"><div class="item-ft"><a class="btn pri" href="' + esc(lv.download_url) + '">实况视频 #' + (idx + 1) + '</a></div></div>';
      });
    }
    html += '</div>';
  }

  card.innerHTML = html;
  card.style.display = 'block';

  card.querySelectorAll('.thumb').forEach(el => {
    el.addEventListener('click', () => {
      $('lbImg').src = el.getAttribute('data-src');
      $('lb').classList.add('open');
    });
  });

  const saveBtn = $('saveLocalBtn');
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = '正在保存到相册…';
      try {
        const r = await fetch('/api/save/' + encodeURIComponent(d.job_id), { method: 'POST' });
        const res = await r.json();
        if (!r.ok || !res.ok) {
          showErr(res.error || '保存失败');
          saveBtn.disabled = false;
          saveBtn.textContent = '一键存进相册';
          return;
        }
        saveBtn.textContent = '已存进相册 ✓ (' + res.saved + '/' + res.total + ')';
        setHint('已直接存到 ' + res.dir + ' 并刷新相册。');
      } catch (e) {
        showErr('保存请求失败: ' + e.message);
        saveBtn.disabled = false;
        saveBtn.textContent = '一键存进相册';
      }
    });
  }
}

$('lb').addEventListener('click', () => { $('lb').classList.remove('open'); $('lbImg').src = ''; });
$('goBtn').addEventListener('click', () => runExtract());
$('clrBtn').addEventListener('click', () => { $('inp').value = ''; showErr(''); $('inp').focus(); });

$('inp').addEventListener('paste', () => {
  setTimeout(() => {
    if (/https?:\/\//i.test($('inp').value)) runExtract();
  }, 60);
});

$('pasteBtn').addEventListener('click', async () => {
  try {
    if (navigator.clipboard && navigator.clipboard.readText) {
      const txt = await navigator.clipboard.readText();
      if (txt && txt.trim()) {
        $('inp').value = txt.trim();
        runExtract(txt.trim());
        return;
      }
      setHint('剪贴板是空的，请先在抖音/小红书复制链接。');
      return;
    }
  } catch (e) {}
  $('inp').value = '';
  $('inp').focus();
  setHint('当前非 HTTPS 地址，浏览器不允许网页自动读剪贴板：请长按输入框点「粘贴」，粘贴后会自动开始提取。');
});

async function refreshCfgStatus() {
  try {
    const r = await fetch('/api/config');
    const c = await r.json();
    $('xhsSt').textContent = c.xhs_cookie_set ? '已配置' : '未配置';
    $('xhsSt').className = c.xhs_cookie_set ? '' : 'off';
    $('dySt').textContent = c.douyin_cookie_set ? '已配置' : '未配置';
    $('dySt').className = c.douyin_cookie_set ? '' : 'off';
    $('liveChk').checked = !!c.save_live_photo_video;
    $('stopBtn').style.display = c.can_shutdown ? '' : 'none';
  } catch (e) {}
}

refreshCfgStatus();

$('stopBtn').addEventListener('click', async () => {
  try { await fetch('/api/shutdown', { method: 'POST' }); } catch (e) {}
  setHint('本地服务已关闭，需要时回 Termux 敲 gw 再开。');
  $('stopBtn').style.display = 'none';
});

function openCfg() {
  refreshCfgStatus();
  $('cfgModal').classList.add('open');
}
window.openCfg = openCfg;
$('cfgBtn').addEventListener('click', openCfg);
$('cfgClose').addEventListener('click', () => $('cfgModal').classList.remove('open'));
$('cfgModal').addEventListener('click', e => { if (e.target === $('cfgModal')) $('cfgModal').classList.remove('open'); });

$('saveCfgBtn').addEventListener('click', async () => {
  const payload = { save_live_photo_video: $('liveChk').checked };
  if ($('xhsCk').value.trim()) payload.xhs_cookie = $('xhsCk').value.trim();
  if ($('dyCk').value.trim()) payload.douyin_cookie = $('dyCk').value.trim();
  await fetch('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
  $('xhsCk').value = '';
  $('dyCk').value = '';
  await refreshCfgStatus();
  $('cfgModal').classList.remove('open');
  setHint('设置已保存。');
});

$('clrCkBtn').addEventListener('click', async () => {
  await fetch('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ xhs_cookie: '', douyin_cookie: '' }) });
  $('xhsCk').value = '';
  $('dyCk').value = '';
  await refreshCfgStatus();
  setHint('已清空保存的 Cookie。');
});
</script>
</body>
</html>
"""


@dataclass
class StoredAsset:
    asset_id: str
    index: int
    pair_index: int
    stem: str
    asset: MediaAsset
    resolved_url: Optional[str] = None
    resolved_ext: Optional[str] = None


@dataclass
class StoredJob:
    job_id: str
    result: ExtractResult
    assets: List[StoredAsset] = field(default_factory=list)


class JobStore:
    """Thread-safe bounded in-memory registry of parsed media tokens (blocks open SSRF)."""

    def __init__(self, max_jobs: int = 30):
        self.max_jobs = max_jobs
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, StoredJob] = OrderedDict()
        self._assets: Dict[str, StoredAsset] = {}

    def put_result(self, result: ExtractResult) -> StoredJob:
        job_id = secrets.token_urlsafe(9)
        total = len(result.assets)
        safe_title = sanitize_filename(result.title, fallback=result.platform, max_len=42)
        short_id = sanitize_filename(result.item_id, fallback="id", max_len=16)

        stored_assets: List[StoredAsset] = []
        last_image_idx = 0
        for idx, asset in enumerate(result.assets, start=1):
            if asset.kind == "image":
                last_image_idx = idx
            pair_idx = last_image_idx if asset.kind == "live_video" else idx
            suffix = f"_{idx:02d}" if total > 1 else ""
            if asset.kind == "live_video":
                suffix += "_live"
            stem = f"{result.platform}_{safe_title}_{short_id}{suffix}"
            stored_assets.append(
                StoredAsset(
                    asset_id=secrets.token_urlsafe(10),
                    index=idx,
                    pair_index=pair_idx,
                    stem=stem,
                    asset=asset,
                )
            )

        job = StoredJob(job_id=job_id, result=result, assets=stored_assets)
        with self._lock:
            self._jobs[job_id] = job
            for sa in stored_assets:
                self._assets[sa.asset_id] = sa
            while len(self._jobs) > self.max_jobs:
                _, evicted = self._jobs.popitem(last=False)
                for sa in evicted.assets:
                    self._assets.pop(sa.asset_id, None)
        return job

    def get_job(self, job_id: str) -> Optional[StoredJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def get_asset(self, asset_id: str) -> Optional[StoredAsset]:
        with self._lock:
            return self._assets.get(asset_id)

    def candidate_urls_for(self, sa: StoredAsset) -> List[str]:
        with self._lock:
            cached = sa.resolved_url
        if not cached:
            return list(sa.asset.urls)
        return [cached] + [u for u in sa.asset.urls if u != cached]

    def remember_winning(self, sa: StoredAsset, url: str, ext: str) -> None:
        with self._lock:
            sa.resolved_url = url
            sa.resolved_ext = ext


def _build_zip_bytes(job: StoredJob, store: JobStore, client: HttpClient, config: Config) -> Tuple[str, bytes]:
    buf = io.BytesIO()
    files: List[Tuple[int, str, bytes]] = []

    def _fetch(sa: StoredAsset) -> Tuple[int, str, bytes]:
        expected = "video" if sa.asset.kind in ("video", "live_video") else "image"
        data, ext = client.fetch_media_bytes(
            store.candidate_urls_for(sa),
            default_ext=sa.asset.default_ext,
            expected_prefix=expected,
            headers=sa.asset.headers,
            mobile=sa.asset.mobile,
        )
        return sa.index, f"{sa.stem}{ext}", data

    workers = min(config.max_workers, max(1, len(job.assets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_fetch, sa) for sa in job.assets]
        for fut in as_completed(futs):
            files.append(fut.result())

    files.sort(key=lambda x: x[0])
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for _, fname, data in files:
            zf.writestr(fname, data)

    title = sanitize_filename(job.result.title, fallback=job.result.platform, max_len=36)
    zip_name = f"{job.result.platform}_{title}_{job.result.item_id}.zip"
    return zip_name, buf.getvalue()


def _save_web_config(updates: Dict[str, Any], config_dir: Optional[Path] = None) -> Config:
    cfg = load_config(config_dir)
    cfg_dir = config_dir or Path(os.environ.get("MEDIA_GRAB_CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))
    cfg_file = cfg_dir / "config.json"
    raw = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
    for key in ("douyin_cookie", "xhs_cookie"):
        if key in updates:
            raw[key] = str(updates[key] or "").strip()
    if "save_live_photo_video" in updates:
        raw["save_live_photo_video"] = bool(updates["save_live_photo_video"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return load_config(cfg_dir)


def _save_job_to_output(job: StoredJob, store: JobStore, client: HttpClient, config: Config) -> Tuple[List[Path], List[str]]:
    saved: List[Path] = []
    errors: List[str] = []

    def _dl(sa: StoredAsset) -> Path:
        expected = "video" if sa.asset.kind in ("video", "live_video") else "image"
        return client.download_candidates(
            store.candidate_urls_for(sa),
            dest_dir=config.output_dir,
            stem=sa.stem,
            default_ext=sa.asset.default_ext,
            expected_prefix=expected,
            headers=sa.asset.headers,
            mobile=sa.asset.mobile,
        )

    workers = min(config.max_workers, max(1, len(job.assets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_dl, sa): sa for sa in job.assets}
        for fut in as_completed(futs):
            sa = futs[fut]
            try:
                saved.append(fut.result())
            except Exception as e:
                errors.append(f"第{sa.index}项: {e}")

    if saved:
        android.scan_media(saved)
    return saved, errors


class WebAppState:
    """Shared server state holding job registry, config path, and activity timestamp."""

    def __init__(self, config_dir: Optional[Path] = None, can_shutdown: bool = False):
        self.config_dir = config_dir
        self.can_shutdown = can_shutdown
        self.store = JobStore()
        self.last_activity = time.monotonic()
        self.server: Optional[ThreadingHTTPServer] = None

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def client(self) -> Tuple[Config, HttpClient]:
        cfg = load_config(self.config_dir)
        return cfg, HttpClient(cfg)


def make_handler(state: WebAppState):
    """Build a request handler class bound to WebAppState."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_bytes(self, status: int, content_type: str, body: bytes, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, obj: Any) -> None:
            self._send_bytes(status, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 256 * 1024:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:
            state.touch()
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/":
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8"))
                return
            if path == "/manifest.json":
                self._send_bytes(HTTPStatus.OK, "application/manifest+json; charset=utf-8", MANIFEST_JSON.encode("utf-8"))
                return
            if path == "/icon.svg":
                self._send_bytes(HTTPStatus.OK, "image/svg+xml; charset=utf-8", APP_ICON_SVG.encode("utf-8"))
                return
            if path == "/api/config":
                cfg = load_config(state.config_dir)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "douyin_cookie_set": bool(cfg.douyin_cookie or cfg.cookies_by_domain),
                        "xhs_cookie_set": bool(cfg.xhs_cookie or cfg.cookies_by_domain),
                        "save_live_photo_video": cfg.save_live_photo_video,
                        "can_shutdown": state.can_shutdown,
                        "output_dir": str(cfg.output_dir),
                    },
                )
                return
            if path.startswith("/api/media/"):
                asset_id = path[len("/api/media/") :]
                qs = parse_qs(parsed.query)
                as_dl = qs.get("dl", [""])[0] == "1"
                self._handle_media(asset_id, as_dl)
                return
            if path.startswith("/api/zip/"):
                job_id = path[len("/api/zip/") :]
                self._handle_zip(job_id)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

        def do_POST(self) -> None:
            state.touch()
            path = urlparse(self.path).path

            if path == "/api/extract":
                try:
                    payload = self._read_json()
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求 JSON 格式有误"})
                    return
                raw_input = str(payload.get("input") or "")
                url = extract_url(raw_input)
                if not url:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "未在文本里识别到有效链接"})
                    return

                cfg, client = state.client()
                try:
                    result = extract(url, client, cfg)
                except (ExtractionError, HttpError) as e:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})
                    return
                except Exception as e:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"解析异常: {e}"})
                    return

                job = state.store.put_result(result)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "job_id": job.job_id,
                        "platform": result.platform,
                        "item_id": result.item_id,
                        "title": result.title,
                        "author": result.author,
                        "zip_url": f"/api/zip/{job.job_id}",
                        "assets": [
                            {
                                "id": sa.asset_id,
                                "index": sa.index,
                                "pair_index": sa.pair_index,
                                "kind": sa.asset.kind,
                                "preview_url": f"/api/media/{sa.asset_id}",
                                "download_url": f"/api/media/{sa.asset_id}?dl=1",
                            }
                            for sa in job.assets
                        ],
                    },
                )
                return

            if path == "/api/config":
                try:
                    payload = self._read_json()
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求 JSON 格式有误"})
                    return
                _save_web_config(payload, state.config_dir)
                self._send_json(HTTPStatus.OK, {"ok": True})
                return

            if path.startswith("/api/save/"):
                job_id = path[len("/api/save/") :]
                job = state.store.get_job(job_id)
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "任务不存在或已过期，请重新提取"})
                    return
                cfg, client = state.client()
                saved, errs = _save_job_to_output(job, state.store, client, cfg)
                if not saved:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": "; ".join(errs) or "保存失败"})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "saved": len(saved),
                        "total": len(job.assets),
                        "dir": cfg.output_dir.name,
                        "errors": errs,
                    },
                )
                return

            if path == "/api/shutdown":
                if not state.can_shutdown or state.server is None:
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "当前模式不允许远程关闭"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True})
                threading.Thread(target=state.server.shutdown, daemon=True).start()
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

        def _handle_media(self, asset_id: str, as_download: bool) -> None:
            sa = state.store.get_asset(asset_id)
            if not sa:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "资源不存在或已过期，请重新提取"})
                return

            _, client = state.client()
            expected = "video" if sa.asset.kind in ("video", "live_video") else "image"
            range_hdr = self.headers.get("Range")
            try:
                stream = client.open_media_candidates(
                    state.store.candidate_urls_for(sa),
                    default_ext=sa.asset.default_ext,
                    expected_prefix=expected,
                    headers=sa.asset.headers,
                    mobile=sa.asset.mobile,
                    range_header=range_hdr,
                )
            except Exception as e:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": f"拉取媒体失败: {e}"})
                return

            state.store.remember_winning(sa, stream.winning_url, stream.ext)
            try:
                self.send_response(stream.status)
                self.send_header("Content-Type", stream.content_type)
                if stream.content_length:
                    self.send_header("Content-Length", stream.content_length)
                else:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                if stream.content_range:
                    self.send_header("Content-Range", stream.content_range)
                if stream.accept_ranges:
                    self.send_header("Accept-Ranges", stream.accept_ranges)
                if as_download:
                    ext = sa.resolved_ext or stream.ext
                    fname = f"{sa.stem}{ext}"
                    ascii_fb = f"media_{sa.index}{ext}"
                    self.send_header(
                        "Content-Disposition",
                        f"attachment; filename=\"{ascii_fb}\"; filename*=UTF-8''{quote(fname)}",
                    )
                self.send_header("Cache-Control", "private, max-age=300")
                self.end_headers()

                self.wfile.write(stream.first_chunk)
                while True:
                    chunk = stream.resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                stream.close()

        def _handle_zip(self, job_id: str) -> None:
            job = state.store.get_job(job_id)
            if not job:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "任务不存在或已过期，请重新提取"})
                return
            cfg, client = state.client()
            try:
                zip_name, zip_bytes = _build_zip_bytes(job, state.store, client, cfg)
            except Exception as e:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": f"打包失败: {e}"})
                return

            self._send_bytes(
                HTTPStatus.OK,
                "application/zip",
                zip_bytes,
                extra={
                    "Content-Disposition": f"attachment; filename=\"media_bundle.zip\"; filename*=UTF-8''{quote(zip_name)}"
                },
            )

    return Handler


def _guess_lan_ips() -> Tuple[Optional[str], List[str]]:
    primary: Optional[str] = None
    others: List[str] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if ip and not ip.startswith("127."):
            primary = ip
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip != primary and ip not in others:
                others.append(ip)
    except OSError:
        pass
    return primary, others


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(host: str = "0.0.0.0", port: int = 8765, idle_timeout: int = 0, allow_shutdown: bool = False) -> None:
    """Start the local web UI server."""
    can_stop = allow_shutdown or idle_timeout > 0 or host == "127.0.0.1"
    state = WebAppState(can_shutdown=can_stop)
    srv = ReusableThreadingHTTPServer((host, port), make_handler(state))
    state.server = srv

    if idle_timeout > 0:
        def _watchdog() -> None:
            while True:
                time.sleep(5)
                if time.monotonic() - state.last_activity > idle_timeout:
                    print("[media-grab] 闲置超时，自动退出。")
                    srv.shutdown()
                    break

        threading.Thread(target=_watchdog, daemon=True).start()

    primary_ip, other_ips = _guess_lan_ips()
    print("=" * 56)
    print("  media-grab 网页版已启动（抖音 · 小红书无水印提取）")
    print(f"  本机访问: http://127.0.0.1:{port}")
    if host != "127.0.0.1":
        if primary_ip:
            print(f"  手机访问: http://{primary_ip}:{port}  （优先用这个，需同 Wi-Fi）")
        for ip in other_ips:
            print(f"  备用网卡: http://{ip}:{port}")
    print("  在手机 X浏览器打开后点菜单「添加到桌面」，即成独立快捷图标")
    print("  按 Ctrl+C 可停止服务")
    print("=" * 56)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[media-grab] 已停止服务。")
    finally:
        srv.server_close()


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动无水印媒体提取网页版")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0 供同局域网手机访问）")
    parser.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    parser.add_argument("--idle-timeout", type=int, default=0, help="闲置多少秒后自动退出（0 为不自动退出）")
    parser.add_argument("--allow-shutdown", action="store_true", help="允许网页右上角「关闭后台」按钮关闭服务")
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port, idle_timeout=args.idle_timeout, allow_shutdown=args.allow_shutdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
