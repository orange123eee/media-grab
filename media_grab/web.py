"""Mobile-first web UI & tokenized media proxy server (pure Python stdlib)."""

import argparse
import gc
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
from contextlib import contextmanager
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
from .utils import dedupe_keep_order, extract_url, sanitize_filename

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

SW_JS = """const CACHE_NAME = 'media-grab-shell-v3';
const SHELL_URLS = ['/', '/manifest.json', '/icon.svg'];

function fetchWithTimeout(req, ms) {
  const ctrl = new AbortController();
  const tid = setTimeout(() => ctrl.abort(), ms);
  return fetch(req, { signal: ctrl.signal, cache: 'no-store' }).finally(() => clearTimeout(tid));
}

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS)).catch(() => {})
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
      .catch(() => {})
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;

  if (req.mode === 'navigate' || url.pathname === '/') {
    event.respondWith(
      fetchWithTimeout(req, 450)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put('/', copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => caches.match('/').then((cached) => cached || new Response(
          '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="background:#0b0e14;color:#eef2f8;font-family:sans-serif;padding:24px"><h3>本地 Termux 服务被系统暂停</h3><p>点下方按钮唤起 Termux 后刷新本页即可：</p><p><a href="intent://#Intent;package=com.termux;end" style="display:inline-block;padding:12px 18px;background:#2563eb;color:#fff;border-radius:10px;text-decoration:none">⚡ 一键唤醒 Termux 服务</a></p></body>',
          { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
        )))
    );
    return;
  }

  if (url.pathname === '/manifest.json' || url.pathname === '/icon.svg') {
    event.respondWith(
      fetchWithTimeout(req, 450)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(url.pathname, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => caches.match(url.pathname))
    );
  }
});
"""

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0e14">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<title>无水印提取 · 抖音 / 小红书</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0b0e14;
    --card: rgba(23, 27, 36, 0.9);
    --card-solid: #171b24;
    --line: rgba(255, 255, 255, 0.08);
    --line-hi: rgba(96, 165, 250, 0.45);
    --text: #eef2f8;
    --muted: #8b95a8;
    --accent: #3b82f6;
    --accent-hi: #2563eb;
    --accent-soft: rgba(59, 130, 246, 0.16);
    --ok: #3fb950;
    --ok-soft: rgba(63, 185, 80, 0.14);
    --warn: #e3b341;
    --err: #f85149;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0;
    padding: 16px 14px 86px;
    background:
      radial-gradient(600px 320px at 15% -10%, rgba(59, 130, 246, .18), transparent 65%),
      radial-gradient(500px 260px at 90% 5%, rgba(255, 36, 66, .12), transparent 65%),
      var(--bg);
    color: var(--text);
    font: 15px/1.55 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    min-height: 100vh;
  }
  .wrap { max-width: 680px; margin: 0 auto; }
  .hd { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 14px; }
  .brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .logo {
    width: 38px; height: 38px; border-radius: 12px; flex-shrink: 0;
    background: linear-gradient(135deg, #2563eb, #06b6d4);
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 6px 18px rgba(37, 99, 235, .3);
  }
  .logo svg { width: 22px; height: 22px; }
  h1 { font-size: 19px; margin: 0; letter-spacing: .2px; }
  .sub { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; margin-top: 1px; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); display: inline-block; transition: background .2s; }
  .dot.on { background: var(--ok); box-shadow: 0 0 8px rgba(63, 185, 80, .65); }
  .dot.off { background: var(--err); }
  .hd-acts { display: flex; gap: 6px; align-items: center; flex-shrink: 0; }
  .pill {
    border: 1px solid var(--line); background: var(--card); color: var(--text);
    border-radius: 999px; padding: 7px 12px; font-size: 12.5px; font-weight: 500;
    cursor: pointer; white-space: nowrap; min-height: 34px;
  }
  .pill:active { opacity: .8; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 18px;
    padding: 15px; margin-bottom: 14px; backdrop-filter: blur(12px);
    box-shadow: 0 8px 28px rgba(0, 0, 0, .24);
  }
  textarea {
    width: 100%; min-height: 92px; border: 1px solid var(--line); background: rgba(9, 12, 18, .85);
    color: var(--text); border-radius: 13px; padding: 12px; font: 14px/1.5 inherit;
    resize: vertical; outline: none; transition: border-color .18s, box-shadow .18s;
  }
  textarea:focus { border-color: var(--line-hi); box-shadow: 0 0 0 3px rgba(59, 130, 246, .15); }
  .detect {
    display: none; align-items: center; justify-content: space-between; gap: 8px;
    margin-top: 9px; padding: 8px 11px; border-radius: 10px; font-size: 12.5px;
    background: var(--ok-soft); border: 1px solid rgba(63, 185, 80, .28); color: #9ae6b4;
  }
  .detect.warn { background: rgba(227, 179, 65, .1); border-color: rgba(227, 179, 65, .28); color: #f6d57a; }
  .detect.show { display: flex; }
  .detect-txt { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .detect-btn {
    border: 1px solid rgba(255,255,255,.16); background: rgba(255,255,255,.08); color: #fff;
    border-radius: 7px; padding: 4px 8px; font-size: 11.5px; min-height: 26px; cursor: pointer; flex-shrink: 0;
  }
  .row { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
  button, a.btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    min-height: 44px; border-radius: 12px; font: 600 14.5px/1 inherit; padding: 0 15px;
    border: 1px solid var(--line); background: #222836; color: var(--text);
    text-decoration: none; cursor: pointer; transition: transform .08s, opacity .15s, background .15s;
  }
  button:active, a.btn:active { transform: scale(.985); }
  button.pri, a.btn.pri {
    background: linear-gradient(135deg, var(--accent), var(--accent-hi));
    border-color: transparent; color: #fff; flex: 1.25;
    box-shadow: 0 4px 14px rgba(37, 99, 235, .28);
  }
  button.sec { flex: 1; }
  button:disabled { opacity: .55; cursor: not-allowed; transform: none; }
  .prog { display: none; height: 3px; border-radius: 3px; background: rgba(255,255,255,.06); overflow: hidden; margin-top: 11px; }
  .prog.show { display: block; }
  .prog > i {
    display: block; height: 100%; width: 42%; border-radius: 3px;
    background: linear-gradient(90deg, transparent, #60a5fa, #38bdf8, transparent);
    animation: slide 1.1s ease-in-out infinite;
  }
  @keyframes slide { 0% { transform: translateX(-110%); } 100% { transform: translateX(260%); } }
  .hint { margin-top: 9px; font-size: 12.5px; color: var(--muted); min-height: 18px; }
  .err { display: none; margin-top: 10px; padding: 11px 12px; border-radius: 11px; background: rgba(248, 81, 73, .12); border: 1px solid rgba(248, 81, 73, .35); color: #ffb3ae; font-size: 13px; }
  .err.show { display: block; }
  .err button { min-height: 32px; padding: 0 10px; font-size: 12px; margin-top: 8px; }
  .meta { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; margin-bottom: 10px; }
  .badge { display: inline-block; font-size: 11.5px; font-weight: 600; padding: 2px 9px; border-radius: 999px; background: var(--accent-soft); color: #8ac4ff; margin-right: 6px; vertical-align: 1px; }
  .badge.xhs { background: rgba(255, 36, 66, .18); color: #ff6b81; }
  .badge.dy { background: linear-gradient(135deg, rgba(37, 244, 238, .18), rgba(254, 44, 85, .18)); color: #6ef3ef; }
  .title { font-weight: 600; font-size: 15.5px; word-break: break-word; }
  .author { font-size: 12.5px; color: var(--muted); margin-top: 3px; }
  .acts { display: flex; gap: 8px; margin: 10px 0 14px; flex-wrap: wrap; }
  .vbox { background: #000; border-radius: 13px; overflow: hidden; margin-bottom: 10px; border: 1px solid var(--line); }
  .vbox video { width: 100%; max-height: 62vh; display: block; background: #000; }
  .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  @media (min-width: 560px) { .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
  .item { background: #0e1118; border: 1px solid var(--line); border-radius: 13px; overflow: hidden; display: flex; flex-direction: column; }
  .thumb {
    position: relative; aspect-ratio: 3/4; overflow: hidden; cursor: zoom-in;
    background: linear-gradient(110deg, #121620 25%, #1a202e 45%, #121620 65%);
    background-size: 200% 100%; animation: skel 1.3s linear infinite;
  }
  @keyframes skel { 0% { background-position: 180% 0; } 100% { background-position: -20% 0; } }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; opacity: 0; transition: opacity .22s ease; }
  .thumb img.loaded { opacity: 1; }
  .tag { position: absolute; left: 7px; top: 7px; font-size: 11px; padding: 2px 7px; border-radius: 6px; background: rgba(0,0,0,.62); color: #fff; backdrop-filter: blur(4px); }
  .item-ft { padding: 8px; display: flex; flex-direction: column; gap: 6px; }
  .item-ft a.btn { min-height: 36px; font-size: 12.5px; width: 100%; border-radius: 9px; }
  .hist-hd { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
  .hist-hd b { font-size: 14px; }
  .hist-clr { background: transparent; border: none; color: var(--muted); font-size: 12px; min-height: 28px; padding: 0 6px; cursor: pointer; }
  .hist-list { display: flex; flex-direction: column; gap: 7px; }
  .hist-row {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 9px 11px; border-radius: 11px; border: 1px solid var(--line); background: rgba(11, 14, 20, .7);
    cursor: pointer; text-align: left; width: 100%; min-height: 42px;
  }
  .hist-row:active { background: rgba(59, 130, 246, .12); }
  .hist-title { font-size: 13px; font-weight: 500; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
  .hist-time { font-size: 11.5px; color: var(--muted); flex-shrink: 0; }
  .modal { position: fixed; inset: 0; background: rgba(0, 0, 0, .74); display: none; align-items: flex-end; justify-content: center; z-index: 99; padding: 12px; }
  .modal.open { display: flex; }
  @media (min-width: 560px) { .modal { align-items: center; } }
  .sheet { width: 100%; max-width: 520px; background: var(--card-solid); border: 1px solid var(--line); border-radius: 18px; padding: 16px; max-height: 86vh; overflow: auto; }
  .sheet h2 { margin: 0 0 10px; font-size: 17px; }
  .field { margin-bottom: 12px; }
  .field label { display: flex; justify-content: space-between; font-size: 13px; color: var(--muted); margin-bottom: 5px; }
  .field label b { color: var(--ok); font-weight: 500; }
  .field label b.off { color: var(--muted); }
  .field textarea { min-height: 66px; font-size: 12.5px; }
  .chk { display: flex; align-items: center; gap: 8px; font-size: 13.5px; margin: 8px 0 14px; }
  .lightbox { position: fixed; inset: 0; background: rgba(0, 0, 0, .94); display: none; flex-direction: column; align-items: center; justify-content: space-between; z-index: 120; padding: 14px; }
  .lightbox.open { display: flex; }
  .lb-top { width: 100%; display: flex; justify-content: space-between; align-items: center; color: #fff; font-size: 14px; }
  .lb-stage { flex: 1; width: 100%; display: flex; align-items: center; justify-content: center; position: relative; min-height: 0; margin: 8px 0; }
  .lb-stage img { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 8px; }
  .lb-nav {
    position: absolute; top: 50%; transform: translateY(-50%);
    width: 40px; height: 48px; border-radius: 10px; border: 1px solid rgba(255,255,255,.18);
    background: rgba(0,0,0,.45); color: #fff; font-size: 22px; min-height: 40px; padding: 0;
  }
  .lb-nav.prev { left: 4px; }
  .lb-nav.next { right: 4px; }
  .lb-ft { width: 100%; max-width: 360px; display: flex; gap: 8px; }
  .toast {
    position: fixed; left: 50%; bottom: 24px; transform: translate(-50%, 16px);
    background: rgba(24, 29, 40, .96); border: 1px solid rgba(255,255,255,.14);
    color: #fff; padding: 10px 16px; border-radius: 999px; font-size: 13px;
    opacity: 0; pointer-events: none; transition: opacity .18s, transform .18s; z-index: 150;
    box-shadow: 0 8px 24px rgba(0,0,0,.45); white-space: nowrap; max-width: 90vw; overflow: hidden; text-overflow: ellipsis;
  }
  .toast.show { opacity: 1; transform: translate(-50%, 0); }
</style>
</head>
<body>
<div class="wrap">
  <div class="hd">
    <div class="brand">
      <div class="logo">
        <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 4v11m0 0l-4.5-4.5M12 15l4.5-4.5M5 19h14"/>
        </svg>
      </div>
      <div>
        <h1>无水印提取</h1>
        <div class="sub"><span class="dot" id="statusDot"></span><span id="statusTxt">正在连接本地网关…</span></div>
      </div>
    </div>
    <div class="hd-acts">
      <button class="pill" id="stopBtn" type="button" style="display:none">关闭后台</button>
      <button class="pill" id="cfgBtn" type="button">Cookie 设置</button>
    </div>
  </div>

  <div class="card" id="wakeBox" style="display:none;border-color:rgba(248,81,73,.45)">
    <div style="font-weight:600;margin-bottom:4px">本地解析网关尚未连接</div>
    <div class="sub" style="margin-bottom:10px;display:block">如果手机刚重启或还没有打开过 Termux，点下方按钮唤起 Termux 后切回本页即可自动恢复连接。</div>
    <div class="row" style="margin-top:0">
      <a class="btn pri" href="intent://#Intent;package=com.termux;end">⚡ 一键唤醒 Termux 服务</a>
      <button type="button" id="retryConnBtn">重试连接</button>
    </div>
  </div>

  <div class="card">
    <textarea id="inp" placeholder="直接粘贴抖音 / 小红书分享文案或链接（不用手动删掉里面的中文，系统会自动识别）"></textarea>
    <div class="detect" id="detectBar">
      <span class="detect-txt" id="detectTxt"></span>
      <button type="button" class="detect-btn" id="trimUrlBtn" style="display:none">仅留纯链接</button>
    </div>
    <div class="row">
      <button type="button" id="clrBtn">清空</button>
      <button type="button" class="sec" id="pasteBtn">读取剪贴板</button>
      <button type="button" class="pri" id="goBtn">提取无水印</button>
    </div>
    <div class="prog" id="prog"><i></i></div>
    <div class="hint" id="hint">提示：粘贴整段口令后会自动识别链接，图集默认先秒开轻量预览、保存时下载最高画质无水印原图。</div>
    <div class="err" id="err"></div>
  </div>

  <div class="card" id="resCard" style="display:none"></div>

  <div class="card" id="histCard" style="display:none">
    <div class="hist-hd">
      <b>最近提取记录</b>
      <button type="button" class="hist-clr" id="clrHistBtn">清空记录</button>
    </div>
    <div class="hist-list" id="histList"></div>
  </div>
</div>

<div class="modal" id="cfgModal">
  <div class="sheet">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <h2 style="margin:0">Cookie 与偏好设置</h2>
      <button type="button" id="cfgClose" style="min-height:34px;padding:0 11px;font-size:12.5px">关闭</button>
    </div>
    <div class="sub" style="margin-bottom:12px;display:block">
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

<div class="lightbox" id="lb">
  <div class="lb-top">
    <span id="lbCount">1 / 1</span>
    <button type="button" id="lbClose" style="min-height:34px;padding:0 12px;font-size:13px">关闭</button>
  </div>
  <div class="lb-stage" id="lbStage">
    <button type="button" class="lb-nav prev" id="lbPrev">‹</button>
    <img id="lbImg" alt="">
    <button type="button" class="lb-nav next" id="lbNext">›</button>
  </div>
  <div class="lb-ft">
    <a class="btn pri" id="lbDl" href="#">保存本张无损原图</a>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
const HIST_KEY = 'mg_history_v1';
let busy = false;
let serverOnline = false;
let currentJob = null;
let lbImages = [];
let lbIdx = 0;
let toastTimer = null;
let timerId = null;

function esc(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function showToast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
}

function setOnline(ok, txt) {
  serverOnline = !!ok;
  $('statusDot').className = 'dot ' + (ok ? 'on' : 'off');
  $('statusTxt').textContent = txt || (ok ? '按需服务就绪 · 关页自动停止' : '未连接本地服务');
}

function showWakeBanner(show) {
  const box = $('wakeBox');
  if (box) box.style.display = show ? 'block' : 'none';
}

function showErr(msg) {
  const el = $('err');
  if (!msg) { el.classList.remove('show'); el.innerHTML = ''; return; }
  const needCk = /cookie|风控|验证|登录/i.test(msg);
  el.innerHTML = esc(msg) + (needCk ? '<div><button type="button" onclick="openCfg()">去配置 Cookie</button></div>' : '');
  el.classList.add('show');
}

function setHint(msg) { $('hint').textContent = msg || ''; }

function clientExtractUrl(text) {
  const m = String(text || '').match(/https?:\/\/[A-Za-z0-9\-._~:\/?#[\]@!$&'()*+,;=%]+/g);
  if (!m) return null;
  const cleaned = m.map(u => u.replace(/[.,;:!?)"']+$/, '')).filter(Boolean);
  const prio = ['douyin.com', 'iesdouyin.com', 'xhslink.com', 'xhslink.cn', 'xiaohongshu.com', 'x.com', 'twitter.com'];
  for (const u of cleaned) {
    try {
      const h = new URL(u).hostname.toLowerCase();
      if (prio.some(d => h === d || h.endsWith('.' + d))) return { url: u, host: h };
    } catch (e) {}
  }
  if (!cleaned.length) return null;
  try { return { url: cleaned[0], host: new URL(cleaned[0]).hostname.toLowerCase() }; } catch (e) { return { url: cleaned[0], host: '' }; }
}

function updateDetectBar() {
  const raw = $('inp').value.trim();
  const bar = $('detectBar');
  const txt = $('detectTxt');
  const trimBtn = $('trimUrlBtn');
  if (!raw) {
    bar.className = 'detect';
    trimBtn.style.display = 'none';
    return;
  }
  const found = clientExtractUrl(raw);
  if (!found) {
    bar.className = 'detect warn show';
    txt.textContent = '未在内容里发现有效 http/https 链接，请确认已复制完整分享内容';
    trimBtn.style.display = 'none';
    return;
  }
  let plat = '链接';
  if (found.host.includes('douyin')) plat = '抖音';
  else if (found.host.includes('xhs') || found.host.includes('xiaohongshu')) plat = '小红书';
  else if (found.host === 'x.com' || found.host.endsWith('.x.com') || found.host.includes('twitter') || found.host === 't.co') plat = 'X';
  bar.className = 'detect show';
  const hasExtra = raw !== found.url;
  txt.textContent = (hasExtra ? '✓ 已自动忽略文案，识别到【' : '✓ 已识别【') + plat + '】' + found.url;
  trimBtn.style.display = hasExtra ? 'inline-flex' : 'none';
  trimBtn.dataset.url = found.url;
}

$('inp').addEventListener('input', updateDetectBar);
$('trimUrlBtn').addEventListener('click', () => {
  const u = $('trimUrlBtn').dataset.url;
  if (u) { $('inp').value = u; updateDetectBar(); showToast('已精简为纯链接'); }
});

async function fetchJsonWithRetry(url, options, timeoutMs = 22000, retries = 1) {
  let lastErr = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(url, Object.assign({}, options || {}, { signal: ctrl.signal }));
      const data = await r.json();
      return { res: r, data };
    } catch (e) {
      lastErr = e;
      if (e && e.name === 'AbortError') {
        throw new Error('请求超时（超过 ' + Math.round(timeoutMs / 1000) + ' 秒），请检查网络或 Cookie');
      }
      if (attempt < retries) {
        await new Promise(resolve => setTimeout(resolve, 250));
      }
    } finally {
      clearTimeout(tid);
    }
  }
  throw lastErr || new Error('网络请求失败');
}

function saveHistory(d, inputUrl) {
  try {
    const list = JSON.parse(localStorage.getItem(HIST_KEY) || '[]').filter(x => x.url !== inputUrl);
    const plat = d.platform === 'douyin' ? '抖音' : (d.platform === 'xiaohongshu' ? '小红书' : d.platform);
    const now = new Date();
    const hhmm = String(now.getHours()).padStart(2, '0') + ':' + String(now.getMinutes()).padStart(2, '0');
    list.unshift({ platform: d.platform, platName: plat, title: d.title || inputUrl, url: inputUrl, time: hhmm });
    localStorage.setItem(HIST_KEY, JSON.stringify(list.slice(0, 8)));
    renderHistory();
  } catch (e) {}
}

function renderHistory() {
  try {
    const list = JSON.parse(localStorage.getItem(HIST_KEY) || '[]');
    const card = $('histCard');
    const box = $('histList');
    if (!list.length) { card.style.display = 'none'; box.innerHTML = ''; return; }
    box.innerHTML = list.map((it, i) => {
      const cls = it.platform === 'xiaohongshu' ? 'xhs' : (it.platform === 'douyin' ? 'dy' : '');
      return '<button type="button" class="hist-row" data-idx="' + i + '">' +
        '<span class="badge ' + cls + '">' + esc(it.platName || it.platform) + '</span>' +
        '<span class="hist-title">' + esc(it.title) + '</span>' +
        '<span class="hist-time">' + esc(it.time || '') + '</span>' +
        '</button>';
    }).join('');
    box.querySelectorAll('.hist-row').forEach(btn => {
      btn.addEventListener('click', () => {
        const it = list[Number(btn.dataset.idx)];
        if (!it) return;
        $('inp').value = it.url;
        updateDetectBar();
        runExtract(it.url);
        window.scrollTo({ top: 0, behavior: 'smooth' });
      });
    });
    card.style.display = 'block';
  } catch (e) {}
}
renderHistory();
$('clrHistBtn').addEventListener('click', () => {
  try { localStorage.removeItem(HIST_KEY); } catch (e) {}
  renderHistory();
  showToast('已清空记录');
});

async function runExtract(text) {
  const val = (text != null ? text : $('inp').value).trim();
  if (!val) { showErr('请先粘贴抖音或小红书分享链接'); return; }
  if (busy) return;
  busy = true;
  showErr('');
  $('goBtn').disabled = true;
  $('goBtn').textContent = '解析中…';
  $('prog').classList.add('show');

  const t0 = performance.now();
  const baseHint = '正在请求目标页面并解析最高画质无水印直链… ';
  setHint(baseHint + '0.0s');
  clearInterval(timerId);
  timerId = setInterval(() => {
    setHint(baseHint + ((performance.now() - t0) / 1000).toFixed(1) + 's');
  }, 100);

  try {
    if (!serverOnline) {
      try {
        await fetchJsonWithRetry('/api/heartbeat', { method: 'POST' }, 1500, 0);
        showWakeBanner(false);
        setOnline(true, '按需服务就绪 · 关页自动停止');
      } catch (hbErr) {
        showWakeBanner(true);
        setOnline(false, '服务未连接');
        showErr('本地 Termux 服务未响应（可能被安卓后台冻结），请点上方「⚡ 一键唤醒 Termux 服务」后重试');
        setHint('');
        return;
      }
    }

    const { res: r, data: d } = await fetchJsonWithRetry('/api/extract', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: val })
    }, 22000, 1);

    if (!r.ok || !d.ok) {
      showErr(d.error || ('请求失败: ' + r.status));
      setHint('');
      return;
    }
    currentJob = d;
    showWakeBanner(false);
    setOnline(true, '按需服务就绪 · 关页自动停止');
    const found = clientExtractUrl(val);
    saveHistory(d, (found && found.url) || val);
    renderResult(d);
    const sec = ((performance.now() - t0) / 1000).toFixed(2);
    setHint('解析完成（耗时 ' + sec + 's），预览已开启加速，保存将直接下载无损原图。');
    showToast('解析成功 (' + sec + 's)');
  } catch (e) {
    const msg = e && e.message ? e.message : '连接失败';
    let alive = true;
    try {
      await fetchJsonWithRetry('/api/heartbeat', { method: 'POST' }, 1000, 0);
    } catch (_) {
      alive = false;
    }
    if (!alive || !/超时/.test(msg)) {
      showWakeBanner(true);
      setOnline(false, '服务未连接');
    }
    showErr('提取失败: ' + msg);
    setHint('');
  } finally {
    busy = false;
    clearInterval(timerId);
    $('prog').classList.remove('show');
    $('goBtn').disabled = false;
    $('goBtn').textContent = '提取无水印';
  }
}

function showLightbox(idx) {
  if (!lbImages.length) return;
  lbIdx = (idx + lbImages.length) % lbImages.length;
  const im = lbImages[lbIdx];
  $('lbCount').textContent = (lbIdx + 1) + ' / ' + lbImages.length;
  $('lbDl').href = im.download_url;
  const thumb = im.thumb_url || im.preview_url;
  $('lbImg').src = thumb;
  if (im.preview_url && im.preview_url !== thumb) {
    const hi = new Image();
    const targetIdx = lbIdx;
    hi.onload = () => { if (lbIdx === targetIdx && $('lb').classList.contains('open')) $('lbImg').src = im.preview_url; };
    hi.src = im.preview_url;
  }
  $('lbPrev').style.display = lbImages.length > 1 ? '' : 'none';
  $('lbNext').style.display = lbImages.length > 1 ? '' : 'none';
  $('lb').classList.add('open');
}

function renderResult(d) {
  const card = $('resCard');
  const plat = d.platform === 'douyin' ? '抖音' : (d.platform === 'xiaohongshu' ? '小红书' : d.platform);
  const badgeCls = d.platform === 'xiaohongshu' ? 'xhs' : (d.platform === 'douyin' ? 'dy' : '');
  const videos = d.assets.filter(a => a.kind === 'video');
  const images = d.assets.filter(a => a.kind === 'image');
  const lives = d.assets.filter(a => a.kind === 'live_video');
  lbImages = images;

  const counts = [];
  if (videos.length) counts.push(videos.length + ' 个高清视频');
  if (images.length) counts.push(images.length + ' 张无水印原图');
  if (lives.length) counts.push(lives.length + ' 个实况视频');

  let html = '<div class="meta"><div>' +
    '<div class="title"><span class="badge ' + badgeCls + '">' + esc(plat) + '</span>' + esc(d.title) + '</div>' +
    '<div class="author">' + (d.author ? '作者：' + esc(d.author) + ' · ' : '') + esc(counts.join(' · ')) + '</div>' +
    '</div></div>';

  if (d.assets.length > 0) {
    html += '<div class="acts">' +
      '<button type="button" class="pri" id="saveLocalBtn">📥 一键全部存进相册</button>' +
      (d.assets.length > 1 ? '<a class="btn" href="' + esc(d.zip_url) + '">📦 打包 (.zip)</a>' : '') +
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
      const thumbSrc = im.thumb_url || im.preview_url;
      html += '<div class="item">' +
        '<div class="thumb" data-idx="' + idx + '">' +
        '<img loading="lazy" src="' + esc(thumbSrc) + '" onload="this.classList.add(\'loaded\')" onerror="this.classList.add(\'loaded\')" alt="">' +
        '<span class="tag">#' + (idx + 1) + '</span></div>' +
        '<div class="item-ft">' +
        '<a class="btn pri" href="' + esc(im.download_url) + '">保存无损原图</a>' +
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
    el.addEventListener('click', () => showLightbox(Number(el.dataset.idx || 0)));
  });

  const saveBtn = $('saveLocalBtn');
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = '正在下载无损原图到相册…';
      try {
        const { res: r, data: res } = await fetchJsonWithRetry('/api/save/' + encodeURIComponent(d.job_id), { method: 'POST' }, 60000, 1);
        if (!r.ok || !res.ok) {
          showErr(res.error || '保存失败');
          saveBtn.disabled = false;
          saveBtn.textContent = '📥 一键全部存进相册';
          return;
        }
        saveBtn.textContent = '已存进相册 ✓ (' + res.saved + '/' + res.total + ')';
        setHint('已直接保存到 ' + res.dir + ' 并刷新相册。');
        showToast('已存进相册 (' + res.saved + '/' + res.total + ')');
      } catch (e) {
        showErr('保存请求失败: ' + e.message);
        saveBtn.disabled = false;
        saveBtn.textContent = '📥 一键全部存进相册';
      }
    });
  }
}

$('lbClose').addEventListener('click', () => { $('lb').classList.remove('open'); $('lbImg').src = ''; });
$('lbPrev').addEventListener('click', e => { e.stopPropagation(); showLightbox(lbIdx - 1); });
$('lbNext').addEventListener('click', e => { e.stopPropagation(); showLightbox(lbIdx + 1); });
$('lb').addEventListener('click', e => { if (e.target === $('lb') || e.target === $('lbStage')) { $('lb').classList.remove('open'); $('lbImg').src = ''; } });
let touchX = null;
$('lbStage').addEventListener('touchstart', e => { touchX = e.touches && e.touches[0] ? e.touches[0].clientX : null; }, { passive: true });
$('lbStage').addEventListener('touchend', e => {
  if (touchX == null || !e.changedTouches || !e.changedTouches[0]) return;
  const dx = e.changedTouches[0].clientX - touchX;
  touchX = null;
  if (Math.abs(dx) > 45) showLightbox(dx > 0 ? lbIdx - 1 : lbIdx + 1);
}, { passive: true });

$('goBtn').addEventListener('click', () => runExtract());
$('clrBtn').addEventListener('click', () => { $('inp').value = ''; updateDetectBar(); showErr(''); $('inp').focus(); });

$('inp').addEventListener('paste', () => {
  setTimeout(() => {
    updateDetectBar();
    if (/https?:\/\//i.test($('inp').value)) runExtract();
  }, 60);
});

$('pasteBtn').addEventListener('click', async () => {
  try {
    if (navigator.clipboard && navigator.clipboard.readText) {
      const txt = await navigator.clipboard.readText();
      if (txt && txt.trim()) {
        $('inp').value = txt.trim();
        updateDetectBar();
        runExtract(txt.trim());
        return;
      }
      setHint('剪贴板是空的，请先在抖音/小红书复制链接。');
      showToast('剪贴板是空的');
      return;
    }
  } catch (e) {}
  $('inp').value = '';
  updateDetectBar();
  $('inp').focus();
  setHint('当前浏览器限制网页自动读剪贴板：请长按输入框点「粘贴」，粘贴后会自动开始提取。');
});

async function refreshCfgStatus(fast = false) {
  try {
    const { res: r, data: c } = await fetchJsonWithRetry('/api/config', {}, fast ? 1200 : 3500, 0);
    if (!r.ok) return;
    $('xhsSt').textContent = c.xhs_cookie_set ? '已配置' : '未配置';
    $('xhsSt').className = c.xhs_cookie_set ? '' : 'off';
    $('dySt').textContent = c.douyin_cookie_set ? '已配置' : '未配置';
    $('dySt').className = c.douyin_cookie_set ? '' : 'off';
    $('liveChk').checked = !!c.save_live_photo_video;
    $('stopBtn').style.display = c.can_shutdown ? '' : 'none';
    showWakeBanner(false);
    setOnline(true, '按需服务就绪 · 关页自动停止');
  } catch (e) {
    showWakeBanner(true);
    setOnline(false, '网关未连接 · 请点击唤醒');
  }
}

refreshCfgStatus(true);

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

function sendHeartbeat() {
  fetch('/api/heartbeat', { method: 'POST', keepalive: true })
    .then(r => { if (r.ok) { showWakeBanner(false); setOnline(true, '按需服务就绪 · 关页自动停止'); } })
    .catch(() => {});
}

setInterval(() => {
  if (document.visibilityState === 'visible') sendHeartbeat();
}, 20000);

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    if (navigator.sendBeacon) navigator.sendBeacon('/api/leave?mode=hidden', '');
  } else {
    refreshCfgStatus(true);
    sendHeartbeat();
  }
});

window.addEventListener('pagehide', () => {
  if (navigator.sendBeacon) navigator.sendBeacon('/api/leave?mode=close', '');
});

if ($('retryConnBtn')) {
  $('retryConnBtn').addEventListener('click', () => {
    refreshCfgStatus();
    sendHeartbeat();
  });
}

$('stopBtn').addEventListener('click', async () => {
  try { await fetch('/api/shutdown', { method: 'POST' }); } catch (e) {}
  setOnline(false, '服务已休眠 · 刷新即醒');
  setHint('本次解析服务已关闭；下次打开或刷新网页会自动唤醒。');
  $('stopBtn').style.display = 'none';
  showToast('已关闭解析服务');
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
  await fetchJsonWithRetry('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) }, 8000, 1);
  $('xhsCk').value = '';
  $('dyCk').value = '';
  await refreshCfgStatus();
  $('cfgModal').classList.remove('open');
  setHint('设置已保存。');
  showToast('设置已保存');
});

$('clrCkBtn').addEventListener('click', async () => {
  await fetchJsonWithRetry('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ xhs_cookie: '', douyin_cookie: '' }) }, 8000, 1);
  $('xhsCk').value = '';
  $('dyCk').value = '';
  await refreshCfgStatus();
  setHint('已清空保存的 Cookie。');
  showToast('已清空 Cookie');
});
</script>
</body>
</html>
"""

APP_ICON_SVG_BYTES = APP_ICON_SVG.encode("utf-8")
MANIFEST_JSON_BYTES = MANIFEST_JSON.encode("utf-8")
SW_JS_BYTES = SW_JS.encode("utf-8")
INDEX_HTML_BYTES = INDEX_HTML.encode("utf-8")


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

    def thumb_urls_for(self, sa: StoredAsset) -> List[str]:
        if sa.asset.thumb_urls:
            return dedupe_keep_order(list(sa.asset.thumb_urls) + list(sa.asset.urls))
        return self.candidate_urls_for(sa)

    def remember_winning(self, sa: StoredAsset, url: str, ext: str) -> None:
        with self._lock:
            sa.resolved_url = url
            sa.resolved_ext = ext

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._assets.clear()


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
    """Shared server state holding job registry, config path, and activity lifecycle."""

    def __init__(self, config_dir: Optional[Path] = None, can_shutdown: bool = False, hibernate_only: bool = False):
        self.config_dir = config_dir
        self.can_shutdown = can_shutdown
        self.hibernate_only = hibernate_only
        self.hibernated = False
        self.store = JobStore()
        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self.active_requests = 0
        self.leave_deadline: Optional[float] = None
        self.last_activity = time.monotonic()
        self.server: Optional[ThreadingHTTPServer] = None

    def touch(self) -> None:
        with self._lock:
            self.last_activity = time.monotonic()
            self.leave_deadline = None
            self.hibernated = False
            self._wake_event.set()

    def mark_leave(self, delay_sec: float) -> None:
        with self._lock:
            self.leave_deadline = time.monotonic() + max(0.1, delay_sec)
            self._wake_event.set()

    def hibernate(self) -> None:
        with self._lock:
            if self.active_requests > 0:
                return
            self.store.clear()
            self.leave_deadline = None
            self.hibernated = True
            self._wake_event.clear()
        gc.collect()

    @contextmanager
    def busy(self):
        with self._lock:
            self.active_requests += 1
            self.last_activity = time.monotonic()
            self.leave_deadline = None
            self.hibernated = False
            self._wake_event.set()
        try:
            yield
        finally:
            with self._lock:
                self.active_requests = max(0, self.active_requests - 1)
                self.last_activity = time.monotonic()

    def is_hibernated(self) -> bool:
        with self._lock:
            return self.hibernated

    def should_shutdown(self, idle_timeout: int) -> bool:
        with self._lock:
            if self.active_requests > 0:
                return False
            now = time.monotonic()
            if self.leave_deadline is not None and now >= self.leave_deadline:
                return True
            if idle_timeout > 0 and (now - self.last_activity) > idle_timeout:
                return True
            return False

    def client(self) -> Tuple[Config, HttpClient]:
        cfg = load_config(self.config_dir)
        return cfg, HttpClient(cfg)


def make_handler(state: WebAppState):
    """Build a request handler class bound to WebAppState."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 15

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
            parsed = urlparse(self.path)
            path = parsed.path
            if path != "/sw.js":
                state.touch()

            if path in ("/", "/index.html"):
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", INDEX_HTML_BYTES)
                return
            if path == "/manifest.json":
                self._send_bytes(HTTPStatus.OK, "application/manifest+json; charset=utf-8", MANIFEST_JSON_BYTES)
                return
            if path == "/icon.svg":
                self._send_bytes(HTTPStatus.OK, "image/svg+xml; charset=utf-8", APP_ICON_SVG_BYTES)
                return
            if path == "/sw.js":
                self._send_bytes(HTTPStatus.OK, "application/javascript; charset=utf-8", SW_JS_BYTES)
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
                as_thumb = qs.get("thumb", [""])[0] == "1"
                self._handle_media(asset_id, as_dl, as_thumb=as_thumb)
                return
            if path.startswith("/api/zip/"):
                job_id = path[len("/api/zip/") :]
                self._handle_zip(job_id)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

        def _drain_body(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            if 0 < length <= 65536:
                self.rfile.read(length)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path != "/api/leave":
                state.touch()

            if path == "/api/heartbeat":
                self._drain_body()
                self._send_json(HTTPStatus.OK, {"ok": True})
                return

            if path == "/api/leave":
                self._drain_body()
                mode = parse_qs(parsed.query).get("mode", ["hidden"])[0]
                delay = 8.0 if mode == "close" else 60.0
                state.mark_leave(delay)
                self._send_json(HTTPStatus.OK, {"ok": True, "mode": mode})
                return

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
                    with state.busy():
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
                                "thumb_url": (
                                    f"/api/media/{sa.asset_id}?thumb=1"
                                    if sa.asset.kind == "image"
                                    else f"/api/media/{sa.asset_id}"
                                ),
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
                with state.busy():
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
                if not state.can_shutdown or (not state.hibernate_only and state.server is None):
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "当前模式不允许远程关闭"})
                    return
                if state.hibernate_only:
                    state.hibernate()
                    self._send_json(HTTPStatus.OK, {"ok": True, "hibernated": True})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True})
                threading.Thread(target=state.server.shutdown, daemon=True).start()
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

        def _handle_media(self, asset_id: str, as_download: bool, as_thumb: bool = False) -> None:
            sa = state.store.get_asset(asset_id)
            if not sa:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "资源不存在或已过期，请重新提取"})
                return

            _, client = state.client()
            expected = "video" if sa.asset.kind in ("video", "live_video") else "image"
            range_hdr = self.headers.get("Range")
            urls = (
                state.store.thumb_urls_for(sa)
                if (as_thumb and sa.asset.thumb_urls)
                else state.store.candidate_urls_for(sa)
            )
            with state.busy():
                try:
                    stream = client.open_media_candidates(
                        urls,
                        default_ext=sa.asset.default_ext,
                        expected_prefix=expected,
                        headers=sa.asset.headers,
                        mobile=sa.asset.mobile,
                        range_header=range_hdr,
                    )
                except Exception as e:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": f"拉取媒体失败: {e}"})
                    return

                if not as_thumb:
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
                with state.busy():
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


def serve(
    host: str = "0.0.0.0",
    port: int = 8765,
    idle_timeout: int = 0,
    allow_shutdown: bool = False,
    hibernate_on_idle: bool = False,
) -> None:
    """Start the local web UI server."""
    can_stop = allow_shutdown or idle_timeout > 0 or host == "127.0.0.1"
    state = WebAppState(can_shutdown=can_stop, hibernate_only=hibernate_on_idle)
    srv = ReusableThreadingHTTPServer((host, port), make_handler(state))
    state.server = srv
    android.wake_lock()

    if idle_timeout > 0:
        def _watchdog() -> None:
            while True:
                if hibernate_on_idle and state.is_hibernated():
                    state._wake_event.wait()
                time.sleep(1)
                if state.should_shutdown(idle_timeout):
                    if hibernate_on_idle:
                        state.hibernate()
                    else:
                        print("[media-grab] 页面已关闭或闲置超时，自动退出。")
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
        android.wake_unlock()


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动无水印媒体提取网页版")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0 供同局域网手机访问）")
    parser.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    parser.add_argument("--idle-timeout", type=int, default=0, help="闲置多少秒后自动退出（0 为不自动退出）")
    parser.add_argument("--allow-shutdown", action="store_true", help="允许网页右上角「关闭后台」按钮关闭服务")
    parser.add_argument(
        "--hibernate-on-idle",
        action="store_true",
        help="闲置或关闭请求时清理内存进入休眠而不退出进程（重开网页秒级响应）",
    )
    args = parser.parse_args(argv)
    serve(
        host=args.host,
        port=args.port,
        idle_timeout=args.idle_timeout,
        allow_shutdown=args.allow_shutdown,
        hibernate_on_idle=args.hibernate_on_idle,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
