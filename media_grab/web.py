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
from typing import Any, Dict, List, Optional, Sequence, Tuple
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

SW_JS = """const CACHE_NAME = 'media-grab-shell-v7';
const SHELL_URLS = ['/', '/manifest.json', '/icon.svg'];
const SW_OFFLINE_HTML = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="background:#0b0e14;color:#eef2f8;font-family:sans-serif;padding:24px;line-height:1.6"><h3>本地 Termux 服务被系统暂停</h3><p style="color:#9aa7bd">正在自动检测并恢复连接；若 3 秒后仍未跳转，请点下方按钮唤起 Termux 后切回本页（建议在系统设置将 Termux 省电策略设为「无限制」）：</p><p><a href="intent://#Intent;package=com.termux;end" style="display:inline-block;padding:12px 18px;background:#2563eb;color:#fff;border-radius:10px;text-decoration:none">⚡ 一键唤醒 Termux 服务</a> <button onclick="location.replace(\\'/\\')" style="padding:12px 16px;background:#1e2636;color:#eef2f8;border:1px solid #334155;border-radius:10px;margin-left:8px">立即刷新</button></p><script>function p(){fetch("/api/config",{cache:"no-store"}).then(function(r){if(r.ok)location.replace("/")}).catch(function(){})}p();setInterval(p,1200);document.addEventListener("visibilitychange",function(){if(document.visibilityState==="visible")p()})</script></body>';

function fetchWithTimeout(reqOrUrl, ms) {
  const ctrl = new AbortController();
  const tid = setTimeout(() => ctrl.abort(), ms);
  const target = typeof reqOrUrl === 'string' ? reqOrUrl : reqOrUrl.url;
  return fetch(target, { signal: ctrl.signal, cache: 'no-store', credentials: 'same-origin' })
    .finally(() => clearTimeout(tid));
}

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => Promise.all(
        SHELL_URLS.map((u) => fetch(u, { cache: 'no-store', credentials: 'same-origin' })
          .then((res) => (res && res.ok ? cache.put(u, res) : null))
          .catch(() => null))
      ))
      .catch(() => {})
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

  if (req.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html') {
    event.respondWith(
      caches.match('/').catch(() => null).then((cached) => {
        const timeoutMs = cached ? 900 : 3000;
        return fetchWithTimeout('/', timeoutMs)
          .then((res) => {
            if (res && res.ok) {
              const copy = res.clone();
              caches.open(CACHE_NAME).then((c) => c.put('/', copy)).catch(() => {});
              return res;
            }
            return cached || res;
          })
          .catch(() => cached || new Response(
            SW_OFFLINE_HTML,
            { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } }
          ));
      })
    );
    return;
  }

  if (url.pathname === '/manifest.json' || url.pathname === '/icon.svg') {
    event.respondWith(
      fetchWithTimeout(url.pathname, 900)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(url.pathname, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => caches.match(url.pathname).catch(() => null))
    );
  }
});
"""

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#070a10">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<title>无水印提取 · 抖音 / 小红书 / X</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #070a10;
    --card: rgba(19, 24, 34, 0.9);
    --card-solid: #141923;
    --line: rgba(255, 255, 255, 0.085);
    --line-hi: rgba(96, 165, 250, 0.52);
    --text: #eef2f8;
    --muted: #8b96aa;
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
    padding: calc(14px + env(safe-area-inset-top)) 13px calc(76px + env(safe-area-inset-bottom));
    background:
      radial-gradient(620px 300px at 12% -8%, rgba(59, 130, 246, .20), transparent 64%),
      radial-gradient(520px 260px at 92% 4%, rgba(255, 36, 66, .13), transparent 64%),
      radial-gradient(460px 240px at 52% 105%, rgba(6, 182, 212, .08), transparent 65%),
      var(--bg);
    color: var(--text);
    font: 15px/1.55 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    min-height: 100vh;
  }
  .wrap { max-width: 680px; margin: 0 auto; }
  .hd { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 14px; padding: 2px 2px 0; }
  .brand { display: flex; align-items: center; gap: 11px; min-width: 0; }
  .logo {
    width: 40px; height: 40px; border-radius: 13px; flex-shrink: 0;
    background: linear-gradient(135deg, #2563eb, #06b6d4);
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 8px 20px rgba(37, 99, 235, .34), inset 0 1px 0 rgba(255, 255, 255, .28);
  }
  .logo svg { width: 22px; height: 22px; }
  h1 { font-size: 19px; font-weight: 700; margin: 0; letter-spacing: .2px; line-height: 1.25; }
  .sub { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; margin-top: 2px; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); display: inline-block; flex-shrink: 0; transition: background .2s, box-shadow .2s; }
  .dot.on { background: var(--ok); box-shadow: 0 0 8px rgba(63, 185, 80, .75); }
  .dot.off { background: var(--err); box-shadow: 0 0 8px rgba(248, 81, 73, .55); }
  .hd-acts { display: flex; gap: 6px; align-items: center; flex-shrink: 0; }
  .pill {
    border: 1px solid var(--line); background: rgba(255, 255, 255, .04); color: var(--text);
    border-radius: 999px; padding: 7px 12px; font-size: 12.5px; font-weight: 500;
    cursor: pointer; white-space: nowrap; min-height: 34px; transition: background .15s, border-color .15s;
  }
  .pill:active { opacity: .82; background: rgba(255, 255, 255, .08); }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 18px;
    padding: 14px; margin-bottom: 13px; backdrop-filter: blur(14px);
    box-shadow: 0 10px 30px rgba(0, 0, 0, .28), inset 0 1px 0 rgba(255, 255, 255, .04);
  }
  .card.anim { animation: cardIn .22s cubic-bezier(.22, .68, 0, 1); }
  @keyframes cardIn { from { opacity: 0; transform: translateY(7px); } to { opacity: 1; transform: translateY(0); } }
  .plats { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
  .pchip {
    font-size: 11.5px; font-weight: 600; padding: 3px 9px; border-radius: 999px;
    background: rgba(255, 255, 255, .05); border: 1px solid rgba(255, 255, 255, .08);
    color: var(--muted); transition: all .18s ease;
  }
  .pchip.act-dy { background: rgba(37, 244, 238, .15); border-color: rgba(37, 244, 238, .42); color: #7af5f0; }
  .pchip.act-xhs { background: rgba(255, 36, 66, .16); border-color: rgba(255, 107, 129, .45); color: #ff7a8d; }
  .pchip.act-x { background: rgba(255, 255, 255, .14); border-color: rgba(255, 255, 255, .36); color: #f8fafc; }
  .pnote { font-size: 11.5px; color: var(--muted); margin-left: auto; opacity: .85; }
  textarea {
    width: 100%; min-height: 92px; border: 1px solid var(--line); background: rgba(8, 11, 17, .88);
    color: var(--text); border-radius: 13px; padding: 11px 12px; font: 14px/1.5 inherit;
    resize: vertical; outline: none; transition: border-color .18s, box-shadow .18s;
  }
  textarea:focus { border-color: var(--line-hi); box-shadow: 0 0 0 3px rgba(59, 130, 246, .16); }
  .detect {
    display: none; align-items: center; justify-content: space-between; gap: 8px;
    margin-top: 9px; padding: 8px 11px; border-radius: 10px; font-size: 12.5px;
    background: var(--ok-soft); border: 1px solid rgba(63, 185, 80, .30); color: #9ae6b4;
  }
  .detect.warn { background: rgba(227, 179, 65, .11); border-color: rgba(227, 179, 65, .30); color: #f6d57a; }
  .detect.show { display: flex; }
  .detect-txt { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .detect-btn {
    border: 1px solid rgba(255,255,255,.18); background: rgba(255,255,255,.09); color: #fff;
    border-radius: 7px; padding: 4px 8px; font-size: 11.5px; min-height: 26px; cursor: pointer; flex-shrink: 0;
  }
  .row { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
  .row-3 { display: grid; grid-template-columns: auto 1fr 1.25fr; gap: 8px; margin-top: 10px; }
  .row-3 button { min-width: 0; padding: 0 10px; }
  button, a.btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    min-height: 44px; border-radius: 12px; font: 600 14px/1 inherit; padding: 0 13px;
    border: 1px solid var(--line); background: #202736; color: var(--text);
    text-decoration: none; cursor: pointer; white-space: nowrap;
    transition: transform .08s, opacity .15s, background .15s, border-color .15s;
  }
  button svg, a.btn svg { width: 15px; height: 15px; flex-shrink: 0; }
  button:active, a.btn:active { transform: scale(.98); }
  button.pri, a.btn.pri {
    background: linear-gradient(135deg, var(--accent), var(--accent-hi));
    border-color: transparent; color: #fff; flex: 1.25;
    box-shadow: 0 5px 16px rgba(37, 99, 235, .30), inset 0 1px 0 rgba(255, 255, 255, .2);
  }
  button.sec { flex: 1; background: rgba(255, 255, 255, .06); border-color: rgba(255, 255, 255, .11); }
  button.ghost { background: rgba(255, 255, 255, .03); color: var(--muted); padding: 0 12px; }
  button:disabled { opacity: .55; cursor: not-allowed; transform: none; }
  .prog { display: none; height: 3px; border-radius: 3px; background: rgba(255,255,255,.06); overflow: hidden; margin-top: 11px; }
  .prog.show { display: block; }
  .prog > i {
    display: block; height: 100%; width: 42%; border-radius: 3px;
    background: linear-gradient(90deg, transparent, #60a5fa, #38bdf8, transparent);
    animation: slide 1.1s ease-in-out infinite;
  }
  @keyframes slide { 0% { transform: translateX(-110%); } 100% { transform: translateX(260%); } }
  .hint { margin-top: 9px; font-size: 12px; color: var(--muted); min-height: 17px; line-height: 1.45; }
  .err { display: none; margin-top: 10px; padding: 11px 12px; border-radius: 11px; background: rgba(248, 81, 73, .12); border: 1px solid rgba(248, 81, 73, .35); color: #ffb3ae; font-size: 13px; }
  .err.show { display: block; }
  .err button { min-height: 32px; padding: 0 10px; font-size: 12px; margin-top: 8px; }
  .meta { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; margin-bottom: 11px; }
  .meta-main { flex: 1; min-width: 0; }
  .copy-btn {
    min-height: 30px; padding: 0 10px; border-radius: 9px; font-size: 12px; font-weight: 500;
    background: rgba(255, 255, 255, .06); border: 1px solid rgba(255, 255, 255, .11); color: var(--muted); flex-shrink: 0;
  }
  .copy-btn:active { color: #fff; }
  .badge { display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px; background: var(--accent-soft); color: #8ac4ff; margin-right: 7px; vertical-align: 1px; letter-spacing: .2px; }
  .badge.xhs { background: rgba(255, 36, 66, .20); color: #ff7086; }
  .badge.dy { background: linear-gradient(135deg, rgba(37, 244, 238, .18), rgba(254, 44, 85, .18)); color: #75f4ef; }
  .badge.x { background: rgba(238, 242, 248, .14); color: #f1f5f9; border: 1px solid rgba(255,255,255,.18); }
  .title { font-weight: 600; font-size: 15.5px; line-height: 1.45; word-break: break-word; }
  .author { font-size: 12.5px; color: var(--muted); margin-top: 4px; }
  .acts { display: flex; gap: 8px; margin: 10px 0 14px; flex-wrap: wrap; }
  .sel-bar {
    display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap;
    margin: 4px 0 10px; padding: 8px 11px; border-radius: 12px;
    background: rgba(255, 255, 255, .035); border: 1px solid var(--line);
  }
  .sel-info { font-size: 12.5px; color: var(--muted); }
  .sel-info b { color: #93c5fd; font-weight: 600; }
  .sel-acts { display: flex; gap: 6px; align-items: center; }
  .sel-btn {
    min-height: 30px; padding: 0 10px; border-radius: 8px; font-size: 12px; font-weight: 500;
    background: rgba(255, 255, 255, .06); border: 1px solid rgba(255, 255, 255, .1); color: var(--text);
  }
  .vbox { background: #000; border-radius: 14px; overflow: hidden; margin-bottom: 10px; border: 1px solid var(--line); box-shadow: 0 6px 20px rgba(0, 0, 0, .36); }
  .vbox video { width: 100%; max-height: 62vh; display: block; background: #000; }
  .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .grid.single { grid-template-columns: minmax(0, 1fr); }
  @media (min-width: 560px) { .grid:not(.single) { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
  .item { background: #0d1119; border: 1.5px solid var(--line); border-radius: 14px; overflow: hidden; display: flex; flex-direction: column; transition: border-color .16s, box-shadow .16s; }
  .item.sel { border-color: var(--accent-hi); box-shadow: 0 0 0 1px rgba(59, 130, 246, .35), 0 6px 18px rgba(37, 99, 235, .18); }
  .thumb {
    position: relative; aspect-ratio: 4/5; overflow: hidden; cursor: zoom-in;
    background: linear-gradient(110deg, #121722 25%, #1b2232 45%, #121722 65%);
    background-size: 200% 100%; animation: skel 1.3s linear infinite;
  }
  .thumb.done { animation: none; background: #0a0d14; }
  .grid.single .thumb { aspect-ratio: auto; min-height: 180px; max-height: 52vh; }
  .grid.single .thumb img { max-height: 52vh; object-fit: contain; }
  @keyframes skel { 0% { background-position: 180% 0; } 100% { background-position: -20% 0; } }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; opacity: 0; transition: opacity .22s ease; }
  .thumb img.loaded { opacity: 1; }
  .thumb.broken img { opacity: 0; }
  .thumb-err {
    position: absolute; inset: 0; display: none; flex-direction: column; align-items: center; justify-content: center;
    gap: 4px; font-size: 12px; color: var(--muted); background: rgba(13, 17, 25, .92); cursor: pointer;
  }
  .thumb.broken .thumb-err { display: flex; }
  .tags-tl { position: absolute; left: 7px; top: 7px; display: flex; gap: 5px; z-index: 2; pointer-events: none; }
  .tag { font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 7px; background: rgba(0,0,0,.62); color: #fff; backdrop-filter: blur(6px); }
  .tag.live { background: rgba(37, 99, 235, .82); }
  .pick-btn {
    position: absolute; right: 7px; top: 7px; width: 28px; height: 28px; min-height: 28px; padding: 0;
    border-radius: 999px; border: 1.5px solid rgba(255, 255, 255, .74);
    background: rgba(8, 12, 20, .54); backdrop-filter: blur(5px);
    color: rgba(255, 255, 255, .42); font-size: 13px; font-weight: 700; z-index: 3;
    display: flex; align-items: center; justify-content: center;
  }
  .pick-btn::after { content: ""; position: absolute; inset: -8px; }
  .pick-btn.on {
    background: linear-gradient(135deg, var(--accent), var(--accent-hi));
    border-color: #93c5fd; color: #fff; box-shadow: 0 2px 10px rgba(37, 99, 235, .48);
  }
  .zoom-ic {
    position: absolute; right: 7px; bottom: 7px; width: 24px; height: 24px; border-radius: 7px;
    background: rgba(0, 0, 0, .52); color: rgba(255, 255, 255, .86); backdrop-filter: blur(4px);
    display: flex; align-items: center; justify-content: center; pointer-events: none;
  }
  .zoom-ic svg { width: 13px; height: 13px; }
  .item-ft { padding: 8px; display: flex; flex-direction: column; gap: 6px; }
  .item-ft a.btn { min-height: 37px; font-size: 12.5px; width: 100%; border-radius: 9px; padding: 0 10px; }
  .hist-hd { display: flex; align-items: center; justify-content: space-between; margin-bottom: 9px; }
  .hist-hd b { font-size: 14px; }
  .hist-clr { background: transparent; border: none; color: var(--muted); font-size: 12px; min-height: 28px; padding: 0 6px; cursor: pointer; }
  .hist-list { display: flex; flex-direction: column; gap: 7px; }
  .hist-row {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 9px 11px; border-radius: 12px; border: 1px solid var(--line); background: rgba(10, 13, 20, .75);
    cursor: pointer; text-align: left; width: 100%; min-height: 44px;
  }
  .hist-row:active { background: rgba(59, 130, 246, .12); border-color: rgba(96, 165, 250, .35); }
  .hist-title { font-size: 13px; font-weight: 500; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
  .hist-time { font-size: 11.5px; color: var(--muted); flex-shrink: 0; }
  .modal { position: fixed; inset: 0; background: rgba(0, 0, 0, .72); backdrop-filter: blur(5px); display: none; align-items: flex-end; justify-content: center; z-index: 99; padding: 12px; }
  .modal.open { display: flex; }
  @media (min-width: 560px) { .modal { align-items: center; } }
  .sheet {
    width: 100%; max-width: 520px; background: var(--card-solid); border: 1px solid rgba(255,255,255,.11);
    border-radius: 20px; padding: 10px 16px 16px; max-height: 86vh; overflow: auto;
    box-shadow: 0 18px 48px rgba(0, 0, 0, .55); animation: sheetUp .2s ease;
  }
  @keyframes sheetUp { from { opacity: 0; transform: translateY(18px); } to { opacity: 1; transform: translateY(0); } }
  .sheet-handle { width: 36px; height: 4px; border-radius: 99px; background: rgba(255, 255, 255, .18); margin: 0 auto 12px; }
  .sheet h2 { margin: 0; font-size: 16.5px; }
  .field { margin-bottom: 12px; }
  .field label { display: flex; justify-content: space-between; font-size: 13px; color: var(--muted); margin-bottom: 5px; }
  .field label b { color: var(--ok); font-weight: 600; }
  .field label b.off { color: var(--muted); font-weight: 500; }
  .field textarea { min-height: 66px; font-size: 12.5px; }
  .chk {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    font-size: 13.5px; margin: 10px 0 14px; padding: 10px 12px;
    border-radius: 12px; background: rgba(255, 255, 255, .03); border: 1px solid var(--line); cursor: pointer;
  }
  .sw { position: relative; width: 42px; height: 24px; flex-shrink: 0; }
  .sw input { opacity: 0; width: 0; height: 0; position: absolute; }
  .sw-track {
    position: absolute; inset: 0; border-radius: 99px; background: #2c3547; transition: background .18s;
  }
  .sw-track::after {
    content: ""; position: absolute; left: 3px; top: 3px; width: 18px; height: 18px;
    border-radius: 50%; background: #fff; transition: transform .18s; box-shadow: 0 1px 4px rgba(0,0,0,.35);
  }
  .sw input:checked + .sw-track { background: var(--accent); }
  .sw input:checked + .sw-track::after { transform: translateX(18px); }
  .lightbox {
    position: fixed; inset: 0; background: rgba(4, 6, 10, .95); backdrop-filter: blur(8px);
    display: none; flex-direction: column; align-items: center; justify-content: space-between;
    z-index: 120; padding: calc(12px + env(safe-area-inset-top)) 14px calc(14px + env(safe-area-inset-bottom));
  }
  .lightbox.open { display: flex; }
  .lb-top { width: 100%; max-width: 720px; display: flex; justify-content: space-between; align-items: center; gap: 10px; color: #fff; font-size: 14px; }
  .lb-meta { display: flex; align-items: center; gap: 8px; }
  .lb-qual { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: rgba(255,255,255,.10); color: #cbd5e1; }
  .lb-qual.hd { background: rgba(63, 185, 80, .18); color: #86efac; }
  .lb-stage { flex: 1; width: 100%; display: flex; align-items: center; justify-content: center; position: relative; min-height: 0; margin: 8px 0; }
  .lb-stage img { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 10px; }
  .lb-nav {
    position: absolute; top: 50%; transform: translateY(-50%);
    width: 40px; height: 48px; border-radius: 12px; border: 1px solid rgba(255,255,255,.18);
    background: rgba(15, 20, 30, .62); backdrop-filter: blur(6px); color: #fff; font-size: 22px; min-height: 40px; padding: 0;
  }
  .lb-nav.prev { left: 4px; }
  .lb-nav.next { right: 4px; }
  .lb-ft { width: 100%; max-width: 440px; display: flex; gap: 8px; }
  .lb-pick {
    min-height: 44px; padding: 0 14px; border-radius: 12px; font-size: 13.5px; font-weight: 600;
    background: rgba(255, 255, 255, .09); border: 1px solid rgba(255, 255, 255, .2); color: #e2e8f0; flex-shrink: 0;
  }
  .lb-pick.on { background: rgba(37, 99, 235, .32); border-color: #60a5fa; color: #dbeafe; }
  .toast {
    position: fixed; left: 50%; bottom: calc(22px + env(safe-area-inset-bottom)); transform: translate(-50%, 16px);
    background: rgba(20, 26, 38, .96); border: 1px solid rgba(255,255,255,.15);
    color: #fff; padding: 10px 16px; border-radius: 999px; font-size: 13px;
    opacity: 0; pointer-events: none; transition: opacity .18s, transform .18s; z-index: 150;
    box-shadow: 0 10px 26px rgba(0,0,0,.48); white-space: nowrap; max-width: 90vw; overflow: hidden; text-overflow: ellipsis;
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
      <button class="pill" id="stopBtn" type="button" style="display:none">休眠</button>
      <button class="pill" id="cfgBtn" type="button">设置</button>
    </div>
  </div>

  <div class="card" id="wakeBox" style="display:none;border-color:rgba(248,81,73,.45)">
    <div style="font-weight:600;margin-bottom:4px">本地解析网关尚未连接</div>
    <div class="sub" style="margin-bottom:10px;display:block">如果手机刚重启、尚未启动 Termux 或后台被系统冻结，点下方按钮唤起 Termux 后切回本页即可自动恢复（若切换后仍常断开，请在系统设置把 Termux 设为「允许后台运行 / 省电无限制」）。</div>
    <div class="row" style="margin-top:0">
      <a class="btn pri" href="intent://#Intent;package=com.termux;end">⚡ 一键唤醒 Termux 服务</a>
      <button type="button" id="retryConnBtn">重试连接</button>
    </div>
  </div>

  <div class="card">
    <div class="plats">
      <span class="pchip" id="pc-dy">抖音</span>
      <span class="pchip" id="pc-xhs">小红书</span>
      <span class="pchip" id="pc-x">X (Twitter)</span>
      <span class="pnote">直链 · 原图 · 实况</span>
    </div>
    <textarea id="inp" placeholder="粘贴抖音 / 小红书 / X 分享文案或链接（自动过滤中文口令，粘贴即解析）"></textarea>
    <div class="detect" id="detectBar">
      <span class="detect-txt" id="detectTxt"></span>
      <button type="button" class="detect-btn" id="trimUrlBtn" style="display:none">仅留纯链接</button>
    </div>
    <div class="row-3">
      <button type="button" class="ghost" id="clrBtn">清空</button>
      <button type="button" class="sec" id="pasteBtn">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        粘贴提取
      </button>
      <button type="button" class="pri" id="goBtn">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
        <span id="goTxt">提取无水印</span>
      </button>
    </div>
    <div class="prog" id="prog"><i></i></div>
    <div class="hint" id="hint">粘贴整段分享即可自动识别：列表优先加载轻量缩略图，保存时下载无损原画。</div>
    <div class="err" id="err"></div>
  </div>

  <div class="card" id="resCard" style="display:none"></div>

  <div class="card" id="histCard" style="display:none">
    <div class="hist-hd">
      <b>最近提取</b>
      <button type="button" class="hist-clr" id="clrHistBtn">清空</button>
    </div>
    <div class="hist-list" id="histList"></div>
  </div>
</div>

<div class="modal" id="cfgModal">
  <div class="sheet">
    <div class="sheet-handle"></div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <h2>Cookie 与偏好设置</h2>
      <button type="button" id="cfgClose" style="min-height:34px;padding:0 12px;font-size:12.5px">关闭</button>
    </div>
    <div class="sub" style="margin-bottom:12px;display:block">
      仅在遇到平台滑块或登录风控时填写；留空表示保持当前配置。
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
      <span>同时提取图集里的实况照片短视频</span>
      <span class="sw"><input type="checkbox" id="liveChk" checked><span class="sw-track"></span></span>
    </label>
    <div class="row">
      <button type="button" id="clrCkBtn" style="flex:1">清空 Cookie</button>
      <button type="button" class="pri" id="saveCfgBtn">保存设置</button>
    </div>
  </div>
</div>

<div class="lightbox" id="lb">
  <div class="lb-top">
    <div class="lb-meta"><span id="lbCount">1 / 1</span><span class="lb-qual" id="lbQuality">预览图</span></div>
    <button type="button" id="lbClose" style="min-height:34px;padding:0 12px;font-size:13px">关闭</button>
  </div>
  <div class="lb-stage" id="lbStage">
    <button type="button" class="lb-nav prev" id="lbPrev">‹</button>
    <img id="lbImg" alt="">
    <button type="button" class="lb-nav next" id="lbNext">›</button>
  </div>
  <div class="lb-ft">
    <button type="button" id="lbPickBtn" class="lb-pick" style="display:none">选中此图</button>
    <a class="btn pri" id="lbDl" href="#">下载本张无损原图</a>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
const HIST_KEY = 'mg_history_v1';
let busy = false;
let serverOnline = false;
let connEpoch = 0;
let lastOkAt = 0;
let reconnectTimer = null;
let currentJob = null;
let lbImages = [];
let lbIdx = 0;
let lbIsSelected = null;
let lbTogglePick = null;
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

function stopReconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

function markOnline() {
  lastOkAt = performance.now();
  stopReconnect();
  showWakeBanner(false);
  setOnline(true, '按需服务就绪 · 关页自动停止');
}

function scheduleReconnect(delayMs = 900) {
  if (reconnectTimer || serverOnline || document.visibilityState !== 'visible') return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (!serverOnline && document.visibilityState === 'visible') {
      refreshCfgStatus(true);
    }
  }, delayMs);
}

function showErr(msg) {
  const el = $('err');
  if (!msg) { el.classList.remove('show'); el.innerHTML = ''; return; }
  const needCk = /cookie|风控|验证|登录/i.test(msg);
  el.innerHTML = esc(msg) + (needCk ? '<div><button type="button" onclick="openCfg()">去配置 Cookie</button></div>' : '');
  el.classList.add('show');
}

function setHint(msg) { $('hint').textContent = msg || ''; }

function platName(p) {
  return p === 'douyin' ? '抖音' : (p === 'xiaohongshu' ? '小红书' : (p === 'x' ? 'X' : p));
}

function platBadge(p) {
  return p === 'xiaohongshu' ? 'xhs' : (p === 'douyin' ? 'dy' : (p === 'x' ? 'x' : ''));
}

function clientExtractUrl(text) {
  const m = String(text || '').match(/https?:\/\/[A-Za-z0-9\-._~:\/?#[\]@!$&'()*+,;=%]+/g);
  if (!m) return null;
  const cleaned = m.map(u => u.replace(/[.,;:!?)"']+$/, '')).filter(Boolean);
  const prio = ['douyin.com', 'iesdouyin.com', 'xhslink.com', 'xhslink.cn', 'xiaohongshu.com', 'x.com', 'twitter.com', 'fxtwitter.com', 'vxtwitter.com', 'fixupx.com', 't.co'];
  for (const u of cleaned) {
    try {
      const h = new URL(u).hostname.toLowerCase();
      if (prio.some(d => h === d || h.endsWith('.' + d))) return { url: u, host: h };
    } catch (e) {}
  }
  if (!cleaned.length) return null;
  try { return { url: cleaned[0], host: new URL(cleaned[0]).hostname.toLowerCase() }; } catch (e) { return { url: cleaned[0], host: '' }; }
}

function setPlatChip(code) {
  ['dy', 'xhs', 'x'].forEach(k => {
    const el = $('pc-' + k);
    if (!el) return;
    el.className = 'pchip' + (code === k ? ' act-' + k : '');
  });
}

function updateDetectBar() {
  const raw = $('inp').value.trim();
  const bar = $('detectBar');
  const txt = $('detectTxt');
  const trimBtn = $('trimUrlBtn');
  if (!raw) {
    bar.className = 'detect';
    trimBtn.style.display = 'none';
    setPlatChip('');
    return;
  }
  const found = clientExtractUrl(raw);
  if (!found) {
    bar.className = 'detect warn show';
    txt.textContent = '未在内容里发现有效 http/https 链接，请确认已复制完整分享内容';
    trimBtn.style.display = 'none';
    setPlatChip('');
    return;
  }
  let plat = '链接';
  let chipCode = '';
  if (found.host.includes('douyin')) { plat = '抖音'; chipCode = 'dy'; }
  else if (found.host.includes('xhs') || found.host.includes('xiaohongshu')) { plat = '小红书'; chipCode = 'xhs'; }
  else if (found.host === 'x.com' || found.host.endsWith('.x.com') || found.host.includes('twitter') || found.host.includes('fixupx') || found.host === 't.co') { plat = 'X'; chipCode = 'x'; }
  setPlatChip(chipCode);
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

async function fetchJsonWithRetry(url, options, timeoutMs = 22000, retries = 1, retryOnAbort = false) {
  let lastErr = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(url, Object.assign({ cache: 'no-store' }, options || {}, { signal: ctrl.signal }));
      const data = await r.json();
      return { res: r, data };
    } catch (e) {
      lastErr = e;
      if (e && e.name === 'AbortError') {
        lastErr = new Error('请求超时（超过 ' + Math.round(timeoutMs / 1000) + ' 秒），请检查网络或 Cookie');
        if (!retryOnAbort || attempt >= retries) {
          throw lastErr;
        }
      }
      if (attempt < retries) {
        const waitMs = retryOnAbort ? (attempt === 0 ? 180 : 420) : 250;
        await new Promise(resolve => setTimeout(resolve, waitMs));
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
    const plat = platName(d.platform);
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
      const cls = platBadge(it.platform);
      return '<button type="button" class="hist-row" data-idx="' + i + '">' +
        '<span class="badge ' + cls + '">' + esc(it.platName || platName(it.platform)) + '</span>' +
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
  if (!val) { showErr('请先粘贴抖音、小红书或 X 分享链接'); return; }
  if (busy) return;
  busy = true;
  showErr('');
  $('goBtn').disabled = true;
  if ($('goTxt')) $('goTxt').textContent = '解析中…';
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
        await fetchJsonWithRetry('/api/heartbeat', { method: 'POST' }, 1800, 2, true);
        markOnline();
      } catch (hbErr) {
        showWakeBanner(true);
        setOnline(false, '服务未连接');
        scheduleReconnect();
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
    markOnline();
    const found = clientExtractUrl(val);
    saveHistory(d, (found && found.url) || val);
    renderResult(d);
    requestAnimationFrame(() => {
      const card = $('resCard');
      if (card && card.getBoundingClientRect().top > window.innerHeight * 0.62) {
        card.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
    const sec = ((performance.now() - t0) / 1000).toFixed(2);
    setHint('解析完成（耗时 ' + sec + 's），预览已开启加速，保存将直接下载无损原图。');
    showToast('解析成功 (' + sec + 's)');
  } catch (e) {
    const msg = e && e.message ? e.message : '连接失败';
    let alive = true;
    try {
      await fetchJsonWithRetry('/api/heartbeat', { method: 'POST' }, 1200, 1, true);
      markOnline();
    } catch (_) {
      alive = false;
    }
    if (!alive || !/超时/.test(msg)) {
      showWakeBanner(true);
      setOnline(false, '服务未连接');
      scheduleReconnect();
    }
    showErr('提取失败: ' + msg);
    setHint('');
  } finally {
    busy = false;
    clearInterval(timerId);
    $('prog').classList.remove('show');
    $('goBtn').disabled = false;
    if ($('goTxt')) $('goTxt').textContent = '提取无水印';
  }
}

function showLightbox(idx) {
  if (!lbImages.length) return;
  lbIdx = (idx + lbImages.length) % lbImages.length;
  const im = lbImages[lbIdx];
  $('lbCount').textContent = (lbIdx + 1) + ' / ' + lbImages.length;
  $('lbDl').href = im.download_url;
  const thumb = im.thumb_url || im.preview_url;
  const qEl = $('lbQuality');
  const pkEl = $('lbPickBtn');
  if (pkEl) {
    if (lbImages.length > 1 && typeof lbIsSelected === 'function') {
      const on = !!lbIsSelected(im.id);
      pkEl.style.display = '';
      pkEl.textContent = on ? '已选中 ✓' : '选中此图';
      pkEl.classList.toggle('on', on);
    } else {
      pkEl.style.display = 'none';
    }
  }
  $('lbImg').src = thumb;
  if (im.preview_url && im.preview_url !== thumb) {
    if (qEl) { qEl.textContent = '高清预览 · 加载原图中'; qEl.className = 'lb-qual'; }
    const hi = new Image();
    const targetIdx = lbIdx;
    hi.onload = () => {
      if (lbIdx === targetIdx && $('lb').classList.contains('open')) {
        $('lbImg').src = im.preview_url;
        if (qEl) { qEl.textContent = '无损原图 ✓'; qEl.className = 'lb-qual hd'; }
      }
    };
    hi.src = im.preview_url;
  } else if (qEl) {
    qEl.textContent = '无损原图 ✓';
    qEl.className = 'lb-qual hd';
  }
  $('lbPrev').style.display = lbImages.length > 1 ? '' : 'none';
  $('lbNext').style.display = lbImages.length > 1 ? '' : 'none';
  $('lb').classList.add('open');
}

function renderResult(d) {
  const card = $('resCard');
  const plat = platName(d.platform);
  const badgeCls = platBadge(d.platform);
  const videos = d.assets.filter(a => a.kind === 'video');
  const images = d.assets.filter(a => a.kind === 'image');
  const lives = d.assets.filter(a => a.kind === 'live_video');
  lbImages = images;
  const selectedImgIds = new Set();
  const liveMap = new Map();
  lives.forEach(l => liveMap.set(l.pair_index, l));

  function getSelectedAssetIds() {
    const ids = [];
    for (const im of images) {
      if (!selectedImgIds.has(im.id)) continue;
      ids.push(im.id);
      const lv = liveMap.get(im.index);
      if (lv) ids.push(lv.id);
    }
    return ids;
  }

  const counts = [];
  if (videos.length) counts.push(videos.length + ' 个高清视频');
  if (images.length) counts.push(images.length + ' 张无水印原图');
  if (lives.length) counts.push(lives.length + ' 个实况视频');

  let html = '<div class="meta"><div class="meta-main">' +
    '<div class="title"><span class="badge ' + badgeCls + '">' + esc(plat) + '</span>' + esc(d.title) + '</div>' +
    '<div class="author">' + (d.author ? esc(d.author) + ' · ' : '') + esc(counts.join(' · ')) + '</div>' +
    '</div>' +
    (d.title ? '<button type="button" class="copy-btn" id="copyTitleBtn">复制文案</button>' : '') +
    '</div>';

  if (d.assets.length > 0) {
    const defaultSaveTxt = d.assets.length > 1 ? '一键保存全部到相册' : '保存到相册';
    html += '<div class="acts">' +
      '<button type="button" class="pri" id="saveLocalBtn">' + defaultSaveTxt + '</button>' +
      (images.length > 1 ? '<button type="button" class="sec" id="saveAllLocalBtn" style="display:none">保存全部</button>' : '') +
      (d.assets.length > 1 ? '<a class="btn" id="zipBtn" href="' + esc(d.zip_url) + '">打包全部 (.zip)</a>' : '') +
      '</div>';
  }

  for (const v of videos) {
    const posterAttr = (v.thumb_url && v.thumb_url !== v.preview_url) ? ' poster="' + esc(v.thumb_url) + '"' : '';
    html += '<div class="vbox"><video controls playsinline preload="metadata"' + posterAttr + ' src="' + esc(v.preview_url) + '"></video></div>' +
      '<div class="acts"><a class="btn pri" href="' + esc(v.download_url) + '">下载最高画质视频</a></div>';
  }

  if (images.length || lives.length) {
    if (images.length > 1) {
      html += '<div class="sel-bar" id="selBar">' +
        '<span class="sel-info" id="selInfo">已选 <b>0</b> / ' + images.length + ' 张（默认保存全部）</span>' +
        '<div class="sel-acts">' +
        '<button type="button" class="sel-btn" id="selAllBtn">全选</button>' +
        '<button type="button" class="sel-btn" id="selInvBtn">反选</button>' +
        '<button type="button" class="sel-btn" id="selClrBtn" style="display:none">清空</button>' +
        '</div></div>';
    }
    html += '<div class="grid' + (images.length === 1 ? ' single' : '') + '">';
    images.forEach((im, idx) => {
      const lv = liveMap.get(im.index);
      const thumbSrc = im.thumb_url || im.preview_url;
      const loadMode = idx < 6 ? 'eager' : 'lazy';
      html += '<div class="item" data-id="' + esc(im.id) + '">' +
        '<div class="thumb" data-idx="' + idx + '">' +
        '<img loading="' + loadMode + '" decoding="async" src="' + esc(thumbSrc) + '" onload="this.classList.add(\'loaded\');this.parentElement.classList.add(\'done\')" onerror="this.classList.add(\'loaded\');this.parentElement.classList.add(\'done\',\'broken\')" alt="">' +
        '<div class="thumb-err"><span>预览加载失败</span><span style="font-size:11px;opacity:.75">点击重试</span></div>' +
        '<div class="tags-tl"><span class="tag">#' + (idx + 1) + '</span>' +
        (lv ? '<span class="tag live">实况</span>' : '') + '</div>' +
        (images.length > 1 ? '<button type="button" class="pick-btn" data-id="' + esc(im.id) + '" aria-label="选择第 ' + (idx + 1) + ' 张">✓</button>' : '') +
        '<span class="zoom-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.35-4.35"/></svg></span></div>' +
        '<div class="item-ft">' +
        '<a class="btn pri" href="' + esc(im.download_url) + '">下载无损原图</a>' +
        (lv ? '<a class="btn" href="' + esc(lv.download_url) + '">下载实况视频</a>' : '') +
        '</div></div>';
    });
    if (!images.length && lives.length) {
      lives.forEach((lv, idx) => {
        html += '<div class="item"><div class="item-ft"><a class="btn pri" href="' + esc(lv.download_url) + '">下载实况视频 #' + (idx + 1) + '</a></div></div>';
      });
    }
    html += '</div>';
  }

  card.innerHTML = html;
  card.style.display = 'block';
  card.classList.remove('anim');
  void card.offsetWidth;
  card.classList.add('anim');

  let saving = false;
  function defaultMainSaveText() {
    if (images.length > 1 && selectedImgIds.size > 0 && selectedImgIds.size < images.length) {
      return '保存已选到相册 (' + selectedImgIds.size + ')';
    }
    return d.assets.length > 1 ? '一键保存全部到相册' : '保存到相册';
  }

  function syncSelectionUI() {
    const n = selectedImgIds.size;
    const partial = images.length > 1 && n > 0 && n < images.length;
    card.querySelectorAll('.item[data-id]').forEach(it => {
      it.classList.toggle('sel', selectedImgIds.has(it.dataset.id));
    });
    card.querySelectorAll('.pick-btn').forEach(btn => {
      btn.classList.toggle('on', selectedImgIds.has(btn.dataset.id));
    });
    const infoEl = $('selInfo');
    if (infoEl) {
      if (n === 0) infoEl.innerHTML = '已选 <b>0</b> / ' + images.length + ' 张（默认保存全部）';
      else if (n === images.length) infoEl.innerHTML = '已选 <b>全部 ' + images.length + '</b> 张';
      else infoEl.innerHTML = '已选 <b>' + n + '</b> / ' + images.length + ' 张';
    }
    const clrBtn = $('selClrBtn');
    if (clrBtn) clrBtn.style.display = n > 0 ? '' : 'none';
    const saveBtnEl = $('saveLocalBtn');
    const saveAllEl = $('saveAllLocalBtn');
    if (saveBtnEl && !saving) {
      saveBtnEl.disabled = false;
      saveBtnEl.textContent = defaultMainSaveText();
    }
    if (saveAllEl && !saving) {
      saveAllEl.disabled = false;
      saveAllEl.style.display = partial ? '' : 'none';
      saveAllEl.textContent = '保存全部';
    }
    const zipEl = $('zipBtn');
    if (zipEl) {
      if (partial) {
        zipEl.href = d.zip_url + '?ids=' + encodeURIComponent(getSelectedAssetIds().join(','));
        zipEl.textContent = '打包已选 (' + n + ')';
      } else {
        zipEl.href = d.zip_url;
        zipEl.textContent = '打包全部 (.zip)';
      }
    }
  }

  lbIsSelected = id => selectedImgIds.has(id);
  lbTogglePick = id => {
    if (selectedImgIds.has(id)) selectedImgIds.delete(id);
    else selectedImgIds.add(id);
    syncSelectionUI();
    return selectedImgIds.has(id);
  };

  card.querySelectorAll('.thumb').forEach(el => {
    el.addEventListener('click', () => {
      if (el.classList.contains('broken')) {
        const img = el.querySelector('img');
        if (img && img.src) {
          const base = img.src.replace(/([?&])_r=\d+/, '').replace(/[?&]$/, '');
          el.classList.remove('broken', 'done');
          img.classList.remove('loaded');
          img.src = base + (base.includes('?') ? '&' : '?') + '_r=' + Date.now();
        }
        return;
      }
      showLightbox(Number(el.dataset.idx || 0));
    });
  });

  card.querySelectorAll('.pick-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const id = btn.dataset.id;
      if (!id) return;
      if (selectedImgIds.has(id)) selectedImgIds.delete(id);
      else selectedImgIds.add(id);
      syncSelectionUI();
    });
  });

  if ($('selAllBtn')) {
    $('selAllBtn').addEventListener('click', () => {
      images.forEach(im => selectedImgIds.add(im.id));
      syncSelectionUI();
    });
  }
  if ($('selInvBtn')) {
    $('selInvBtn').addEventListener('click', () => {
      images.forEach(im => {
        if (selectedImgIds.has(im.id)) selectedImgIds.delete(im.id);
        else selectedImgIds.add(im.id);
      });
      syncSelectionUI();
    });
  }
  if ($('selClrBtn')) {
    $('selClrBtn').addEventListener('click', () => {
      selectedImgIds.clear();
      syncSelectionUI();
    });
  }

  const copyBtn = $('copyTitleBtn');
  if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
      const txt = d.title || '';
      let ok = false;
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(txt);
          ok = true;
        }
      } catch (e) {}
      if (!ok) {
        try {
          const ta = document.createElement('textarea');
          ta.value = txt;
          ta.readOnly = true;
          ta.style.cssText = 'position:fixed;left:-9999px;top:0';
          document.body.appendChild(ta);
          ta.select();
          ok = document.execCommand('copy');
          ta.remove();
        } catch (e) {}
      }
      showToast(ok ? '已复制文案' : '请长按标题手动复制');
    });
  }

  async function runSave(btn, assetIds, donePrefix) {
    if (saving || !btn) return;
    saving = true;
    const saveBtnEl = $('saveLocalBtn');
    const saveAllEl = $('saveAllLocalBtn');
    if (saveBtnEl) saveBtnEl.disabled = true;
    if (saveAllEl) saveAllEl.disabled = true;
    btn.textContent = assetIds ? '正在保存已选原图…' : '正在保存全部到相册…';
    try {
      const reqOpts = { method: 'POST' };
      if (assetIds) {
        reqOpts.headers = { 'Content-Type': 'application/json' };
        reqOpts.body = JSON.stringify({ asset_ids: assetIds });
      }
      const { res: r, data: res } = await fetchJsonWithRetry('/api/save/' + encodeURIComponent(d.job_id), reqOpts, 60000, 1);
      if (!r.ok || !res.ok) {
        showErr(res.error || '保存失败');
        if (saveBtnEl) { saveBtnEl.disabled = false; saveBtnEl.textContent = defaultMainSaveText(); }
        if (saveAllEl) { saveAllEl.disabled = false; saveAllEl.textContent = '保存全部'; }
        return;
      }
      if (saveBtnEl) saveBtnEl.disabled = false;
      if (saveAllEl) saveAllEl.disabled = false;
      btn.textContent = donePrefix + ' ✓ (' + res.saved + '/' + res.total + ')';
      const other = btn === saveBtnEl ? saveAllEl : saveBtnEl;
      if (other) other.textContent = other === saveBtnEl ? defaultMainSaveText() : '保存全部';
      setHint('已直接保存到 ' + res.dir + ' 并刷新相册。');
      showToast('已存进相册 (' + res.saved + '/' + res.total + ')');
    } catch (e) {
      showErr('保存请求失败: ' + e.message);
      if (saveBtnEl) { saveBtnEl.disabled = false; saveBtnEl.textContent = defaultMainSaveText(); }
      if (saveAllEl) { saveAllEl.disabled = false; saveAllEl.textContent = '保存全部'; }
    } finally {
      saving = false;
    }
  }

  const saveBtn = $('saveLocalBtn');
  if (saveBtn) {
    saveBtn.addEventListener('click', () => {
      const partial = images.length > 1 && selectedImgIds.size > 0 && selectedImgIds.size < images.length;
      const ids = partial ? getSelectedAssetIds() : null;
      runSave(saveBtn, ids, partial ? '已存已选' : '已存进相册');
    });
  }
  const saveAllBtn = $('saveAllLocalBtn');
  if (saveAllBtn) {
    saveAllBtn.addEventListener('click', () => {
      runSave(saveAllBtn, null, '已全部保存');
    });
  }
}

document.addEventListener('keydown', e => {
  if (!$('lb').classList.contains('open')) return;
  if (e.key === 'Escape') { $('lb').classList.remove('open'); $('lbImg').src = ''; }
  else if (e.key === 'ArrowLeft') showLightbox(lbIdx - 1);
  else if (e.key === 'ArrowRight') showLightbox(lbIdx + 1);
});

$('lbClose').addEventListener('click', () => { $('lb').classList.remove('open'); $('lbImg').src = ''; });
$('lbPickBtn').addEventListener('click', e => {
  e.stopPropagation();
  if (!lbImages.length || typeof lbTogglePick !== 'function') return;
  const im = lbImages[lbIdx];
  if (!im) return;
  const on = !!lbTogglePick(im.id);
  const pkEl = $('lbPickBtn');
  pkEl.textContent = on ? '已选中 ✓' : '选中此图';
  pkEl.classList.toggle('on', on);
});
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
      setHint('剪贴板是空的，请先在抖音、小红书或 X 复制链接。');
      showToast('剪贴板是空的');
      return;
    }
  } catch (e) {}
  $('inp').value = '';
  updateDetectBar();
  $('inp').scrollIntoView({ behavior: 'smooth', block: 'center' });
  $('inp').focus();
  setHint('当前浏览器限制网页自动读剪贴板：请长按输入框点「粘贴」，粘贴后会自动开始提取。');
});

let manualStopped = false;

async function refreshCfgStatus(fast = false) {
  const ep = ++connEpoch;
  const startOkAt = lastOkAt;
  try {
    const { res: r, data: c } = await fetchJsonWithRetry(
      '/api/config',
      {},
      fast ? 1800 : 3500,
      fast ? 2 : 1,
      true
    );
    if (!r.ok) return false;
    $('xhsSt').textContent = c.xhs_cookie_set ? '已配置' : '未配置';
    $('xhsSt').className = c.xhs_cookie_set ? '' : 'off';
    $('dySt').textContent = c.douyin_cookie_set ? '已配置' : '未配置';
    $('dySt').className = c.douyin_cookie_set ? '' : 'off';
    $('liveChk').checked = !!c.save_live_photo_video;
    $('stopBtn').style.display = c.can_shutdown ? '' : 'none';
    manualStopped = false;
    markOnline();
    return true;
  } catch (e) {
    if (ep === connEpoch && lastOkAt === startOkAt && !manualStopped) {
      showWakeBanner(true);
      setOnline(false, '网关未连接 · 请点击唤醒');
      scheduleReconnect();
    }
    return false;
  }
}

refreshCfgStatus(true);

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').then((reg) => {
    try { reg.update(); } catch (e) {}
  }).catch(() => {});
}

function sendHeartbeat() {
  fetch('/api/heartbeat', { method: 'POST', keepalive: true, cache: 'no-store' })
    .then(r => { if (r.ok) { manualStopped = false; markOnline(); } })
    .catch(() => {});
}

setInterval(() => {
  if (document.visibilityState === 'visible' && !manualStopped) sendHeartbeat();
}, 20000);

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    stopReconnect();
    if (navigator.sendBeacon) navigator.sendBeacon('/api/leave?mode=hidden', '');
  } else {
    manualStopped = false;
    refreshCfgStatus(true);
    sendHeartbeat();
  }
});

window.addEventListener('pageshow', (e) => {
  if (e && e.persisted) {
    manualStopped = false;
    refreshCfgStatus(true);
    sendHeartbeat();
  }
});

window.addEventListener('pagehide', () => {
  stopReconnect();
  if (navigator.sendBeacon) navigator.sendBeacon('/api/leave?mode=close', '');
});

if ($('retryConnBtn')) {
  $('retryConnBtn').addEventListener('click', () => {
    manualStopped = false;
    refreshCfgStatus();
    sendHeartbeat();
  });
}

$('stopBtn').addEventListener('click', async () => {
  manualStopped = true;
  stopReconnect();
  try { await fetch('/api/shutdown', { method: 'POST', cache: 'no-store' }); } catch (e) {}
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


_MAX_THUMB_CACHE_BYTES = 512 * 1024


@dataclass
class StoredAsset:
    asset_id: str
    index: int
    pair_index: int
    stem: str
    asset: MediaAsset
    platform: str = ""
    resolved_url: Optional[str] = None
    resolved_ext: Optional[str] = None
    resolved_thumb_url: Optional[str] = None
    thumb_bytes: Optional[bytes] = None
    thumb_content_type: Optional[str] = None
    thumb_ext: Optional[str] = None


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
        self._preferred_hosts: Dict[Tuple[str, bool], str] = {}
        self._thumb_inflight: Dict[str, threading.Event] = {}

    @staticmethod
    def _prioritize_urls(urls: List[str], exact: Optional[str], pref_host: Optional[str]) -> List[str]:
        ordered: List[str] = []
        if exact:
            ordered.append(exact)
        if pref_host:
            for u in urls:
                if (urlparse(u).hostname or "").lower() == pref_host:
                    ordered.append(u)
        ordered.extend(urls)
        return dedupe_keep_order(ordered)

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
                    platform=result.platform,
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
                    sa.thumb_bytes = None
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
            exact = sa.resolved_url
            pref = self._preferred_hosts.get((sa.platform, False))
        return self._prioritize_urls(list(sa.asset.urls), exact, pref)

    def thumb_urls_for(self, sa: StoredAsset) -> List[str]:
        if not sa.asset.thumb_urls:
            return self.candidate_urls_for(sa)
        if sa.asset.kind in ("video", "live_video"):
            base = list(sa.asset.thumb_urls)
        else:
            base = list(sa.asset.thumb_urls) + list(sa.asset.urls)
        with self._lock:
            exact = sa.resolved_thumb_url
            pref = self._preferred_hosts.get((sa.platform, True))
        return self._prioritize_urls(base, exact, pref)

    def remember_winning(self, sa: StoredAsset, url: str, ext: str, is_thumb: bool = False) -> None:
        host = (urlparse(url).hostname or "").lower()
        with self._lock:
            if host and sa.platform:
                self._preferred_hosts[(sa.platform, bool(is_thumb))] = host
            if is_thumb:
                sa.resolved_thumb_url = url
            else:
                sa.resolved_url = url
                sa.resolved_ext = ext

    def get_cached_thumb(self, sa: StoredAsset) -> Optional[Tuple[bytes, str, str]]:
        with self._lock:
            if sa.thumb_bytes is None:
                return None
            return sa.thumb_bytes, (sa.thumb_content_type or "image/jpeg"), (sa.thumb_ext or ".jpg")

    def cache_thumb(self, sa: StoredAsset, data: bytes, content_type: str, ext: str) -> None:
        if not data or len(data) > _MAX_THUMB_CACHE_BYTES:
            return
        with self._lock:
            if sa.asset_id not in self._assets:
                return
            sa.thumb_bytes = data
            sa.thumb_content_type = content_type
            sa.thumb_ext = ext

    def acquire_thumb_slot(self, asset_id: str) -> Tuple[bool, threading.Event]:
        """Deduplicate concurrent upstream thumbnail fetches for the same asset."""
        with self._lock:
            ev = self._thumb_inflight.get(asset_id)
            if ev is not None:
                return False, ev
            ev = threading.Event()
            self._thumb_inflight[asset_id] = ev
            return True, ev

    def release_thumb_slot(self, asset_id: str, ev: threading.Event) -> None:
        with self._lock:
            if self._thumb_inflight.get(asset_id) is ev:
                self._thumb_inflight.pop(asset_id, None)
        ev.set()

    def clear(self) -> None:
        with self._lock:
            for sa in self._assets.values():
                sa.thumb_bytes = None
            self._jobs.clear()
            self._assets.clear()
            self._preferred_hosts.clear()
            for ev in self._thumb_inflight.values():
                ev.set()
            self._thumb_inflight.clear()


def _filter_job_assets(job: StoredJob, selected_ids: Optional[Sequence[str]]) -> List[StoredAsset]:
    """Filter job assets to requested IDs, or return all when selected_ids is None."""
    if selected_ids is None:
        return list(job.assets)
    wanted = {str(x).strip() for x in selected_ids if str(x).strip()}
    return [sa for sa in job.assets if sa.asset_id in wanted]


def _build_zip_bytes(
    job: StoredJob,
    store: JobStore,
    client: HttpClient,
    config: Config,
    target_assets: Optional[List[StoredAsset]] = None,
) -> Tuple[str, bytes]:
    assets = list(job.assets) if target_assets is None else list(target_assets)
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

    workers = min(config.max_workers, max(1, len(assets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_fetch, sa) for sa in assets]
        for fut in as_completed(futs):
            files.append(fut.result())

    files.sort(key=lambda x: x[0])
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for _, fname, data in files:
            zf.writestr(fname, data)

    title = sanitize_filename(job.result.title, fallback=job.result.platform, max_len=36)
    suffix = f"_sel{len(assets)}" if target_assets is not None and len(assets) < len(job.assets) else ""
    zip_name = f"{job.result.platform}_{title}_{job.result.item_id}{suffix}.zip"
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


def _save_job_to_output(
    job: StoredJob,
    store: JobStore,
    client: HttpClient,
    config: Config,
    target_assets: Optional[List[StoredAsset]] = None,
) -> Tuple[List[Path], List[str]]:
    assets = list(job.assets) if target_assets is None else list(target_assets)
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

    workers = min(config.max_workers, max(1, len(assets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_dl, sa): sa for sa in assets}
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
        timeout = 65

        def handle(self) -> None:
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
                return

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_bytes(
            self,
            status: int,
            content_type: str,
            body: bytes,
            extra: Optional[Dict[str, str]] = None,
            close_conn: bool = False,
        ) -> None:
            if close_conn:
                self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if not extra or "Cache-Control" not in extra:
                self.send_header("Cache-Control", "no-store")
            if close_conn:
                self.send_header("Connection", "close")
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, obj: Any, close_conn: bool = False) -> None:
            self._send_bytes(
                status,
                "application/json; charset=utf-8",
                json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                close_conn=close_conn,
            )

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
                    close_conn=True,
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
                qs = parse_qs(parsed.query, keep_blank_values=True)
                selected_ids: Optional[List[str]] = None
                if "ids" in qs:
                    raw_ids = ",".join(qs["ids"])
                    selected_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
                self._handle_zip(job_id, selected_ids=selected_ids)
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
                self._send_json(HTTPStatus.OK, {"ok": True}, close_conn=True)
                return

            if path == "/api/leave":
                self._drain_body()
                mode = parse_qs(parsed.query).get("mode", ["hidden"])[0]
                delay = 8.0 if mode == "close" else 60.0
                state.mark_leave(delay)
                self._send_json(HTTPStatus.OK, {"ok": True, "mode": mode}, close_conn=True)
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
                                    if sa.asset.thumb_urls
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
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求 JSON 格式有误"}, close_conn=True)
                    return
                _save_web_config(payload, state.config_dir)
                self._send_json(HTTPStatus.OK, {"ok": True}, close_conn=True)
                return

            if path.startswith("/api/save/"):
                job_id = path[len("/api/save/") :]
                try:
                    payload = self._read_json()
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求 JSON 格式有误"})
                    return
                if not isinstance(payload, dict):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求 JSON 格式有误"})
                    return
                job = state.store.get_job(job_id)
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "任务不存在或已过期，请重新提取"})
                    return
                selected_ids: Optional[List[str]] = None
                if payload.get("asset_ids") is not None:
                    if not isinstance(payload["asset_ids"], list):
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "asset_ids 参数必须为数组"})
                        return
                    selected_ids = [str(x) for x in payload["asset_ids"]]
                target_assets = _filter_job_assets(job, selected_ids)
                if not target_assets:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "未选中任何有效文件"})
                    return
                cfg, client = state.client()
                with state.busy():
                    saved, errs = _save_job_to_output(job, state.store, client, cfg, target_assets=target_assets)
                if not saved:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": "; ".join(errs) or "保存失败"})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "saved": len(saved),
                        "total": len(target_assets),
                        "dir": cfg.output_dir.name,
                        "errors": errs,
                    },
                )
                return

            if path == "/api/shutdown":
                if not state.can_shutdown or (not state.hibernate_only and state.server is None):
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "当前模式不允许远程关闭"}, close_conn=True)
                    return
                if state.hibernate_only:
                    state.hibernate()
                    self._send_json(HTTPStatus.OK, {"ok": True, "hibernated": True}, close_conn=True)
                    return
                self._send_json(HTTPStatus.OK, {"ok": True}, close_conn=True)
                threading.Thread(target=state.server.shutdown, daemon=True).start()
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

        def _handle_media(self, asset_id: str, as_download: bool, as_thumb: bool = False) -> None:
            sa = state.store.get_asset(asset_id)
            if not sa:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "资源不存在或已过期，请重新提取"})
                return

            range_hdr = self.headers.get("Range")
            use_thumb = bool(as_thumb and sa.asset.thumb_urls)
            allow_thumb_cache = bool(use_thumb and not as_download and not range_hdr)
            slot_ev: Optional[threading.Event] = None
            owns_slot = False

            if allow_thumb_cache:
                cached = state.store.get_cached_thumb(sa)
                if cached is not None:
                    self._send_bytes(
                        HTTPStatus.OK,
                        cached[1],
                        cached[0],
                        extra={"Cache-Control": "private, max-age=300"},
                    )
                    return
                owns_slot, slot_ev = state.store.acquire_thumb_slot(sa.asset_id)
                if not owns_slot:
                    slot_ev.wait(timeout=12.0)
                    cached = state.store.get_cached_thumb(sa)
                    if cached is not None:
                        self._send_bytes(
                            HTTPStatus.OK,
                            cached[1],
                            cached[0],
                            extra={"Cache-Control": "private, max-age=300"},
                        )
                        return
                    owns_slot, slot_ev = state.store.acquire_thumb_slot(sa.asset_id)

            try:
                _, client = state.client()
                expected = (
                    "image"
                    if use_thumb
                    else ("video" if sa.asset.kind in ("video", "live_video") else "image")
                )
                urls = state.store.thumb_urls_for(sa) if use_thumb else state.store.candidate_urls_for(sa)
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

                    state.store.remember_winning(sa, stream.winning_url, stream.ext, is_thumb=use_thumb)
                    thumb_buf: Optional[bytearray] = None
                    if allow_thumb_cache and stream.status == 200:
                        try:
                            cl_val = int(stream.content_length) if stream.content_length else 0
                        except ValueError:
                            cl_val = 0
                        if cl_val <= _MAX_THUMB_CACHE_BYTES and len(stream.first_chunk) <= _MAX_THUMB_CACHE_BYTES:
                            thumb_buf = bytearray(stream.first_chunk)

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
                        eof = False
                        while True:
                            chunk = stream.resp.read(65536)
                            if not chunk:
                                eof = True
                                break
                            if thumb_buf is not None:
                                if len(thumb_buf) + len(chunk) <= _MAX_THUMB_CACHE_BYTES:
                                    thumb_buf.extend(chunk)
                                else:
                                    thumb_buf = None
                            self.wfile.write(chunk)
                        if eof and thumb_buf:
                            state.store.cache_thumb(sa, bytes(thumb_buf), stream.content_type, stream.ext)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass
                    finally:
                        stream.close()
            finally:
                if owns_slot and slot_ev is not None:
                    state.store.release_thumb_slot(sa.asset_id, slot_ev)

        def _handle_zip(self, job_id: str, selected_ids: Optional[Sequence[str]] = None) -> None:
            job = state.store.get_job(job_id)
            if not job:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "任务不存在或已过期，请重新提取"})
                return
            target_assets = _filter_job_assets(job, selected_ids)
            if not target_assets:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "未选中可打包文件"})
                return
            cfg, client = state.client()
            try:
                with state.busy():
                    zip_name, zip_bytes = _build_zip_bytes(job, state.store, client, cfg, target_assets=target_assets)
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
    request_queue_size = 64


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

    def _prewarm_extractors() -> None:
        cfg = load_config(state.config_dir)
        try:
            from .extractors.x import prewarm as _prewarm_x

            _prewarm_x(cfg)
        except Exception:
            pass
        try:
            from .extractors.douyin import prewarm as _prewarm_douyin

            _prewarm_douyin(cfg)
        except Exception:
            pass

    threading.Thread(target=_prewarm_extractors, daemon=True).start()

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
    print("  media-grab 网页版已启动（抖音 · 小红书 · X 无水印提取）")
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
